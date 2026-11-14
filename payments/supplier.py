"""Автовыдача Telegram Stars через API поставщика.

Схема: после подтверждения оплаты покупателем бот создаёт заказ у поставщика,
оплачивает выставленный инвойс с hot-кошелька (TON + memo) и ждёт статус
``delivered``. Нужен только API-ключ поставщика.

Модуль намеренно не импортирует aiogram/SQLAlchemy: его можно тестировать
офлайн с фейковым клиентом и фейковым кошельком.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Awaitable, Callable

import httpx

# Статусы поставщика -> локальные статусы заказа.
# Терминальные ошибки не выдаём автоматически: заказ уходит админу на разбор.
_STATUS_MAP = {
    "delivered": "completed",
    "failed": "needs_manual",
    "reversed": "needs_manual",
    "expired": "needs_manual",
    "cancelled": "needs_manual",
    "canceled": "needs_manual",
    "awaiting_payment": "fulfilling",
    "paid": "fulfilling",
    "fulfilling": "fulfilling",
    "swapping": "fulfilling",
    "held": "fulfilling",
}

_REQUEST_TIMEOUT = 20.0


class SupplierError(Exception):
    """Базовая ошибка автовыдачи."""


class RecipientIneligible(SupplierError):
    """Получатель не может принять звёзды (не найден, бот, закрытый аккаунт)."""


class UnsupportedCurrency(SupplierError):
    """Поставщик выставил инвойс в валюте, которую кошелёк бота не умеет платить."""


class InvoiceUnpaid(SupplierError):
    """Инвойс поставщика некорректен или не был оплачен."""


@dataclass(frozen=True)
class Invoice:
    order_id: str
    status: str
    currency: str
    amount_ton: float
    memo: str


@dataclass(frozen=True)
class PriceQuote:
    quantity: int
    amount_ton: float
    currency: str
    valid_until: str


@dataclass(frozen=True)
class SupplierStatus:
    order_id: str
    status: str
    purchase_tx: str | None = None
    failure_reason: str | None = None
    telegram_message: str | None = None


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Достаёт поле из dict или объекта — адаптер работает с обоими."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def map_supplier_status(status: str) -> str:
    """Переводит статус поставщика в локальный статус (completed/fulfilling/needs_manual)."""
    return _STATUS_MAP.get((status or "").lower(), "fulfilling")


def next_action(supplier_status: str, invoice_paid: bool) -> str:
    """Что делать с заказом в статусе FULFILLING при очередном опросе.

    ``manual_unknown_payment`` — самый важный случай: заказ у поставщика создан,
    но локально нет подтверждения, что инвойс оплачен. Повторно платить нельзя
    (риск двойной оплаты), поэтому такой заказ отдаётся админу.
    """
    status = (supplier_status or "").lower()
    if not invoice_paid:
        if status in ("", "awaiting_payment"):
            return "manual_unknown_payment"
        if status in ("failed", "reversed", "expired", "cancelled", "canceled"):
            return "manual"
        return "mark_paid"

    if status == "delivered":
        return "complete"
    if status in ("failed", "reversed", "expired", "cancelled", "canceled"):
        return "manual"
    return "wait"


def auto_allowed(
    *,
    fulfillment_mode: str,
    api_key: str,
    auto_enabled: bool,
    hot_ready: bool,
    is_test: bool,
    ton_amount: float,
    max_ton_per_order: float,
) -> bool:
    """Проверяет базовые условия автозакупки (без дневных/часовых лимитов БД)."""
    if is_test:
        return False
    if (fulfillment_mode or "").upper() != "SUPPLIER":
        return False
    if not api_key or not auto_enabled or not hot_ready:
        return False
    if ton_amount and ton_amount > max_ton_per_order:
        return False
    return True


class SupplierClient:
    """Минимальный HTTP-клиент API поставщика.

    Авторизация — заголовок ``X-Api-Key``. Все суммы приходят строками или
    числами и приводятся к ``Decimal`` на уровне адаптера.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        timeout: float = _REQUEST_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = (base_url or "").rstrip("/")
        self._timeout = timeout
        self._client = client

    @property
    def client(self) -> httpx.AsyncClient:
        if not self._base_url:
            raise SupplierError("не задан адрес API поставщика (SUPPLIER_API_BASE)")
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        headers = {"Accept": "application/json", "X-Api-Key": self._api_key}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = await self.client.request(
                method,
                f"{self._base_url}{path}",
                params=params,
                json=body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise SupplierError(f"сетевая ошибка: {exc}") from exc

        if response.status_code >= 400:
            raise SupplierError(f"HTTP {response.status_code}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise SupplierError("ответ поставщика не является JSON") from exc

    async def get_pricing(self, *, type: str, quantity: int, payment_currency: str) -> Any:
        return await self._request(
            "GET",
            "/pricing",
            params={
                "type": type,
                "quantity": quantity,
                "payment_currency": payment_currency,
            },
        )

    async def check_recipient(self, username: str, *, type: str) -> Any:
        return await self._request(
            "POST",
            "/recipients/check",
            body={"type": type, "recipient": {"username": username}},
        )

    async def create_order(
        self,
        *,
        type: str,
        recipient: str,
        quantity: int,
        payment_currency: str,
        callback_url: str,
        idempotency_key: str,
    ) -> Any:
        body: dict[str, Any] = {
            "type": type,
            "recipient": {"username": recipient},
            "quantity": quantity,
            "payment_currency": payment_currency,
        }
        if callback_url:
            body["callback_url"] = callback_url
        return await self._request(
            "POST", "/orders", body=body, idempotency_key=idempotency_key
        )

    async def get_order(self, order_id: str) -> Any:
        return await self._request("GET", f"/orders/{order_id}")


class SupplierFulfillment:
    """Тонкий адаптер над API поставщика + hot-кошелёк бота.

    Клиент и кошелёк можно подменить в тестах.
    """

    def __init__(
        self,
        api_key: str,
        wallet: Any,
        *,
        pay_currency: str = "ton",
        callback_url: str = "",
        base_url: str = "",
        client: Any = None,
    ) -> None:
        self._api_key = api_key
        self._wallet = wallet
        self._pay_currency = pay_currency
        self._callback_url = callback_url
        self._base_url = base_url
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = SupplierClient(self._api_key, self._base_url)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()

    async def get_quote(self, stars_qty: int) -> PriceQuote:
        """Живая цена заказа у поставщика (без создания заказа и без списания денег)."""
        quote = await self.client.get_pricing(
            type="stars",
            quantity=stars_qty,
            payment_currency=self._pay_currency,
        )
        return PriceQuote(
            quantity=stars_qty,
            amount_ton=float(_field(quote, "amount")),
            currency=(_field(quote, "currency", "") or "").lower(),
            valid_until=_field(quote, "valid_until", "") or "",
        )

    async def start_order(
        self,
        *,
        order_id: str,
        username: str,
        stars_qty: int,
        on_created: Callable[[Invoice], Awaitable[None]] | None = None,
    ) -> Invoice:
        """Создаёт заказ у поставщика и оплачивает инвойс с hot-кошелька.

        ``on_created`` вызывается *до* перевода денег — бот сохраняет id заказа
        поставщика, чтобы после падения не создать/не оплатить заказ дважды.
        Идемпотентность у поставщика задаётся ключом, равным локальному id заказа.
        """
        recipient = (username or "").strip().lstrip("@")
        check = await self.client.check_recipient(recipient, type="stars")
        if not _field(check, "eligible", False):
            reason = (
                _field(check, "telegram_message")
                or _field(check, "reason")
                or "получатель не может принять звёзды"
            )
            raise RecipientIneligible(reason)

        created = await self.client.create_order(
            type="stars",
            recipient=recipient,
            quantity=stars_qty,
            payment_currency=self._pay_currency,
            callback_url=self._callback_url or "",
            idempotency_key=str(order_id),
        )

        payment = _field(created, "payment")
        if payment is None:
            raise InvoiceUnpaid("поставщик не вернул инвойс")

        currency = (_field(payment, "currency", "") or "").lower()
        if currency != "ton":
            raise UnsupportedCurrency(
                f"валюта инвойса {currency or '?'} не поддерживается (нужен ton)"
            )

        address = _field(payment, "pay_to_address")
        memo = _field(payment, "memo")
        amount = _field(payment, "amount")
        if not address or not memo or amount is None or Decimal(amount) <= 0:
            raise InvoiceUnpaid("инвойс поставщика без адреса, memo или суммы")

        invoice = Invoice(
            order_id=_field(created, "order_id"),
            status=_field(created, "status"),
            currency=currency,
            amount_ton=float(amount),
            memo=str(memo),
        )

        if on_created is not None:
            await on_created(invoice)

        await self._wallet.transfer(address, invoice.amount_ton, comment=invoice.memo)
        return invoice

    async def get_status(self, order_id: str) -> SupplierStatus:
        order = await self.client.get_order(order_id)
        return SupplierStatus(
            order_id=_field(order, "order_id"),
            status=_field(order, "status"),
            purchase_tx=_field(order, "purchase_tx"),
            failure_reason=_field(order, "failure_reason"),
            telegram_message=_field(order, "telegram_message"),
        )
