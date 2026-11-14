import asyncio
import logging
import time
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeChat,
)
from sqlalchemy import select, func

from config import cfg
from database import (
    init_db, Session, create_order, Order, OrderStatus, PaymentMethod,
    track_user, link_support_message, get_support_user, get_all_user_ids,
    get_setting, set_setting,
)
from pricing import calc_price, get_price_params, set_star_cost, set_margin
from pricing_core import auto_price_sync
from states import BuyStars, AdminPanel, SupportFlow, BroadcastFlow
from payments import ton_wallet, fragment, supplier

logging.basicConfig(level=logging.INFO)

# Если в .env задан PROXY_URL — весь трафик бота к Telegram API пойдёт через него
# (актуально, когда api.telegram.org недоступен напрямую без VPN/прокси)
session = AiohttpSession(proxy=cfg.proxy_url) if cfg.proxy_url else None
bot = Bot(token=cfg.bot_token, session=session)
dp = Dispatcher(storage=MemoryStorage())

# Автовыдача через поставщик: создаём заказ у поставщика и платим его инвойс
# с hot-кошелька. Клиент SDK импортируется лениво, поэтому без ключа бот
# спокойно работает в ручном режиме.
supplier_fulfillment = supplier.SupplierFulfillment(
    cfg.supplier_api_key,
    ton_wallet,
    pay_currency=cfg.supplier_pay_currency,
    callback_url=cfg.supplier_callback_url,
    base_url=cfg.supplier_api_base,
)

# Заказы, которые прямо сейчас обрабатываются в этом процессе. Защищает от
# двойного создания/оплаты, если payment_watcher и supplier_watcher увидят
# один и тот же заказ одновременно.
_in_progress_orders: set[str] = set()

# Момент первого запуска с ключом поставщик. Заказы, созданные раньше, не
# дозапускаем автоматически: их могли уже выдать вручную в старой версии бота.
_supplier_since: float = 0.0

# Не спамим админа одинаковыми ошибками автоцены чаще раза в час.
_auto_price_last_alert: float = 0.0


# ---------- вспомогательное: одна "живая" панель на пользователя ----------
# Вместо того чтобы слать новое сообщение на каждом шаге, храним id одного
# сообщения-панели в FSM-данных и просто редактируем его. Сообщения, которые
# пользователь печатает сам (цифры, username), удаляем сразу после обработки —
# в чате остаётся только одна аккуратная карточка заказа.

async def render_panel(chat_id: int, state: FSMContext, text: str, kb: InlineKeyboardMarkup | None = None):
    data = await state.get_data()
    panel_id = data.get("panel_msg_id")

    if panel_id:
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=panel_id, reply_markup=kb, parse_mode="HTML"
            )
            return
        except TelegramBadRequest:
            pass  # сообщение удалили/устарело — просто пошлём новое ниже

    msg = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
    await state.update_data(panel_msg_id=msg.message_id)


async def delete_user_message(message: Message):
    try:
        await message.delete()
    except TelegramBadRequest:
        pass  # нет прав на удаление (например, в группе) — не критично


def is_admin(user_id: int) -> bool:
    return user_id == cfg.admin_chat_id


# ---------- выдача заказов: авто (Fragment) или ручной режим ----------

async def _auto_spend_today(session) -> float:
    """Сумма (в TON) оплаченных/выполняемых заказов за последние 24 часа — грубая оценка авто-трат."""
    cutoff = time.time() - 86400
    result = await session.execute(
        select(func.coalesce(func.sum(Order.ton_amount), 0)).where(
            Order.created_at >= cutoff,
            Order.status.in_([OrderStatus.PAID, OrderStatus.FULFILLING, OrderStatus.COMPLETED]),
        )
    )
    return float(result.scalar() or 0)


async def _auto_orders_last_hour(session) -> int:
    """Число оплаченных/выполняемых заказов за последний час (анти-флуд)."""
    cutoff = time.time() - 3600
    result = await session.execute(
        select(func.count(Order.id)).where(
            Order.created_at >= cutoff,
            Order.status.in_([OrderStatus.PAID, OrderStatus.FULFILLING, OrderStatus.COMPLETED]),
        )
    )
    return int(result.scalar() or 0)


async def _can_auto(order: Order) -> bool:
    """Проходит ли заказ по всем лимитам автозакупки."""
    if not supplier.auto_allowed(
        fulfillment_mode=cfg.fulfillment_mode,
        api_key=cfg.supplier_api_key,
        auto_enabled=cfg.auto_enabled,
        hot_ready=cfg.hot_ready,
        is_test=order.payment_ref == "TEST",
        ton_amount=order.ton_amount or 0.0,
        max_ton_per_order=cfg.auto_max_ton_per_order,
    ):
        return False

    if cfg.auto_daily_spend_ton > 0:
        async with Session() as session:
            spent = await _auto_spend_today(session)
        if spent + (order.ton_amount or 0) > cfg.auto_daily_spend_ton:
            return False

    if cfg.auto_max_orders_per_hour > 0:
        async with Session() as session:
            cnt = await _auto_orders_last_hour(session)
        if cnt >= cfg.auto_max_orders_per_hour:
            return False

    return True


async def _auto_price_alert(text: str) -> None:
    """Сообщает админу о проблеме автоцены, но не чаще раза в час."""
    global _auto_price_last_alert
    now = time.time()
    if now - _auto_price_last_alert < 3600:
        return
    _auto_price_last_alert = now
    try:
        await bot.send_message(cfg.admin_chat_id, text)
    except Exception:
        logging.exception("auto price alert failed")


