"""Интеграционный офлайн-тест связки bot.py + payments.supplier.

Поднимает временную SQLite-базу, подменяет Telegram и поставщик фейками и
проходит путь: PAID -> FULFILLING (заказ у поставщика оплачен) -> COMPLETED.
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
DB = Path(tempfile.gettempdir()) / "stars_bot_flow_test.db"
for p in (DB, Path(str(DB) + "-journal")):
    try:
        p.unlink()
    except FileNotFoundError:
        pass

sys.path.insert(0, str(ROOT))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DB.as_posix()}"
os.environ["BOT_TOKEN"] = "123456:AA-test-token"
os.environ["ADMIN_CHAT_ID"] = "1"
os.environ["ORDERS_CHAT_ID"] = "1"
os.environ["PAYMENT_MODE"] = "TEST"
os.environ["FULFILLMENT_MODE"] = "SUPPLIER"
os.environ["SUPPLIER_API_KEY"] = "dummy-key"
os.environ["SUPPLIER_PAY_CURRENCY"] = "ton"
os.environ["HOT_WALLET_MNEMONIC"] = "one two three"
os.environ["TON_WALLET_ADDRESS"] = "UQ-test-wallet"
os.environ["COLD_WALLET_ADDRESS"] = "UQ-test-cold"
os.environ["SWEEP_ENABLED"] = "true"
os.environ["AUTO_ENABLED"] = "true"
os.environ["AUTO_MAX_TON_PER_ORDER"] = "7"
os.environ["AUTO_DAILY_SPEND_TON"] = "0"
os.environ["AUTO_MAX_ORDERS_PER_HOUR"] = "0"

import bot  # noqa: E402
import database  # noqa: E402
from payments import fragment, supplier  # noqa: E402


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))


class FakeAdapter:
    def __init__(self):
        self.starts = []
        self.status = "delivered"

    async def start_order(self, *, order_id, username, stars_qty, on_created=None):
        invoice = supplier.Invoice(
            order_id=f"sup-{order_id}",
            status="awaiting_payment",
            currency="ton",
            amount_ton=0.5,
            memo=f"sup-{order_id}",
        )
        if on_created is not None:
            await on_created(invoice)
        self.starts.append(order_id)
        return invoice

    async def get_status(self, order_id):
        return supplier.SupplierStatus(
            order_id=order_id,
            status=self.status,
            purchase_tx="purchase-tx" if self.status == "delivered" else None,
        )


async def _noop(*args, **kwargs):
    return None


def _run_flow():
    bot.bot = FakeBot()
    bot.supplier_fulfillment = FakeAdapter()
    bot._sweep_after = _noop
    bot._supplier_since = time.time() - 1

    async def scenario():
        await bot.init_db()

        async with database.Session() as session:
            order = await database.create_order(
                session,
                user_id=42,
                stars_qty=50,
                target_username="buyer",
                cost_rub=67.5,
                total_rub=77.63,
                payment_method=database.PaymentMethod.CRYPTO,
            )
            order.ton_amount = 1.0
            order.status = database.OrderStatus.PAID
            await session.commit()
            order_id = order.id

        # До создания инвойса резервируем всю сумму, которую заплатил покупатель.
        assert await bot._pending_invoice_reserve() == 1.0

        await bot.fulfill_or_manual(order)

        async with database.Session() as session:
            saved = await session.get(database.Order, order_id)
            assert saved.status == database.OrderStatus.FULFILLING
            assert saved.fulfill_ref == f"sup-{order_id}"
            assert saved.supplier_invoice_paid is True
            assert saved.fragment_cost_ton == 0.5

        # Инвойс оплачен — его сумма больше не блокирует sweep.
        assert await bot._pending_invoice_reserve() == 0.0

        # Неоплаченный инвойс (например, упали между созданием и переводом)
        # обязан остаться на hot-кошельке.
        async with database.Session() as session:
            pending = await database.create_order(
                session,
                user_id=44,
                stars_qty=50,
                target_username="pending",
                cost_rub=67.5,
                total_rub=77.63,
                payment_method=database.PaymentMethod.CRYPTO,
            )
            pending.status = database.OrderStatus.FULFILLING
            pending.fulfill_ref = "sup-pending"
            pending.fragment_cost_ton = 0.75
            await session.commit()
        assert await bot._pending_invoice_reserve() == 0.75

        # Повторный вызов (как от второго watcher'а) не должен создать/оплатить заказ ещё раз.
        await bot.fulfill_or_manual(order)
        assert len(bot.supplier_fulfillment.starts) == 1

        async with database.Session() as session:
            saved = await session.get(database.Order, order_id)
        await bot._advance_supplier(saved)

        async with database.Session() as session:
            saved = await session.get(database.Order, order_id)
            assert saved.status == database.OrderStatus.COMPLETED

        # Старый «висяк» без id поставщика автоматически не трогаем.
        async with database.Session() as session:
            old = await database.create_order(
                session,
                user_id=43,
                stars_qty=50,
                target_username="old",
                cost_rub=1,
                total_rub=2,
                payment_method=database.PaymentMethod.CRYPTO,
            )
            old.status = database.OrderStatus.FULFILLING
            old.created_at = time.time() - 30 * 86400
            await session.commit()
        await bot._advance_supplier(old)
        assert len(bot.supplier_fulfillment.starts) == 1

        await database.engine.dispose()

    asyncio.run(scenario())


def test_paid_order_is_fulfilled_via_supplier_and_completed():
    _run_flow()


def test_price_message_shows_rate_bot_and_supplier_prices():
    text = bot.format_price_message(
        rate=95.7,
        bot_price=1.69,
        my_cost_ton=0.0138,
        my_cost_rub=1.32,
        updated_at="2026-11-04T14:00:00",
        auto_enabled=True,
        interval_minutes=60,
    )

    assert "95.70" in text
    assert "🏷️ Моя цена: <b>1.69" in text
    assert "🛒 поставщик: <b>1.32" in text
    assert "0.0138 TON" in text
    assert "04.11.2026 14:00" in text
    assert "Автоцена: вкл" in text


def test_price_message_live_mode():
    text = bot.format_price_message(
        rate=95.7,
        bot_price=1.69,
        my_cost_ton=0.0138,
        my_cost_rub=1.32,
        updated_at="",
        auto_enabled=False,
        interval_minutes=60,
        live=True,
    )

    assert "живой запрос" in text
    assert "Автоцена: выкл" in text


def _order_for_notification():
    return SimpleNamespace(
        id="abc123",
        created_at=time.time(),
        user_id=42,
        target_username="buyer",
        stars_qty=50,
        cost_rub=66.0,
        total_rub=84.67,
        ton_amount=0.8827,
        fragment_cost_ton=0.69,
        payment_ref="memo",
    )


def test_order_notification_created_is_unpaid():
    text = fragment.format_order_event(
        _order_for_notification(),
        "created",
        buyer_username="john",
        buyer_name="John",
    )

    assert "🛒 Новый заказ" in text
    assert "❌ ещё не оплачена" in text
    assert "0.8827 TON" in text
    assert "@buyer" in text
    assert "50" in text
    assert "84.67" in text
    assert "@john" in text
    # Себестоимость и наценка — только в админ-панели, не в группе.
    assert "поставщик" not in text
    assert "Моя цена" not in text
    assert "Цена за 1" not in text


def test_order_notification_paid_shows_amount():
    text = fragment.format_order_event(_order_for_notification(), "paid")

    assert "✅ Заказ оплачен" in text
    assert "✅ оплачена" in text
    assert "0.8827 TON" in text


def test_order_notification_completed_and_escaped_name():
    text = fragment.format_order_event(
        _order_for_notification(),
        "completed",
        buyer_name="<b>John</b>",
    )

    assert "🎉 Заказ выдан" in text
    assert "&lt;b&gt;John&lt;/b&gt;" in text
    assert "Звёзды отправлены получателю" in text


def test_admin_panel_has_prices_button():
    callbacks = [
        button.callback_data
        for row in bot.admin_main_kb().inline_keyboard
        for button in row
    ]
    assert "admin:prices" in callbacks


def test_order_notification_sends_to_orders_group(monkeypatch):
    class FakeNotifyBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, **kwargs):
            self.sent.append((chat_id, text, kwargs))

    fake = FakeNotifyBot()
    monkeypatch.setattr(fragment.cfg, "orders_chat_id", 999, raising=False)

    asyncio.run(
        fragment.notify_order_event(
            fake,
            _order_for_notification(),
            "created",
            buyer_username="john",
        )
    )

    assert fake.sent[0][0] == 999
    assert "Новый заказ" in fake.sent[0][1]
    assert fake.sent[0][2]["parse_mode"] == "HTML"
