import enum
import time
import uuid

from sqlalchemy import String, Integer, Float, BigInteger, Boolean, Enum, DateTime, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from config import cfg


class Base(DeclarativeBase):
    pass


class PaymentMethod(str, enum.Enum):
    CRYPTO = "crypto"
    RUB = "rub"


class OrderStatus(str, enum.Enum):
    AWAITING_PAYMENT = "awaiting_payment"   # ждём оплату от покупателя
    PAID = "paid"                           # оплата подтверждена, ждём покупки на Fragment
    FULFILLING = "fulfilling"               # звёзды покупаются на Fragment
    COMPLETED = "completed"                 # звёзды доставлены
    FAILED = "failed"                       # что-то пошло не так (возврат)
    EXPIRED = "expired"                     # не оплатили вовремя
    NEEDS_MANUAL = "needs_manual"           # автозакупка не прошла/недопустима — нужен ручной разбор


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)

    stars_qty: Mapped[int] = mapped_column(Integer)
    target_username: Mapped[str] = mapped_column(String)

    cost_rub: Mapped[float] = mapped_column(Float)     # себестоимость (пойдёт на Fragment)
    total_rub: Mapped[float] = mapped_column(Float)     # что платит покупатель (себестоимость + маржа)
    profit_rub: Mapped[float] = mapped_column(Float)     # total - cost, остаётся на счету

    payment_method: Mapped[PaymentMethod] = mapped_column(Enum(PaymentMethod))
    payment_ref: Mapped[str] = mapped_column(String, default="")  # memo для TON-перевода (= id заказа)
    ton_amount: Mapped[float] = mapped_column(Float, default=0.0)  # сумма в TON, зафиксированная при создании счёта

    # Поля автозакупки Fragment:
    # fulfill_ref — идентификатор выполненной покупки (tx hash / id у реселлера): нужен
    #   для идемпотентности, чтобы после краша/рестарта не купить звёзды дважды.
    fulfill_ref: Mapped[str] = mapped_column(String, default="")
    # fragment_cost_ton — сколько TON фактически списано на покупку звёзд у поставщика.
    fragment_cost_ton: Mapped[float] = mapped_column(Float, default=0.0)
    # supplier_invoice_paid — подтверждён ли исходящий платёж инвойса поставщику (поставщик).
    # Нужен для безопасного рестарта: без него нельзя понять, ушёл ли уже TON.
    supplier_invoice_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    # swept — ушла ли прибыль по этому заказу на основной (cold) кошелёк.
    swept: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.AWAITING_PAYMENT)

    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Setting(Base):
    """Редактируемые через команды бота настройки (цена, маржа), переживают перезапуск."""
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String)


class User(Base):
    """Все, кто хоть раз писал /start — нужно для рассылок."""
    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String, default="")
    first_seen: Mapped[float] = mapped_column(Float, default=time.time)


class SupportLink(Base):
    """
    Связь между сообщением, пересланным в чат поддержки, и пользователем,
    который его написал — нужна, чтобы понять, кому отправить ответ,
    когда админ нажимает "Reply" на пересланное сообщение.
    """
    __tablename__ = "support_links"

    admin_message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


async def get_setting(session: AsyncSession, key: str, default: str) -> str:
    row = await session.get(Setting, key)
    return row.value if row else default


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(Setting, key)
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))
    await session.commit()


async def track_user(session: AsyncSession, user_id: int, username: str = "") -> None:
    """Запоминает пользователя (для рассылок), если его ещё нет в базе."""
    row = await session.get(User, user_id)
    if row:
        if username and row.username != username:
            row.username = username
            await session.commit()
    else:
        session.add(User(user_id=user_id, username=username or ""))
        await session.commit()


async def link_support_message(session: AsyncSession, admin_message_id: int, user_id: int) -> None:
    session.add(SupportLink(admin_message_id=admin_message_id, user_id=user_id))
    await session.commit()