async def refresh_auto_price() -> bool:
    """
    Пересчитывает себестоимость и цену звезды от живой закупки поставщик.

    Цена поставщик и курс TON/RUB берутся заново, поэтому фиксированная цена
    не устаревает. В БД записываются реальная себестоимость и эффективная
    маржа (буфер + прибыль), чтобы обычный calc_price работал без изменений.
    """
    if not cfg.auto_price_enabled:
        return False
    if not cfg.supplier_api_key:
        logging.warning("auto price: SUPPLIER_API_KEY не задан — цена не обновляется")
        return False

    try:
        quote = await supplier_fulfillment.get_quote(cfg.auto_price_quantity)
        if quote.currency != "ton":
            raise RuntimeError(
                f"цена поставщик в {quote.currency or '?'}, а нужен ton"
            )
        rate = await ton_wallet.get_ton_rub_rate()
        cost, price, margin = auto_price_sync(
            stars_qty=cfg.auto_price_quantity,
            quote_ton=quote.amount_ton,
            ton_rub=rate,
            margin_percent=cfg.auto_price_margin_percent,
            buffer_percent=cfg.auto_price_buffer_percent,
            fee_ton=cfg.auto_price_fee_ton,
            min_profit_rub=cfg.auto_price_min_profit_rub,
        )
    except Exception as e:
        logging.exception("auto price: не удалось получить цену/курс")
        await _auto_price_alert(
            f"⚠️ Автоцена не обновилась: {type(e).__name__}: {e}\n"
            "Продажи идут по последней сохранённой цене."
        )
        return False

    async with Session() as session:
        old_cost, _ = await get_price_params(session)
        if old_cost > 0 and cost > 0:
            change = abs(cost - old_cost) / old_cost * 100
            # Резкое подорожание применяем (это защищает маржу), а резкое
            # удешевление — нет: оно может быть битым ответом API, и продавать
            # по подозрительно низкой цене опаснее, чем временно завысить её.
            if change > cfg.auto_price_max_change_percent and cost < old_cost:
                logging.error(
                    "auto price: подозрительное снижение себестоимости %.1f%% "
                    "(было %.4f, стало %.4f) — не применяю",
                    change,
                    old_cost,
                    cost,
                )
                await _auto_price_alert(
                    f"⚠️ Автоцена не снижена: себестоимость упала на "
                    f"{change:.1f}% (было {old_cost:.2f} ₽, стало {cost:.2f} ₽). "
                    "Проверь курс и цену поставщик."
                )
                return False
            if change > cfg.auto_price_max_change_percent:
                logging.warning(
                    "auto price: себестоимость выросла на %.1f%% "
                    "(было %.4f, стало %.4f) — применяю, чтобы не уйти в минус",
                    change,
                    old_cost,
                    cost,
                )
                await _auto_price_alert(
                    f"⚠️ Автоцена выросла на {change:.1f}%: было {old_cost:.2f} ₽, "
                    f"стало {cost:.2f} ₽ за звезду. Проверь курс и цену поставщик."
                )

        await set_star_cost(session, cost)
        await set_margin(session, margin)
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        await set_setting(
            session, "auto_price_updated_at", now_utc.isoformat(timespec="seconds")
        )
        await set_setting(session, "auto_price_quote_ton", str(quote.amount_ton))
        await set_setting(session, "auto_price_rate", str(rate))

    logging.info(
        "auto price: %s ⭐ = %.4f TON × %.2f ₽ = %.4f ₽/шт, "
        "цена %.4f ₽/шт (маржа %.2f%%)",
        cfg.auto_price_quantity,
        quote.amount_ton,
        rate,
        cost,
        price,
        margin,
    )
    return True


async def auto_pricer() -> None:
    """Фоновое обновление цены: раз в AUTO_PRICE_INTERVAL_MINUTES."""
    interval = max(60.0, cfg.auto_price_interval_minutes * 60)
    while True:
        await asyncio.sleep(interval)
        try:
            await refresh_auto_price()
        except Exception:
            logging.exception("auto_pricer error")


async def _mark_needs_manual(order: Order, reason: str) -> None:
    """Переводит заказ в NEEDS_MANUAL и уведомляет админа (никогда не теряем оплаченный заказ)."""
    async with Session() as session:
        db_order = await session.get(Order, order.id)
        db_order.status = OrderStatus.NEEDS_MANUAL
        await session.commit()
    try:
        await fragment.notify_needs_manual(bot, order, reason)
    except Exception:
        logging.exception("notify_needs_manual failed")


async def _pending_invoice_reserve() -> float:
    """
    Сколько TON на hot-кошельке нельзя отправлять на cold.

    Это суммы заказов, по которым поставщику ещё не заплачено: если слить их
    в cold, на оплату инвойса не хватит баланса. Для уже выставленного инвойса
    резервируем его точную сумму, для оплаченного, но ещё не обработанного
    заказа — сумму, которую заплатил покупатель (она заведомо покрывает закупку).
    """
    async with Session() as session:
        result = await session.execute(
            select(Order).where(
                Order.status.in_([OrderStatus.PAID, OrderStatus.FULFILLING]),
                Order.supplier_invoice_paid.is_(False),
            )
        )
        orders = result.scalars().all()

    now = time.time()
    max_age = cfg.supplier_resume_max_age_hours * 3600
    reserve = 0.0
    for order in orders:
        if order.fulfill_ref:
            # Инвойс уже создан — резервируем именно его сумму.
            amount = order.fragment_cost_ton or order.ton_amount or 0.0
        else:
            # Ещё не обработанный заказ: те же правила, что и у supplier_watcher,
            # чтобы старые «висяки» не блокировали sweep навсегда.
            if _supplier_since <= 0 or order.created_at < _supplier_since:
                continue
            if now - (order.created_at or 0) > max_age:
                continue
            amount = order.ton_amount or 0.0
        if amount > 0:
            reserve += amount
    return round(reserve, 6)


async def _sweep_after(order: Order) -> None:
    """После выдачи переводит накопившуюся прибыль hot-кошелька на холодный (с буфером)."""
    if not cfg.sweep_enabled or not cfg.hot_ready:
        return
    try:
        reserve = await _pending_invoice_reserve()
        swept = await ton_wallet.sweep_profit(reserve_ton=reserve)
    except Exception as e:
        logging.exception("sweep error")
        try:
            await bot.send_message(
                cfg.admin_chat_id, f"⚠️ Sweep не прошёл для заказа {order.id[:8]}: {e}"
            )
        except Exception:
            pass
        return

    if swept and swept > 0:
        async with Session() as session:
            db_order = await session.get(Order, order.id)
            db_order.swept = True
            await session.commit()


