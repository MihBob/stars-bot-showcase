"""Ручная выдача заказа и уведомления в группу заказов.

Если автовыдача недоступна или завершилась ошибкой, заказ переводится в
NEEDS_MANUAL: админ получает карточку заказа и подтверждает выдачу командой
``/done <order_id>``. Заказ при этом никогда не теряется.
"""
import logging
from datetime import datetime
from html import escape

from aiogram import Bot

from config import cfg
from database import Order, Session, User


def payment_label(order: Order) -> str:
    return "💎 Крипта (TON)" if order.payment_method.value == "crypto" else "🏦 Рубли"


async def fulfill_order(bot: Bot, order: Order) -> str:
    """Отправляет заказ админу на ручную выдачу. Заказ ждёт команды ``/done``."""
    await notify_admin_manual(bot, order)
    return ""


_EVENT_TITLES = {
    "created": "🛒 Новый заказ",
    "paid": "✅ Заказ оплачен",
    "completed": "🎉 Заказ выдан",
    "failed": "⚠️ Заказ требует разбора",
}

_EVENT_STATUS = {
    "created": "⏳ ожидает оплаты",
    "paid": "✅ оплачен",
    "completed": "🎉 выдан",
    "failed": "⚠️ ручной разбор",
}


def format_order_event(
    order: Order,
    event: str,
    *,
    buyer_username: str = "",
    buyer_name: str = "",
    reason: str = "",
) -> str:
    """Текст уведомления в группу заказов (вынесен отдельно для тестов)."""
    is_test = order.payment_ref == "TEST"
    order_time = datetime.fromtimestamp(order.created_at).strftime("%d.%m.%Y %H:%M")
    title = _EVENT_TITLES.get(event, "ℹ️ Заказ")
    status = _EVENT_STATUS.get(event, event)

    buyer = f'<a href="tg://user?id={order.user_id}">{escape(buyer_name or "профиль")}</a>'
    username_part = f" (@{escape(buyer_username)})" if buyer_username else ""

    if is_test:
        payment = "🧪 тест (без реальных денег)"
    elif event == "created":
        payment = "❌ ещё не оплачена"
        if order.ton_amount:
            payment += f" · 💎 {order.ton_amount:g} TON"
    else:
        payment = "✅ оплачена"
        if order.ton_amount:
            payment += f" · 💎 {order.ton_amount:g} TON"

    lines = [
        title,
        "━━━━━━━━━━━━━━━━━━",
        f"№ заказа: <code>{order.id}</code>",
        f"Время: {order_time}",
        f"🙋 Покупатель: {buyer}{username_part} (id {order.user_id})",
        f"👤 Получатель: @{escape(order.target_username)}",
        f"⭐ Звёзд: {order.stars_qty}",
        f"💰 Сумма заказа: {order.total_rub:.2f} ₽",
        f"💳 Оплата: {payment}",
        f"📦 Статус: {status}",
    ]
    if reason:
        lines.append(f"📝 Причина: {escape(reason)}")

    if event == "created":
        lines.append("\n⏰ Ждём оплату 30 минут.")
    elif event == "paid":
        lines.append("\n⚙️ Заказ передан в автовыдачу.")
    elif event == "completed":
        lines.append("\n⭐ Звёзды отправлены получателю.")

    return "\n".join(lines)


async def notify_order_event(
    bot: Bot,
    order: Order,
    event: str,
    *,
    buyer_username: str = "",
    buyer_name: str = "",
    reason: str = "",
) -> None:
    """Пишет в группу заказов, кто и что заказал и оплатил ли он."""
    if not cfg.orders_chat_id:
        return

    if not buyer_username:
        try:
            async with Session() as session:
                user = await session.get(User, order.user_id)
                if user and user.username:
                    buyer_username = user.username
        except Exception:
            logging.exception("notify_order_event: не удалось получить username")

    text = format_order_event(
        order,
        event,
        buyer_username=buyer_username,
        buyer_name=buyer_name,
        reason=reason,
    )
    try:
        await bot.send_message(cfg.orders_chat_id, text, parse_mode="HTML")
    except Exception:
        # Уведомление не должно ломать оплату/выдачу заказа.
        logging.exception("notify_order_event: не удалось отправить в группу")


async def notify_admin_manual(bot: Bot, order: Order) -> None:
    order_time = datetime.fromtimestamp(order.created_at).strftime("%d.%m.%Y %H:%M")
    is_test = order.payment_ref == "TEST"

    text = (
        f"{'🧪 ТЕСТОВЫЙ заказ (без реальных денег)' if is_test else '🛒 Новый оплаченный заказ'}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"№ заказа: <code>{order.id}</code>\n"
        f"Время: {order_time}\n"
        f"Оплата: {'🧪 Тест' if is_test else payment_label(order)}\n\n"
        f"⭐ Кол-во звёзд: {order.stars_qty}\n"
        f"👤 Кому: @{order.target_username}\n"
        f"🙋 Покупатель: <a href=\"tg://user?id={order.user_id}\">профиль</a> (id {order.user_id})\n\n"
        f"💸 Себестоимость (списать на Fragment): {order.cost_rub} ₽\n"
        f"💰 Прибыль: {order.profit_rub} ₽\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        + ("Это тестовый заказ — реально покупать на Fragment не нужно.\n" if is_test else
           f"Купи звёзды на fragment.com на @{order.target_username}, после чего подтверди:\n")
        + f"<code>/done {order.id}</code>"
    )
    await bot.send_message(cfg.orders_chat_id, text, parse_mode="HTML")


async def notify_needs_manual(bot: Bot, order: Order, reason: str) -> None:
    """
    Уведомление админу, что автозакупка не прошла и заказ переведён в NEEDS_MANUAL.
    Это критично для безопасности: заказ никогда не теряется и не выдаётся молча.
    """
    order_time = datetime.fromtimestamp(order.created_at).strftime("%d.%m.%Y %H:%M")
    is_test = order.payment_ref == "TEST"

    text = (
        f"⚠️ Заказ требует ручного разбора (автовыдача не выполнена)\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"№ заказа: <code>{order.id}</code>\n"
        f"Время: {order_time}\n"
        f"⭐ Кол-во звёзд: {order.stars_qty}\n"
        f"👤 Кому: @{order.target_username}\n"
        f"Оплата: {'🧪 Тест (без денег)' if is_test else payment_label(order)}\n"
        + (
            "Это тестовый заказ — реальные звёзды покупать не нужно.\n\n"
            if is_test
            else "Покупатель уже оплатил — доставь звёзды вручную "
                 "(через поставщик или Fragment).\n\n"
        )
        + f"Причина: {reason}\n\n"
        f"После выдачи подтверди: <code>/done {order.id}</code>"
    )
    await bot.send_message(cfg.orders_chat_id, text, parse_mode="HTML")