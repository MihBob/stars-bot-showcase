"""Офлайн-тесты адаптера поставщик и правил автозакупки.

Запуск: python -m pytest tests/test_supplier.py -q
Сеть и SDK не нужны: клиент и кошелёк подменяются фейками.
"""
import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from payments import supplier


class FakeWallet:
    def __init__(self):
        self.calls = []

    async def transfer(self, to_address: str, amount_ton: float, comment: str = "") -> None:
        self.calls.append((to_address, amount_ton, comment))


class FakeClient:
    def __init__(self, *, eligible=True, currency="ton", status="awaiting_payment"):
        self.eligible = eligible
        self.currency = currency
        self.status = status
        self.created = []
        self.checked = []

    async def check_recipient(self, username, *, type):
        self.checked.append((username, type))
        return SimpleNamespace(
            eligible=self.eligible,
            reason=None if self.eligible else "user_not_found",
            telegram_message=None if self.eligible else "Получатель не найден",
        )

    async def create_order(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(
            order_id="supplier-1",
            status="awaiting_payment",
            payment=SimpleNamespace(
                currency=self.currency,
                pay_to_address="UQ-supplier",
                memo="supplier-1",
                amount=Decimal("1.25"),
            ),
        )

    async def get_order(self, order_id):
        return SimpleNamespace(
            order_id=order_id,
            status=self.status,
            purchase_tx="purchase-tx" if self.status == "delivered" else None,
            failure_reason="rejected" if self.status == "failed" else None,
            telegram_message="delivered!" if self.status == "delivered" else None,
        )


def run(coro):
    return asyncio.run(coro)


def test_start_order_creates_supplier_order_and_pays_ton_invoice():
    wallet = FakeWallet()
    client = FakeClient()
    events = []

    async def on_created(invoice):
        events.append(("created", invoice.order_id))
        events.append(("balance_after_create", len(wallet.calls)))

    adapter = supplier.SupplierFulfillment("key", wallet, client=client)
    invoice = run(adapter.start_order(
        order_id="local-1",
        username="@buyer",
        stars_qty=50,
        on_created=on_created,
    ))

    assert client.checked == [("buyer", "stars")]
    assert client.created[0]["idempotency_key"] == "local-1"
    assert client.created[0]["type"] == "stars"
    assert client.created[0]["quantity"] == 50
    assert client.created[0]["payment_currency"] == "ton"
    assert invoice.order_id == "supplier-1"
    assert invoice.amount_ton == 1.25
    assert wallet.calls == [("UQ-supplier", 1.25, "supplier-1")]
    # Сначала сохраняем id заказа поставщика, только потом платим.
    assert events == [("created", "supplier-1"), ("balance_after_create", 0)]


def test_start_order_refuses_ineligible_recipient_without_creating_or_paying():
    wallet = FakeWallet()
    client = FakeClient(eligible=False)
    adapter = supplier.SupplierFulfillment("key", wallet, client=client)

    try:
        run(adapter.start_order(order_id="local-1", username="buyer", stars_qty=50))
    except supplier.RecipientIneligible as exc:
        assert "не найден" in str(exc)
    else:
        raise AssertionError("ожидали RecipientIneligible")

    assert client.created == []
    assert wallet.calls == []


def test_start_order_refuses_non_ton_invoice_without_paying():
    wallet = FakeWallet()
    client = FakeClient(currency="usdt_ton")
    adapter = supplier.SupplierFulfillment("key", wallet, client=client)

    try:
        run(adapter.start_order(order_id="local-1", username="buyer", stars_qty=50))
    except supplier.UnsupportedCurrency as exc:
        assert "usdt_ton" in str(exc)
    else:
        raise AssertionError("ожидали UnsupportedCurrency")

    assert wallet.calls == []


def test_start_order_refuses_invoice_without_memo_or_address():
    for payment in (
        SimpleNamespace(currency="ton", pay_to_address="UQ-supplier", memo="", amount=Decimal("1")),
        SimpleNamespace(currency="ton", pay_to_address="", memo="memo", amount=Decimal("1")),
    ):
        wallet = FakeWallet()
        client = FakeClient()
        client.create_order = _create_with_payment(payment)
        adapter = supplier.SupplierFulfillment("key", wallet, client=client)

        try:
            run(adapter.start_order(order_id="local-1", username="buyer", stars_qty=50))
        except supplier.InvoiceUnpaid as exc:
            assert "инвойс" in str(exc).lower()
        else:
            raise AssertionError("ожидали InvoiceUnpaid")

        assert wallet.calls == []


def _create_with_payment(payment):
    async def create_order(**kwargs):
        return SimpleNamespace(order_id="supplier-1", status="awaiting_payment", payment=payment)
    return create_order


def test_map_supplier_status():
    assert supplier.map_supplier_status("delivered") == "completed"
    assert supplier.map_supplier_status("DELIVERED") == "completed"
    for status in ("failed", "reversed", "expired", "cancelled", "canceled"):
        assert supplier.map_supplier_status(status) == "needs_manual"
    for status in ("awaiting_payment", "paid", "fulfilling", "swapping", "held", "unknown"):
        assert supplier.map_supplier_status(status) == "fulfilling"


def test_get_status_returns_supplier_fields():
    client = FakeClient(status="delivered")
    adapter = supplier.SupplierFulfillment("key", FakeWallet(), client=client)

    status = run(adapter.get_status("supplier-1"))

    assert status.status == "delivered"
    assert status.purchase_tx == "purchase-tx"
    assert status.failure_reason is None


def test_next_action_does_not_repay_unknown_invoice():
    # Заказ создан, но неизвестно, ушёл ли платёж: повторно платить нельзя.
    assert supplier.next_action("awaiting_payment", invoice_paid=False) == "manual_unknown_payment"
    assert supplier.next_action("", invoice_paid=False) == "manual_unknown_payment"

    # Если поставщик уже отклонил/закрыл неоплаченный заказ — это ручной разбор,
    # а не «платёж дошёл».
    for status in ("failed", "reversed", "expired", "cancelled", "canceled"):
        assert supplier.next_action(status, invoice_paid=False) == "manual"


def test_next_action_marks_paid_when_supplier_already_saw_payment():
    assert supplier.next_action("paid", invoice_paid=False) == "mark_paid"
    assert supplier.next_action("fulfilling", invoice_paid=False) == "mark_paid"
    assert supplier.next_action("delivered", invoice_paid=False) == "mark_paid"


def test_next_action_for_paid_invoice():
    assert supplier.next_action("delivered", invoice_paid=True) == "complete"
    for status in ("failed", "reversed", "expired", "cancelled"):
        assert supplier.next_action(status, invoice_paid=True) == "manual"
    for status in ("awaiting_payment", "paid", "fulfilling", "swapping", "held"):
        assert supplier.next_action(status, invoice_paid=True) == "wait"


def test_auto_allowed_requires_live_supplier_and_real_order():
    base = dict(
        fulfillment_mode="SUPPLIER",
        api_key="key",
        auto_enabled=True,
        hot_ready=True,
        is_test=False,
        ton_amount=1.0,
        max_ton_per_order=7.0,
    )
    assert supplier.auto_allowed(**base) is True

    # Тестовый заказ никогда не должен покупать реальные звёзды.
    assert supplier.auto_allowed(**{**base, "is_test": True}) is False
    # Без ключа/режима/кошелька/выключателя — только ручной режим.
    assert supplier.auto_allowed(**{**base, "api_key": ""}) is False
    assert supplier.auto_allowed(**{**base, "fulfillment_mode": "MANUAL"}) is False
    assert supplier.auto_allowed(**{**base, "auto_enabled": False}) is False
    assert supplier.auto_allowed(**{**base, "hot_ready": False}) is False
    # Лимит на один заказ.
    assert supplier.auto_allowed(**{**base, "ton_amount": 8.0}) is False