async def _save_supplier_invoice(order_id: str, invoice: supplier.Invoice) -> None:
    """Сохраняет id заказа поставщика ДО отправки денег (защита от двойной оплаты)."""
    async with Session() as session:
        db_order = await session.get(Order, order_id)
        if not db_order:
            raise RuntimeError(f"заказ {order_id} не найден")
        db_order.fulfill_ref = invoice.order_id
        db_order.fragment_cost_ton = invoice.amount_ton
        await session.commit()


async def fulfill_or_manual(order: Order) -> None:
    """
    Единая точка выдачи заказа после подтверждения оплаты.

    - SUPPLIER: проверяем получателя, создаём заказ у поставщика и платим инвойс
      с hot-кошелька. Заказ остаётся FULFILLING до статуса delivered.
    - Иначе (MANUAL / лимит / тестовый заказ / ошибка) — NEEDS_MANUAL, чтобы
      оплаченный заказ никогда не потерялся и не был выдан молча.
    """
    if order.id in _in_progress_orders:
        return
    _in_progress_orders.add(order.id)

    try:
        async with Session() as session:
            db_order = await session.get(Order, order.id)
            if not db_order:
                return
            # Повторный/параллельный вызов не должен создать и оплатить заказ дважды.
            if (
                db_order.fulfill_ref
                or db_order.supplier_invoice_paid
                or db_order.status
                not in (OrderStatus.PAID, OrderStatus.FULFILLING)
            ):
                return
            db_order.status = OrderStatus.FULFILLING
            await session.commit()

        if not await _can_auto(order):
            await _mark_needs_manual(
                order,
                "автовыдача недоступна: режим/ключ поставщик, лимиты, кошелёк или тестовый заказ",
            )
            return

        try:
            invoice = await supplier_fulfillment.start_order(
                order_id=order.id,
                username=order.target_username,
                stars_qty=order.stars_qty,
                on_created=lambda inv: _save_supplier_invoice(order.id, inv),
            )
        except supplier.RecipientIneligible as e:
            await _mark_needs_manual(order, f"получатель не подходит: {e}")
            return
        except Exception as e:
            logging.exception("supplier start_order failed for %s", order.id)
            await _mark_needs_manual(order, f"ошибка поставщик: {e}")
            return

        async with Session() as session:
            db_order = await session.get(Order, order.id)
            db_order.supplier_invoice_paid = True
            db_order.fragment_cost_ton = invoice.amount_ton
            await session.commit()

        logging.info(
            "supplier: заказ %s создан у поставщика (%s), оплачено %.6f TON, ждём delivered",
            order.id,
            invoice.order_id,
            invoice.amount_ton,
        )
    finally:
        _in_progress_orders.discard(order.id)


async def _advance_supplier(order: Order) -> None:
    """Один шаг фонового цикла: дозапуск или опрос статуса у поставщик."""
    if order.id in _in_progress_orders:
        return

    # Заказ оплачен, но поставщик ещё не создан (падение/рестарт между шагами).
    # Идемпотентный ключ = локальный id, поэтому повтор безопасен. Но старые
    # заказы, оставшиеся в FULFILLING от ручного режима, не трогаем: их могли
    # уже выдать вручную, и автопокупка была бы второй выдачей.
    if not order.fulfill_ref:
        # Не трогаем заказы, созданные до первого включения поставщик, и слишком
        # старые «висяки» — риск повторной выдачи реальному покупателю.
        if _supplier_since <= 0 or order.created_at < _supplier_since:
            return
        age = time.time() - (order.created_at or 0)
        if age > cfg.supplier_resume_max_age_hours * 3600:
            return
        await fulfill_or_manual(order)
        return

    remote = await supplier_fulfillment.get_status(order.fulfill_ref)
    action = supplier.next_action(remote.status, order.supplier_invoice_paid)

    if action == "mark_paid":
        # Поставщик уже видит оплату — значит платёж дошёл, просто флаг не успел сохраниться.
        async with Session() as session:
            db_order = await session.get(Order, order.id)
            if db_order and not db_order.supplier_invoice_paid:
                db_order.supplier_invoice_paid = True
                await session.commit()
        return

    if action == "complete":
        async with Session() as session:
            db_order = await session.get(Order, order.id)
            if not db_order or db_order.status != OrderStatus.FULFILLING:
                return
            db_order.status = OrderStatus.COMPLETED
            await session.commit()

        logging.info(
            "supplier: заказ %s доставлен (tx %s)", order.id, remote.purchase_tx or "?"
        )
        await fragment.notify_order_event(bot, order, "completed")
        try:
            await bot.send_message(
                order.user_id,
                f"🎉 Готово! {order.stars_qty} ⭐ отправлены на @{order.target_username}.",
            )
        except Exception:
            logging.exception("не удалось уведомить покупателя по заказу %s", order.id)
        await _sweep_after(order)
        return

    if action == "manual":
        reason = remote.failure_reason or remote.telegram_message or remote.status
        await _mark_needs_manual(order, f"поставщик: {remote.status} ({reason})")
        return

    if action == "manual_unknown_payment":
        await _mark_needs_manual(
            order,
            "заказ у поставщика создан, но оплата инвойса не подтверждена — проверь вручную",
        )
        return

    # action == "wait" — поставщик ещё обрабатывает заказ, просто ждём следующий цикл.


async def supplier_watcher():
    """Раз в 20 секунд двигает оплаченные заказы: старт у поставщик и опрос статуса."""
    while True:
        try:
            async with Session() as session:
                result = await session.execute(
                    select(Order).where(
                        Order.status.in_([OrderStatus.PAID, OrderStatus.FULFILLING])
                    )
                )
                orders = result.scalars().all()

            for order in orders:
                try:
                    await _advance_supplier(order)
                except Exception:
                    logging.exception("supplier_watcher: заказ %s", order.id)

        except Exception:
            logging.exception("supplier_watcher error")

        await asyncio.sleep(20)


# ---------- /start ----------

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Купить звёзды", callback_data="menu:buy")],
            [InlineKeyboardButton(text="📜 Мои покупки", callback_data="menu:history")],
            [InlineKeyboardButton(text="🆘 Поддержка", callback_data="menu:support")],
        ]
    )


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="menu:cancel")]]
    )


