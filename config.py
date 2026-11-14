import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    admin_chat_id: int = int(os.getenv("ADMIN_CHAT_ID", "0"))
    # Куда падают уведомления о заказах. Если не задан отдельно — используется admin_chat_id
    # (личка с ботом). Для отдельной группы/канала укажи ORDERS_CHAT_ID в .env.
    orders_chat_id: int = int(os.getenv("ORDERS_CHAT_ID", "0")) or int(os.getenv("ADMIN_CHAT_ID", "0"))

    star_cost_rub: float = float(os.getenv("STAR_COST_RUB", "1.35"))
    margin_percent: float = float(os.getenv("MARGIN_PERCENT", "15"))

    # TEST — без реальных платежей, кнопка "Тестовая оплата" сразу помечает заказ оплаченным.
    # LIVE — настоящая оплата: покупатель переводит TON прямо на TON_WALLET_ADDRESS ниже.
    payment_mode: str = os.getenv("PAYMENT_MODE", "TEST")

    # Кошелёк, куда покупатели переводят TON напрямую (нужен для PAYMENT_MODE=LIVE).
    ton_wallet_address: str = os.getenv("TON_WALLET_ADDRESS", "")
    toncenter_api_key: str = os.getenv("TONCENTER_API_KEY", "")
    # true — проверяем платежи в тестовой сети TON (тестовые монеты через @testgiver_ton_bot),
    # false — обычная сеть TON, настоящие деньги.
    ton_testnet: bool = os.getenv("TON_TESTNET", "false").lower() in ("1", "true", "yes")

    fragment_mode: str = os.getenv("FRAGMENT_MODE", "STUB")

    # --- Выдача звёзд ---
    # SUPPLIER — автовыдача через поставщик FaaS (нужен SUPPLIER_API_KEY и hot-кошелёк).
    # MANUAL  — ничего не покупаем автоматически, заказ уходит админу на /done.
    fulfillment_mode: str = os.getenv("FULFILLMENT_MODE", "SUPPLIER").upper()
    supplier_api_key: str = os.getenv("SUPPLIER_API_KEY", "")
    # Базовый адрес API поставщика, включая версию (например, https://api.example.com/v1).
    supplier_api_base: str = os.getenv("SUPPLIER_API_BASE", "").rstrip("/")
    # Бот умеет платить native TON. USDT-jetton пока не поддержан.
    supplier_pay_currency: str = os.getenv("SUPPLIER_PAY_CURRENCY", "ton").lower()
    # Необязательный HTTPS-webhook поставщика; для polling оставляем пустым.
    supplier_callback_url: str = os.getenv("SUPPLIER_CALLBACK_URL", "")
    # Насколько старые заказы без id поставщика можно автоматически дозапускать
    # после рестарта. Старые ручные заказы трогать нельзя — их могли уже выдать.
    supplier_resume_max_age_hours: float = float(
        os.getenv("SUPPLIER_RESUME_MAX_AGE_HOURS", "6")
    )

    # --- Кошельки (схема "рабочий + основной") ---
    # Рабочий (hot) кошелёк: сюда падают платежи покупателей, с него же бот покупает
    # звёзды на Fragment. Это и есть TON_WALLET_ADDRESS выше.
    # Основной (cold) кошелёк: сюда уходит вся прибыль по sweep-логике.
    # Seed от cold кошелька НИКОГДА не должен лежать на сервере.
    cold_wallet_address: str = os.getenv("COLD_WALLET_ADDRESS", "")
    # Seed-фраза (12 слов Tonkeeper или 24 слова TON через пробел) рабочего кошелька.
    # Нужна для авто-подписи переводов (sweep + оплата инвойсов поставщик).
    # Хранится только в .env (уже в .gitignore).
    hot_wallet_mnemonic: str = os.getenv("HOT_WALLET_MNEMONIC", "")
    # Сколько TON постоянно оставлять на рабочем кошельке (буфер на комиссии/газ).
    hot_keep_ton: float = float(os.getenv("HOT_KEEP_TON", "1.0"))
    # Включён ли авто-sweep прибыли на основной кошелёк (false — просто копим на hot).
    sweep_enabled: bool = os.getenv("SWEEP_ENABLED", "true").lower() in ("1", "true", "yes")

    # --- Лимиты безопасности автозакупки Fragment ---
    # Заказы с суммой в TON не больше этого значения покупаются автоматически,
    # всё дороже — уходит тебе на ручное подтверждение (режим STUB).
    auto_max_ton_per_order: float = float(os.getenv("AUTO_MAX_TON_PER_ORDER", "7"))
    # Потолок суммарных авто-трат в TON за сутки (0 = без ограничения). Страховка
    # от слива кошелька при компрометации: больше этого автомат тратить не будет.
    auto_daily_spend_ton: float = float(os.getenv("AUTO_DAILY_SPEND_TON", "0"))
    # Максимум автозакупок в час (анти-флуд / человеческий pacing).
    auto_max_orders_per_hour: int = int(os.getenv("AUTO_MAX_ORDERS_PER_HOUR", "10"))
    # Общий выключатель автозакупки (можно рубить и через .env, и через /auto off).
    auto_enabled: bool = os.getenv("AUTO_ENABLED", "true").lower() in ("1", "true", "yes")

    # --- Автоматическая цена от живой закупки поставщик ---
    # Раз в N минут бот смотрит цену 50 ⭐ у поставщик и курс TON/RUB, после чего
    # пересчитывает себестоимость и цену продажи. Так фиксированная цена не
    # устаревает и не уходит в минус при движении курса.
    auto_price_enabled: bool = os.getenv("AUTO_PRICE_ENABLED", "true").lower() in (
        "1", "true", "yes"
    )
    auto_price_interval_minutes: float = float(
        os.getenv("AUTO_PRICE_INTERVAL_MINUTES", "60")
    )
    auto_price_quantity: int = int(os.getenv("AUTO_PRICE_QUANTITY", "50"))
    # Желаемая маржа поверх закупки (включая прибыль).
    auto_price_margin_percent: float = float(
        os.getenv("AUTO_PRICE_MARGIN_PERCENT", "22")
    )
    # Дополнительный буфер на движение курса между заказом и покупкой.
    auto_price_buffer_percent: float = float(
        os.getenv("AUTO_PRICE_BUFFER_PERCENT", "5")
    )
    # Минимальная прибыль с одной звезды после комиссий.
    auto_price_min_profit_rub: float = float(
        os.getenv("AUTO_PRICE_MIN_PROFIT_RUB", "0.10")
    )
    # Оценка комиссий сети на заказ (оплата инвойса + sweep), в TON.
    auto_price_fee_ton: float = float(os.getenv("AUTO_PRICE_FEE_TON", "0.02"))
    # Защита от битого ответа API: не применять изменение цены больше чем на N%.
    auto_price_max_change_percent: float = float(
        os.getenv("AUTO_PRICE_MAX_CHANGE_PERCENT", "40")
    )

    database_url: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///stars_bot.db")

    # Прокси для доступа к api.telegram.org (актуально при блокировках в РФ)
    # Формат: socks5://user:pass@host:port  или  http://user:pass@host:port
    proxy_url: str = os.getenv("PROXY_URL", "")

    min_stars: int = 50

    @property
    def hot_mnemonic_list(self) -> list[str]:
        """Seed-фраза рабочего кошелька в виде списка слов (для pytoniq)."""
        return [w for w in self.hot_wallet_mnemonic.split() if w]

    @property
    def hot_ready(self) -> bool:
        """Готов ли авто-режим: есть seed, адрес hot и (если включён sweep) адрес cold."""
        if not self.hot_mnemonic_list or not self.ton_wallet_address:
            return False
        if self.sweep_enabled and not self.cold_wallet_address:
            return False
        return True


cfg = Config()