async def get_support_user(session: AsyncSession, admin_message_id: int) -> int | None:
    row = await session.get(SupportLink, admin_message_id)
    return row.user_id if row else None


def _normalize_db_url(url: str) -> tuple[str, dict]:
    """
    Приводит DATABASE_URL к виду, который понимает SQLAlchemy + asyncpg,
    и достаёт настройки SSL отдельно (нужно для Neon и подобных облачных Postgres —
    они требуют SSL, но передают его через query-параметр sslmode, который
    asyncpg не понимает напрямую).
    """
    connect_args: dict = {}

    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]

    if url.startswith("postgresql+asyncpg://") and "?" in url:
        base, _, query = url.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        params.pop("sslmode", None)
        params.pop("channel_binding", None)
        url = base
        connect_args["ssl"] = True
        # Neon (и другие облачные Postgres) часто дают соединение через пулер (PgBouncer) —
        # с ним asyncpg-кэш подготовленных запросов ломается, отключаем его.
        connect_args["statement_cache_size"] = 0
    elif url.startswith("postgresql+asyncpg://"):
        connect_args["ssl"] = True
        connect_args["statement_cache_size"] = 0

    return url, connect_args


_db_url, _connect_args = _normalize_db_url(cfg.database_url)
engine = create_async_engine(_db_url, connect_args=_connect_args)
Session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _migrate()


async def _migrate() -> None:
    """
    Лёгкая миграция схемы под новые колонки автозакупки (fulfill_ref, fragment_cost_ton,
    swept) и новый статус NEEDS_MANUAL. create_all умеет создавать таблицы, но НЕ меняет
    уже существующие — поэтому для уже живой базы добавляем недостающее здесь.

    Postgres (Neon) хранит статусы как native enum ('orderstatus'), поэтому новое значение
    добавляем через ALTER TYPE; колонки — через ADD COLUMN IF NOT EXISTS.
    SQLite не умеет ADD COLUMN IF NOT EXISTS — проверяем существующие колонки через inspector.
    """
    async with engine.connect() as conn:
        dialect = engine.dialect.name
        if dialect == "postgresql":
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'NEEDS_MANUAL'"))
            await conn.execute(text(
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS fulfill_ref VARCHAR NOT NULL DEFAULT ''"
            ))
            await conn.execute(text(
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS fragment_cost_ton DOUBLE PRECISION NOT NULL DEFAULT 0"
            ))
            await conn.execute(text(
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS supplier_invoice_paid BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            await conn.execute(text(
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS swept BOOLEAN NOT NULL DEFAULT FALSE"
            ))
        else:
            await conn.run_sync(_migrate_sqlite)


def _migrate_sqlite(sync_conn) -> None:
    from sqlalchemy import inspect

    insp = inspect(sync_conn)
    if "orders" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("orders")}
    adds = {
        "fulfill_ref": "VARCHAR NOT NULL DEFAULT ''",
        "fragment_cost_ton": "FLOAT NOT NULL DEFAULT 0",
        "supplier_invoice_paid": "BOOLEAN NOT NULL DEFAULT 0",
        "swept": "BOOLEAN NOT NULL DEFAULT 0",
    }
    for name, ddl in adds.items():
        if name not in existing:
            sync_conn.execute(text(f"ALTER TABLE orders ADD COLUMN {name} {ddl}"))
    sync_conn.commit()


async def create_order(
    session: AsyncSession,
    user_id: int,
    stars_qty: int,
    target_username: str,
    cost_rub: float,
    total_rub: float,
    payment_method: PaymentMethod,
) -> Order:
    order = Order(
        user_id=user_id,
        stars_qty=stars_qty,
        target_username=target_username,
        cost_rub=cost_rub,
        total_rub=total_rub,
        profit_rub=total_rub - cost_rub,
        payment_method=payment_method,
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return order


async def get_all_user_ids(session: AsyncSession) -> list[int]:
    """Все известные пользователи бота — для рассылки."""
    from sqlalchemy import select
    result = await session.execute(select(User.user_id))
    return [row[0] for row in result.all()]