async def show_main_menu(chat_id: int, state: FSMContext):
    await state.clear()
    await render_panel(
        chat_id,
        state,
        "Привет! Здесь можно купить Telegram Stars.\n\n"
        f"Минимальное количество — {cfg.min_stars} ⭐",
        main_menu_kb(),
    )


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await delete_user_message(message)
    async with Session() as session:
        await track_user(session, message.from_user.id, message.from_user.username or "")
    await show_main_menu(message.chat.id, state)


def _human_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "только что"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч назад"
    return f"{hours // 24} дн назад"


def format_price_message(
    *,
    rate: float,
    bot_price: float,
    my_cost_ton: float,
    my_cost_rub: float,
    updated_at: str,
    auto_enabled: bool,
    interval_minutes: float,
    live: bool = False,
) -> str:
    """Текст ответа /price (вынесен отдельно, чтобы тестировать без Telegram)."""
    lines = [
        "💱 <b>Курс TON</b>",
        f"1 TON = <b>{rate:.2f} ₽</b>",
        "",
        "⭐ <b>Цена за 1 звезду</b>",
        f"🏷️ Моя цена: <b>{bot_price:.2f} ₽</b>",
        f"🛒 поставщик: <b>{my_cost_rub:.2f} ₽</b> ({my_cost_ton:.4f} TON)",
    ]

    if live:
        lines += ["", "🕒 Данные получены сейчас (живой запрос)"]
    elif updated_at:
        try:
            dt = datetime.fromisoformat(updated_at)
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            age = _human_age((now_utc - dt).total_seconds())
            lines += [
                "",
                f"🕒 Обновлено: {dt.strftime('%d.%m.%Y %H:%M')} UTC ({age})",
            ]
        except ValueError:
            lines += ["", f"🕒 Обновлено: {updated_at}"]
    else:
        lines += ["", "🕒 Обновлено: ещё не было"]

    lines.append(
        f"⚙️ Автоцена: {'вкл' if auto_enabled else 'выкл'}, "
        f"каждые {interval_minutes:g} мин"
    )
    return "\n".join(lines)


async def build_price_view_text() -> str:
    """Готовит текст с курсом TON, ценой бота и ценой поставщик."""
    qty = cfg.auto_price_quantity
    quote_ton: float | None = None
    rate: float | None = None
    updated_at = ""
    auto_enabled = cfg.auto_price_enabled

    try:
        async with Session() as session:
            star_cost, margin = await get_price_params(session)
            updated_at = await get_setting(session, "auto_price_updated_at", "")
            quote_raw = await get_setting(session, "auto_price_quote_ton", "")
            rate_raw = await get_setting(session, "auto_price_rate", "")
        if quote_raw:
            quote_ton = float(quote_raw)
        if rate_raw:
            rate = float(rate_raw)
    except Exception:
        logging.exception("price view: не удалось прочитать настройки")
        star_cost, margin = cfg.star_cost_rub, cfg.margin_percent

    # Если автоцена ещё ни разу не сохранялась — показываем живые данные,
    # но ничего не записываем в БД (просмотр только читает).
    live = False
    if quote_ton is None or rate is None:
        try:
            quote = await supplier_fulfillment.get_quote(qty)
            quote_ton = quote.amount_ton
            rate = await ton_wallet.get_ton_rub_rate()
            live = True
        except Exception as e:
            logging.exception("price view: не удалось получить живые данные")
            raise RuntimeError(
                f"не удалось получить курс TON и цену поставщик: {e}"
            ) from e

    bot_price = star_cost * (1 + margin / 100)
    my_cost_ton = quote_ton / qty
    my_cost_rub = my_cost_ton * rate

    return format_price_message(
        rate=rate,
        bot_price=bot_price,
        my_cost_ton=my_cost_ton,
        my_cost_rub=my_cost_rub,
        updated_at=updated_at,
        auto_enabled=auto_enabled,
        interval_minutes=cfg.auto_price_interval_minutes,
        live=live,
    )


@dp.callback_query(F.data == "menu:cancel")
async def menu_cancel(call: CallbackQuery, state: FSMContext):
    await show_main_menu(call.message.chat.id, state)
    await call.answer()


@dp.callback_query(F.data == "menu:history")
async def menu_history(call: CallbackQuery, state: FSMContext):
    async with Session() as session:
        result = await session.execute(
            select(Order)
            .where(Order.user_id == call.from_user.id, Order.status == OrderStatus.COMPLETED)
            .order_by(Order.created_at.desc())
            .limit(15)
        )
        orders = result.scalars().all()

    if not orders:
        text = "У тебя пока нет завершённых покупок."
    else:
        lines = ["📜 Твои покупки:\n"]
        for o in orders:
            dt = datetime.fromtimestamp(o.created_at).strftime("%d.%m.%Y %H:%M")
            lines.append(f"{dt} · ⭐ {o.stars_qty} → @{o.target_username}")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 В меню", callback_data="menu:cancel")]]
    )
    await render_panel(call.message.chat.id, state, text, kb)
    await call.answer()


# ---------- поддержка: сообщение от покупателя -> чат поддержки, ответ админа -> обратно покупателю ----------

@dp.callback_query(F.data == "menu:support")
async def menu_support(call: CallbackQuery, state: FSMContext):
    await state.set_state(SupportFlow.waiting_message)
    await render_panel(
        call.message.chat.id,
        state,
        "Напиши свой вопрос одним сообщением — перешлём его в поддержку, "
        "ответ придёт сюда же.",
        cancel_kb(),
    )
    await call.answer()


@dp.message(SupportFlow.waiting_message)
async def support_message(message: Message, state: FSMContext):
    await delete_user_message(message)

    if not message.text:
        await render_panel(message.chat.id, state, "Пока поддерживаются только текстовые сообщения. Напиши текстом:", cancel_kb())
        return

    user = message.from_user
    username_part = f"@{user.username}" if user.username else "(без username)"
    forwarded = await bot.send_message(
        cfg.orders_chat_id,
        f"🆘 Вопрос в поддержку\n"
        f"От: <a href=\"tg://user?id={user.id}\">{user.full_name}</a> {username_part} (id {user.id})\n\n"
        f"{message.text}\n\n"
        f"↩️ Ответь на это сообщение (Reply), чтобы отправить ответ пользователю.",
        parse_mode="HTML",
    )

    async with Session() as session:
        await link_support_message(session, forwarded.message_id, user.id)

    await state.clear()
    await render_panel(
        message.chat.id,
        state,
        "✅ Вопрос отправлен в поддержку. Как только ответят — пришлём сюда сообщение.",
        main_menu_kb(),
    )


@dp.message(F.chat.id == cfg.orders_chat_id, F.reply_to_message)
async def support_admin_reply(message: Message):
    if not is_admin(message.from_user.id):
        return
    if not message.text:
        return

    async with Session() as session:
        target_user_id = await get_support_user(session, message.reply_to_message.message_id)

    if not target_user_id:
        return  # это Reply на что-то другое (не на вопрос в поддержку) — не наше дело

    try:
        await bot.send_message(target_user_id, f"💬 Ответ поддержки:\n\n{message.text}")
        await message.reply("✅ Отправлено пользователю.")
    except TelegramBadRequest:
        await message.reply("⚠️ Не удалось отправить — возможно, пользователь заблокировал бота.")


# ---------- шаг 1: количество звёзд ----------

QUICK_AMOUNTS = [50, 100, 250, 500]


def qty_kb() -> InlineKeyboardMarkup:
    amount_buttons = [
        InlineKeyboardButton(text=f"{n} ⭐", callback_data=f"qty:{n}") for n in QUICK_AMOUNTS
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            amount_buttons[:2],
            amount_buttons[2:],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:cancel")],
        ]
    )


async def show_qty_prompt(chat_id: int, state: FSMContext):
    await state.set_state(BuyStars.waiting_qty)
    await render_panel(
        chat_id,
        state,
        f"Сколько звёзд купить? Выбери вариант ниже или напиши число (минимум {cfg.min_stars}):",
        qty_kb(),
    )


@dp.message(Command("buy"))
async def buy_start(message: Message, state: FSMContext):
    await delete_user_message(message)
    await show_qty_prompt(message.chat.id, state)


@dp.callback_query(F.data == "menu:buy")
async def menu_buy(call: CallbackQuery, state: FSMContext):
    await show_qty_prompt(call.message.chat.id, state)
    await call.answer()


async def process_qty(chat_id: int, state: FSMContext, qty: int):
    if qty < cfg.min_stars:
        await render_panel(
            chat_id, state, f"Минимум {cfg.min_stars} звёзд. Попробуй ещё раз:", qty_kb()
        )
        return

    await state.update_data(qty=qty)
    await state.set_state(BuyStars.waiting_username)
    await render_panel(
        chat_id,
        state,
        "На какой username отправить звёзды? (без @)\n\n"
        "⚠️ Проверь username внимательно — если он введён неверно "
        "(опечатка, чужой аккаунт), деньги не возвращаются.",
        cancel_kb(),
    )


@dp.callback_query(BuyStars.waiting_qty, F.data.startswith("qty:"))
async def qty_quick_pick(call: CallbackQuery, state: FSMContext):
    qty = int(call.data.split(":", 1)[1])
    await process_qty(call.message.chat.id, state, qty)
    await call.answer()


@dp.message(BuyStars.waiting_qty)
async def buy_qty(message: Message, state: FSMContext):
    await delete_user_message(message)

    if not message.text or not message.text.isdigit():
        await render_panel(message.chat.id, state, "Введи число, пожалуйста.", qty_kb())
        return

    await process_qty(message.chat.id, state, int(message.text))


# ---------- шаг 2: username получателя ----------

@dp.message(BuyStars.waiting_username)
async def buy_username(message: Message, state: FSMContext):
    await delete_user_message(message)

    username = (message.text or "").strip().lstrip("@")
    if not username or " " in username:
        await render_panel(message.chat.id, state, "Похоже на некорректный username. Введи ещё раз.", cancel_kb())
        return

    data = await state.update_data(username=username)
    qty = data["qty"]
    async with Session() as session:
        cost_rub, total_rub, _ = await calc_price(session, qty)

    if cfg.payment_mode == "TEST":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🧪 Тестовая оплата (без денег)", callback_data="pay:test")],
                [InlineKeyboardButton(text="🔙 Изменить количество", callback_data="edit:qty")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:cancel")],
            ]
        )
    else:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💎 Оплатить TON", callback_data="pay:ton")],
                [InlineKeyboardButton(text="🔙 Изменить количество", callback_data="edit:qty")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:cancel")],
            ]
        )
    await state.set_state(BuyStars.waiting_payment_method)
    await render_panel(
        message.chat.id,
        state,
        f"Заказ:\n"
        f"⭐ {qty} звёзд для @{username}\n"
        f"💰 К оплате: {total_rub} ₽\n\n"
        f"⚠️ Внимательно проверь username получателя выше — "
        f"после оплаты изменить его нельзя, а при ошибке деньги не возвращаются.\n\n"
        f"Выбери способ оплаты:",
        kb,
    )


# ---------- шаг 3: выбор способа оплаты ----------

@dp.callback_query(BuyStars.waiting_payment_method, F.data == "edit:qty")
async def edit_qty(call: CallbackQuery, state: FSMContext):
    await show_qty_prompt(call.message.chat.id, state)
    await call.answer()


@dp.callback_query(BuyStars.waiting_payment_method, F.data == "pay:test")
async def pay_test(call: CallbackQuery, state: FSMContext):
    """
    Тестовая оплата — без реальных денег. Сразу создаёт заказ со статусом PAID
    и запускает ту же цепочку, что и настоящая оплата (уведомление в группу заказов,
    команда /done для ручного подтверждения). Нужна, пока не подключены реальные
    платежи (например, из-за возрастных ограничений на платёжных сервисах).
    """
    data = await state.get_data()
    qty, username = data["qty"], data["username"]

    async with Session() as session:
        cost_rub, total_rub, _ = await calc_price(session, qty)
        order = await create_order(
            session, call.from_user.id, qty, username, cost_rub, total_rub, PaymentMethod.CRYPTO
        )
        order.payment_ref = "TEST"
        order.status = OrderStatus.PAID
        await session.commit()
        await session.refresh(order)

    await fragment.notify_order_event(
        bot,
        order,
        "paid",
        buyer_name=call.from_user.full_name,
        buyer_username=call.from_user.username or "",
    )

    await state.clear()

    await render_panel(
        call.message.chat.id,
        state,
        f"🧪 Тестовая оплата принята!\n"
        f"⭐ {qty} звёзд для @{username}\n"
        f"💰 Сумма (тест): {total_rub} ₽\n\n"
        f"Заказ передан на обработку.",
        main_menu_kb(),
    )

    await fulfill_or_manual(order)

    await call.answer()


@dp.callback_query(BuyStars.waiting_payment_method, F.data == "pay:ton")
async def pay_ton(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    qty, username = data["qty"], data["username"]

    async with Session() as session:
        cost_rub, total_rub, _ = await calc_price(session, qty)
        order = await create_order(
            session, call.from_user.id, qty, username, cost_rub, total_rub, PaymentMethod.CRYPTO
        )

    try:
        payment = await ton_wallet.create_payment_request(total_rub, order.id)
    except Exception as e:
        await render_panel(
            call.message.chat.id,
            state,
            f"Не удалось подготовить счёт на оплату (проблема с получением курса или "
            f"адрес кошелька не настроен). Попробуй позже.\n\nОшибка: {e}",
            main_menu_kb(),
        )
        await call.answer()
        return

    async with Session() as session:
        db_order = await session.get(Order, order.id)
        db_order.payment_ref = payment["memo"]
        db_order.ton_amount = payment["ton_amount"]
        await session.commit()

    await fragment.notify_order_event(
        bot,
        db_order,
        "created",
        buyer_name=call.from_user.full_name,
        buyer_username=call.from_user.username or "",
    )

    await state.set_state(BuyStars.waiting_payment)
    await state.update_data(order_id=order.id)

    # Deep-ссылка на кошелёк с уже заполненными адресом, суммой и комментарием —
    # покупателю не нужно ничего вводить вручную, только подтвердить перевод в кошельке.
    nano_amount = int(payment["ton_amount"] * 1_000_000_000)
    wallet_link = (
        f"ton://transfer/{payment['address']}"
        f"?amount={nano_amount}&text={payment['memo']}"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💎 Открыть кошелёк и оплатить", url=wallet_link)],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="menu:cancel")],
        ]
    )
    await render_panel(
        call.message.chat.id,
        state,
        f"💎 К оплате: <b>{payment['ton_amount']} TON</b>\n\n"
        f"Нажми кнопку ниже — откроется твой TON-кошелёк с уже заполненной суммой "
        f"и адресом, останется только подтвердить перевод.\n\n"
        f"Если кнопка не сработала (кошелёк не поддерживает автозаполнение), "
        f"переведи вручную:\n"
        f"Адрес: <code>{payment['address']}</code>\n"
        f"Сумма: <code>{payment['ton_amount']}</code> TON\n"
        f"Комментарий (обязательно!): <code>{payment['memo']}</code>\n\n"
        f"⏰ Оплати в течение 30 минут — после этого счёт автоматически "
        f"отменится и комментарий перестанет действовать.\n"
        f"После перевода статус обновится автоматически (обычно 1-2 минуты).",
        kb,
    )
    await call.answer()


# ---------- фоновая проверка оплат ----------

async def payment_watcher():
    """Раз в 20 секунд проверяет неоплаченные заказы и запускает fulfillment при оплате."""
    ORDER_TIMEOUT_SECONDS = 30 * 60  # 30 минут на оплату, потом заказ автоматически отменяется

    while True:
        try:
            async with Session() as session:
                from sqlalchemy import select

                result = await session.execute(
                    select(Order).where(Order.status == OrderStatus.AWAITING_PAYMENT)
                )
                pending_orders = result.scalars().all()

                for order in pending_orders:
                    paid = False
                    if order.payment_method == PaymentMethod.CRYPTO and order.payment_ref:
                        paid = await ton_wallet.find_incoming_payment(order.payment_ref, order.ton_amount)

                    if paid:
                        order.status = OrderStatus.PAID
                        await session.commit()

                        await fragment.notify_order_event(bot, order, "paid")

                        await bot.send_message(
                            order.user_id,
                            f"✅ Оплата получена! Готовим {order.stars_qty} ⭐ для @{order.target_username}.",
                        )

                        await fulfill_or_manual(order)
                        continue

                    # Оплаты нет — проверяем, не истёк ли срок ожидания
                    if time.time() - order.created_at > ORDER_TIMEOUT_SECONDS:
                        order.status = OrderStatus.EXPIRED
                        await session.commit()
                        try:
                            await bot.send_message(
                                order.user_id,
                                f"⏰ Время на оплату заказа истекло (30 минут). "
                                f"Счёт отменён, комментарий больше не действителен.\n"
                                f"Если всё ещё хочешь купить — оформи новый заказ через меню.",
                            )
                        except TelegramBadRequest:
                            pass

        except Exception:
            logging.exception("payment_watcher error")

        await asyncio.sleep(20)


# ---------- подтверждение выдачи заказа — кнопкой прямо под уведомлением ----------

async def complete_order(order_id: str) -> tuple[bool, str]:
    """Помечает заказ завершённым, уведомляет покупателя. Возвращает (успех, текст_результата)."""
    async with Session() as session:
        order = await session.get(Order, order_id)
        if not order:
            return False, "Заказ не найден."
        if order.status == OrderStatus.COMPLETED:
            return False, "Этот заказ уже отмечен как выполненный."

        order.status = OrderStatus.COMPLETED
        await session.commit()

        await fragment.notify_order_event(bot, order, "completed")

        await bot.send_message(
            order.user_id,
            f"🎉 Готово! {order.stars_qty} ⭐ отправлены на @{order.target_username}.",
        )

        await _sweep_after(order)
    return True, f"Заказ {order_id[:8]} закрыт."


@dp.callback_query(F.data.startswith("done:"))
async def done_button(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Только админ может подтверждать заказы.", show_alert=True)
        return

    order_id = call.data.split(":", 1)[1]
    ok, result_text = await complete_order(order_id)

    if not ok:
        await call.answer(result_text, show_alert=True)
        return

    # Убираем кнопку и дописываем отметку о выполнении прямо в исходное уведомление
    original_text = call.message.html_text or call.message.text or ""
    await call.message.edit_text(
        original_text + "\n\n✅ <b>Выполнено</b>", parse_mode="HTML", reply_markup=None
    )
    await call.answer("Готово ✅")


# Оставляем /done как запасной вариант (например, если кнопка потерялась) —
# работает так же, просто без кнопки.
@dp.message(Command("done"))
async def mark_done(message: Message):
    if not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Использование: /done <order_id>")
        return

    ok, result_text = await complete_order(parts[1])
    await message.answer(result_text)


# ---------- admin: управление автозакупкой (kill-switch) ----------

@dp.message(Command("auto"))
async def auto_control(message: Message):
    if not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split()
    arg = parts[1].lower() if len(parts) > 1 else ""

    if arg == "off":
        cfg.auto_enabled = False
        await message.answer("⛔ Автозакупка ОТКЛЮЧЕНА. Оплаченные заказы будут уходить на ручной разбор.")
    elif arg == "on":
        cfg.auto_enabled = True
        await message.answer("✅ Автозакупка включена (действуют лимиты из .env).")
    else:
        state = "✅ включена" if cfg.auto_enabled else "⛔ отключена"
        await message.answer(
            f"Режим выдачи: {cfg.fulfillment_mode}\n"
            f"поставщик ключ: {'✅ есть' if cfg.supplier_api_key else '❌ нет'}\n"
            f"Автозакупка: {state}\n"
            f"Лимит/заказ: {cfg.auto_max_ton_per_order} TON\n"
            f"Дневной бюджет: {cfg.auto_daily_spend_ton} TON (0 = без лимита)\n"
            f"Макс заказов/час: {cfg.auto_max_orders_per_hour}\n"
            f"Sweep на cold: {'вкл' if cfg.sweep_enabled else 'выкл'}"
        )


# ---------- админ-панель: кнопки вместо команд, одно "живое" сообщение ----------

async def build_stats_text() -> str:
    async with Session() as session:
        total_orders = (await session.execute(select(func.count(Order.id)))).scalar() or 0
        completed = (
            await session.execute(
                select(func.count(Order.id)).where(Order.status == OrderStatus.COMPLETED)
            )
        ).scalar() or 0
        revenue = (
            await session.execute(
                select(func.sum(Order.total_rub)).where(Order.status == OrderStatus.COMPLETED)
            )
        ).scalar() or 0
        profit = (
            await session.execute(
                select(func.sum(Order.profit_rub)).where(Order.status == OrderStatus.COMPLETED)
            )
        ).scalar() or 0
        stars_sold = (
            await session.execute(
                select(func.sum(Order.stars_qty)).where(Order.status == OrderStatus.COMPLETED)
            )
        ).scalar() or 0
        pending = (
            await session.execute(
                select(func.count(Order.id)).where(
                    Order.status.in_([OrderStatus.PAID, OrderStatus.FULFILLING])
                )
            )
        ).scalar() or 0

    return (
        f"📊 Статистика\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Всего заказов: {total_orders}\n"
        f"Завершено: {completed}\n"
        f"В работе (оплачены, ждут выдачи): {pending}\n\n"
        f"⭐ Продано звёзд: {int(stars_sold)}\n"
        f"💰 Выручка: {revenue:.2f} ₽\n"
        f"💸 Прибыль: {profit:.2f} ₽"
    )


async def build_orders_text() -> str:
    async with Session() as session:
        result = await session.execute(
            select(Order).order_by(Order.created_at.desc()).limit(10)
        )
        orders = result.scalars().all()

    if not orders:
        return "Заказов пока нет."

    status_labels = {
        OrderStatus.AWAITING_PAYMENT: "⏳ ждёт оплаты",
        OrderStatus.PAID: "✅ оплачен",
        OrderStatus.FULFILLING: "⚙️ в работе",
        OrderStatus.COMPLETED: "🎉 завершён",
        OrderStatus.FAILED: "❌ ошибка",
        OrderStatus.EXPIRED: "⌛ истёк",
    }

    lines = ["📋 Последние 10 заказов:\n"]
    for o in orders:
        dt = datetime.fromtimestamp(o.created_at).strftime("%d.%m %H:%M")
        lines.append(
            f"<code>{o.id[:8]}</code> · {dt} · {o.stars_qty}⭐ · @{o.target_username} · "
            f"{status_labels.get(o.status, o.status)}"
        )
    return "\n".join(lines)


async def build_price_text() -> str:
    async with Session() as session:
        star_cost, margin = await get_price_params(session)
        updated_at = await get_setting(session, "auto_price_updated_at", "")

    lines = [
        "💲 Текущая цена",
        f"Себестоимость за звезду: {star_cost} ₽",
        f"Маржа: {margin}%",
    ]
    if cfg.auto_price_enabled:
        lines.append(
            f"Автоцена: вкл, обновление каждые {cfg.auto_price_interval_minutes:g} мин"
        )
        if updated_at:
            lines.append(f"Последнее обновление: {updated_at} UTC")
        lines.append(
            "Ручные себестоимость/маржа при автоцене перезаписываются автоматически."
        )
    else:
        lines.append("Автоцена: выкл (цена задана вручную)")
    return "\n".join(lines)


def admin_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
            [InlineKeyboardButton(text="📋 Последние заказы", callback_data="admin:orders")],
            [InlineKeyboardButton(text="💲 Цена и маржа", callback_data="admin:price")],
            [InlineKeyboardButton(text="💵 Курс TON и цены", callback_data="admin:prices")],
            [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast")],
        ]
    )


def admin_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")]]
    )


def admin_price_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить себестоимость", callback_data="admin:setcost")],
            [InlineKeyboardButton(text="✏️ Изменить маржу", callback_data="admin:setmargin")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")],
        ]
    )


@dp.message(Command("panel"))
async def admin_panel_open(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await delete_user_message(message)
    await state.clear()
    await render_panel(message.chat.id, state, "🛠 Админ-панель", admin_main_kb())


@dp.callback_query(F.data == "admin:menu")
async def admin_panel_menu(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await state.clear()
    await render_panel(call.message.chat.id, state, "🛠 Админ-панель", admin_main_kb())
    await call.answer()


@dp.callback_query(F.data == "admin:prices")
async def admin_prices(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    try:
        text = await build_price_view_text()
    except Exception as e:
        logging.exception("admin prices: не удалось получить данные")
        text = (
            "⚠️ Не удалось получить курс TON и цену поставщик.\n"
            f"Ошибка: <code>{type(e).__name__}: {e}</code>"
        )
    await render_panel(call.message.chat.id, state, text, admin_back_kb())
    await call.answer()


@dp.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await state.set_state(BroadcastFlow.waiting_text)
    await render_panel(
        call.message.chat.id,
        state,
        "📢 Напиши текст рассылки одним сообщением — он уйдёт всем, кто хоть раз "
        "писал боту /start.",
        admin_back_kb(),
    )
    await call.answer()


@dp.message(BroadcastFlow.waiting_text)
async def admin_broadcast_send(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await delete_user_message(message)

    if not message.text:
        await render_panel(
            message.chat.id, state, "Пока поддерживается только текст. Напиши текстом:", admin_back_kb()
        )
        return

    text = message.text
    async with Session() as session:
        user_ids = await get_all_user_ids(session)

    await render_panel(message.chat.id, state, f"Рассылаю {len(user_ids)} пользователям...")

    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # не долбим Telegram API слишком быстро

    await state.clear()
    await render_panel(
        message.chat.id,
        state,
        f"✅ Рассылка завершена\nОтправлено: {sent}\nНе доставлено: {failed} "
        f"(скорее всего, заблокировали бота)",
        admin_main_kb(),
    )


@dp.callback_query(F.data == "admin:stats")
async def admin_panel_stats(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    text = await build_stats_text()
    await render_panel(call.message.chat.id, state, text, admin_back_kb())
    await call.answer()


@dp.callback_query(F.data == "admin:orders")
async def admin_panel_orders(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    text = await build_orders_text()
    await render_panel(call.message.chat.id, state, text, admin_back_kb())
    await call.answer()


@dp.callback_query(F.data == "admin:price")
async def admin_panel_price(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    text = await build_price_text()
    await render_panel(call.message.chat.id, state, text, admin_price_kb())
    await call.answer()


@dp.callback_query(F.data == "admin:setcost")
async def admin_panel_setcost(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await state.set_state(AdminPanel.waiting_cost)
    await render_panel(
        call.message.chat.id, state, "Введи новую себестоимость звезды числом (например 1.35):"
    )
    await call.answer()


@dp.callback_query(F.data == "admin:setmargin")
async def admin_panel_setmargin(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await state.set_state(AdminPanel.waiting_margin)
    await render_panel(
        call.message.chat.id, state, "Введи новую маржу в процентах числом (например 15):"
    )
    await call.answer()


@dp.message(AdminPanel.waiting_cost)
async def admin_set_cost_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await delete_user_message(message)

    try:
        value = float((message.text or "").replace(",", "."))
    except ValueError:
        await render_panel(message.chat.id, state, "Нужно число, например: 1.35\nПопробуй ещё раз:")
        return

    async with Session() as session:
        await set_star_cost(session, value)

    await state.clear()
    text = await build_price_text()
    await render_panel(
        message.chat.id, state, f"✅ Себестоимость обновлена: {value} ₽\n\n" + text, admin_price_kb()
    )


@dp.message(AdminPanel.waiting_margin)
async def admin_set_margin_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await delete_user_message(message)

    try:
        value = float((message.text or "").replace(",", "."))
    except ValueError:
        await render_panel(message.chat.id, state, "Нужно число, например: 15\nПопробуй ещё раз:")
        return

    async with Session() as session:
        await set_margin(session, value)

    await state.clear()
    text = await build_price_text()
    await render_panel(
        message.chat.id, state, f"✅ Маржа обновлена: {value}%\n\n" + text, admin_price_kb()
    )


async def setup_commands():
    """
    Настраивает меню команд Telegram (появляется по кнопке "Меню" / при вводе "/").
    В личке с ботом (покупатели, включая тебя) — только /start и /buy.
    В группе заказов (ORDERS_CHAT_ID) — только /panel (дальше всё через кнопки).
    """
    public_commands = [
        BotCommand(command="start", description="Начать / главное меню"),
        BotCommand(command="buy", description="Купить звёзды"),
    ]
    await bot.set_my_commands(public_commands, scope=BotCommandScopeDefault())

    if cfg.admin_chat_id:
        try:
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=cfg.admin_chat_id))
        except TelegramBadRequest:
            pass

    if cfg.orders_chat_id:
        admin_commands = [
            BotCommand(command="panel", description="🛠 Открыть админ-панель"),
            BotCommand(command="auto", description="⚙️ Автозакупка: on/off/статус"),
        ]
        try:
            await bot.set_my_commands(
                admin_commands, scope=BotCommandScopeChat(chat_id=cfg.orders_chat_id)
            )
        except TelegramBadRequest:
            pass


async def _ensure_supplier_since() -> float:
    """Запоминает первый запуск с ключом поставщик (для безопасного рестарта)."""
    if not cfg.supplier_api_key:
        return 0.0
    async with Session() as session:
        raw = await get_setting(session, "supplier_since", "")
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
        now = time.time()
        await set_setting(session, "supplier_since", str(now))
        logging.info(
            "поставщик: автовыдача включена с %s",
            datetime.fromtimestamp(now).strftime("%d.%m.%Y %H:%M"),
        )
        return now


async def main():
    global _supplier_since
    await init_db()
    _supplier_since = await _ensure_supplier_since()
    await setup_commands()
    if cfg.auto_price_enabled:
        # Сразу считаем цену от живой закупки, чтобы не стартовать со старой.
        await refresh_auto_price()
        asyncio.create_task(auto_pricer())
    asyncio.create_task(payment_watcher())
    asyncio.create_task(supplier_watcher())
    try:
        await dp.start_polling(bot)
    finally:
        await supplier_fulfillment.aclose()
        await ton_wallet.close()


if __name__ == "__main__":
    asyncio.run(main())
