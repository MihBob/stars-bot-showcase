"""
Работа с TON-кошельком рабочего (hot) адреса.

Ключевая идея безопасности: на hot-кошельке (TON_WALLET_ADDRESS) деньги не копятся.
Сюда падают платежи покупателей -> с него же бот покупает звёзды на Fragment ->
прибыль (всё, что выше буфера HOT_KEEP_TON) сразу уходит на cold (COLD_WALLET_ADDRESS).

Чтение входящих переводов: через LiteBalancer (pytoniq) — надёжнее и без rate-limit'ов
TonCenter. Подпись исходящих (sweep, переводы): тоже через pytoniq (WalletV4R2).

Seed hot-кошелька лежит только в .env (HOT_WALLET_MNEMONIC) и НИКОГДА не логируется.
Поддержаны 12 слов (multichain/BIP39, новый Tonkeeper) и 24 слова (классическая
TON-фраза). Версия контракта (v3r2/v4r2/v5r1) определяется по совпадению адреса
из .env, чтобы подписывать переводы именно тем ключом, который ожидает кошелёк.
"""
import hashlib
import hmac
import logging
import time
from typing import Optional

import aiohttp
from pytoniq import LiteBalancer, WalletV3R2, WalletV4R2, WalletV5R1
from pytoniq_core import Address, StateInit, begin_cell
from pytoniq_core.crypto.keys import mnemonic_to_private_key

from config import cfg

_client: Optional[LiteBalancer] = None
_wallet = None


async def get_client() -> LiteBalancer:
    """Ленивый синглтон LiteBalancer (пул liteserver'ов mainnet/testnet)."""
    global _client
    if _client is None:
        if cfg.ton_testnet:
            _client = LiteBalancer.from_testnet_config(trust_level=1)
        else:
            _client = LiteBalancer.from_mainnet_config(trust_level=2)
        await _client.start_up()
    return _client


def _bip39_seed(words: list[str], passphrase: str = "") -> bytes:
    """BIP39 seed из 12 слов (multichain-фраза Tonkeeper, без checksum-проверки)."""
    phrase = " ".join(w.lower() for w in words)
    return hashlib.pbkdf2_hmac(
        "sha512",
        phrase.encode("utf-8"),
        ("mnemonic" + passphrase).encode("utf-8"),
        2048,
    )


def _slip10_ed25519_derive(seed: bytes, path: tuple[int, ...]) -> bytes:
    """SLIP-0010 Ed25519: m/44'/607'/0' и т.п. Возвращает 32-байтовый seed ключа."""
    digest = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    key, chain = digest[:32], digest[32:]
    for index in path:
        data = b"\x00" + key + index.to_bytes(4, "big")
        digest = hmac.new(chain, data, hashlib.sha512).digest()
        key, chain = digest[:32], digest[32:]
    return key


def _private_key_from_mnemonic(words: list[str]) -> bytes:
    """
    Приватный ключ (64 байта) из seed-фразы.

    - 24 слова — классическая TON-фраза (Tonkeeper старого формата, PBKDF2
      с солью "TON default seed");
    - 12 слов — multichain/BIP39-фраза (новый Tonkeeper: BIP39 ->
      m/44'/607'/0' по SLIP-0010).
    """
    count = len(words)
    if count == 24:
        _, private_key = mnemonic_to_private_key(words)
        return private_key
    if count == 12:
        seed = _bip39_seed(words)
        path = (44 | 0x80000000, 607 | 0x80000000, 0 | 0x80000000)
        key_seed = _slip10_ed25519_derive(seed, path)
        # pytoniq хранит приватный ключ как seed+public (64 байта).
        from pytoniq_core.crypto.keys import crypto_sign_seed_keypair

        _, private_key = crypto_sign_seed_keypair(key_seed)
        return private_key
    raise RuntimeError(
        f"в HOT_WALLET_MNEMONIC {count} слов, а нужно 12 (Tonkeeper multichain) "
        "или 24 (классическая TON-фраза)"
    )


def _wallet_candidates(private_key: bytes) -> list[dict]:
    """
    Адреса, которые может иметь этот ключ: v3r2 / v4r2 / v5r1.

    Адрес TON зависит не только от ключа, но и от версии контракта кошелька.
    Новый Tonkeeper создаёт v5r1, старый — v4r2. Сравниваем с адресом из .env
    и выбираем совпавшую версию, иначе можно подписать перевод «чужим» ключом.
    """
    from pytoniq.contract.wallets.wallet import (
        WALLET_V3_R2_CODE,
        WALLET_V4_R2_CODE,
    )
    from pytoniq.contract.wallets.wallet_v5 import WALLET_V5_R1_CODE
    from pytoniq_core.crypto.keys import private_key_to_public_key

    public_key = private_key_to_public_key(private_key)
    network_id = -3 if cfg.ton_testnet else -239

    specs = [
        (
            "v3r2",
            WALLET_V3_R2_CODE,
            WalletV3R2.create_data_cell(public_key, wc=0),
        ),
        (
            "v4r2",
            WALLET_V4_R2_CODE,
            WalletV4R2.create_data_cell(public_key, wc=0),
        ),
        (
            "v5r1",
            WALLET_V5_R1_CODE,
            WalletV5R1.create_data_cell(
                public_key, wc=0, network_global_id=network_id
            ),
        ),
    ]

    result = []
    for version, code, data in specs:
        state_init = StateInit(code=code, data=data)
        address = Address((0, state_init.serialize().hash))
        result.append({"version": version, "address": address})
    return result


async def _build_wallet(version: str, private_key: bytes):
    client = await get_client()
    if version == "v5r1":
        return await WalletV5R1.from_private_key(
            client,
            private_key,
            network_global_id=-3 if cfg.ton_testnet else -239,
        )
    if version == "v4r2":
        return await WalletV4R2.from_private_key(client, private_key, version="v4r2")
    return await WalletV3R2.from_private_key(client, private_key, version="v3r2")


async def get_wallet():
    """Ленивый синглтон hot-кошелька из seed-фразы в .env."""
    global _wallet
    if _wallet is None:
        if not cfg.hot_mnemonic_list:
            raise RuntimeError("HOT_WALLET_MNEMONIC не задан в .env")
        if not cfg.ton_wallet_address:
            raise RuntimeError("TON_WALLET_ADDRESS не задан в .env")

        private_key = _private_key_from_mnemonic(cfg.hot_mnemonic_list)
        candidates = _wallet_candidates(private_key)
        target = Address(cfg.ton_wallet_address)

        matched = next(
            (c for c in candidates if c["address"] == target),
            None,
        )
        if matched is None:
            variants = ", ".join(
                f"{c['version']}={c['address'].to_str()}" for c in candidates
            )
            raise RuntimeError(
                "seed не совпадает с TON_WALLET_ADDRESS ни для одной версии "
                f"кошелька. Проверь seed/адрес. Кандидаты: {variants}"
            )

        _wallet = await _build_wallet(matched["version"], private_key)
        logging.info(
            "[ton_wallet] hot-кошелёк: %s (%s)",
            _wallet.address.to_str(),
            matched["version"],
        )
    return _wallet


async def close() -> None:
    """Корректное закрытие liteserver-соединений (на shutdown)."""
    global _client, _wallet
    if _client is not None:
        try:
            await _client.close_all()
        except Exception:
            pass
    _client = None
    _wallet = None


def _comment_body(text: str):
    """Собираем body сообщения с текстовым комментарием (op=0, snake-string)."""
    return begin_cell().store_uint(0, 32).store_string(text).end_cell()


def _parse_comment(body) -> str:
    """Достаём текст из тела входящего сообщения (безопасно, любые ошибки -> '')."""
    try:
        s = body.begin_parse()
        if s.remaining_bits < 32:
            return ""
        if s.load_uint(32) != 0:  # это не текстовый комментарий
            return ""
        return s.load_snake_string()
    except Exception:
        return ""


_RATE_CACHE: Optional[tuple[float, float, str]] = None  # (rate, fetched_at, source)
_RATE_TTL = 60.0          # свежий курс держим минуту
_RATE_STALE_TTL = 900.0   # при падении источников можно взять курс не старше 15 минут


def _plausible_rate(rate: float) -> bool:
    """Отсекаем явно битые ответы API (0, отрицательные, фантастические значения)."""
    return 10.0 <= rate <= 1000.0


async def _rate_from_tonapi(session: aiohttp.ClientSession) -> float:
    async with session.get(
        "https://tonapi.io/v2/rates",
        params={"tokens": "ton", "currencies": "rub"},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return float(data["rates"]["TON"]["prices"]["RUB"])


async def _rate_from_coingecko(session: aiohttp.ClientSession) -> float:
    async with session.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": "the-open-network", "vs_currencies": "rub"},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return float(data["the-open-network"]["rub"])


async def get_ton_rub_rate(force: bool = False) -> float:
    """
    Курс TON/RUB из независимых источников.

    Раньше здесь был Binance TONUSDT + USD/RUB, но Binance отдаёт по этому
    символу цену, которая расходится с рынком TON в разы (например $1.60
    против реальных $1.17). Из-за этого бот занижал сумму к оплате и уходил
    в минус. Теперь основной источник — TonAPI (сразу RUB), резервный —
    CoinGecko. При недоступности обоих используем последний свежий курс,
    но не старше 15 минут; иначе честно падаем, чтобы не продать в убыток.
    """
    global _RATE_CACHE

    now = time.time()
    if not force and _RATE_CACHE and now - _RATE_CACHE[1] < _RATE_TTL:
        return _RATE_CACHE[0]

    errors: list[str] = []
    async with aiohttp.ClientSession() as session:
        for name, fetcher in (
            ("tonapi", _rate_from_tonapi),
            ("coingecko", _rate_from_coingecko),
        ):
            try:
                rate = await fetcher(session)
                if not _plausible_rate(rate):
                    raise ValueError(f"подозрительный курс: {rate}")
                _RATE_CACHE = (rate, now, name)
                logging.info("[ton_wallet] курс TON/RUB: %.2f (%s)", rate, name)
                return rate
            except Exception as e:
                errors.append(f"{name}: {type(e).__name__}: {e}")

    if _RATE_CACHE and now - _RATE_CACHE[1] < _RATE_STALE_TTL:
        rate, fetched_at, source = _RATE_CACHE
        logging.warning(
            "[ton_wallet] все источники курса недоступны, беру последний курс "
            "%.2f (%s, %.0f сек назад): %s",
            rate,
            source,
            now - fetched_at,
            "; ".join(errors),
        )
        return rate

    raise RuntimeError("не удалось получить курс TON/RUB: " + "; ".join(errors))


async def create_payment_request(total_rub: float, order_id: str) -> dict:
    """
    Готовит данные для оплаты: сколько TON нужно перевести (курс фиксируется
    на момент создания), на какой адрес и с каким комментарием (memo = order_id).
    """
    if not cfg.ton_wallet_address:
        raise RuntimeError("TON_WALLET_ADDRESS не задан в .env")

    rate = await get_ton_rub_rate()
    ton_amount = round(total_rub / rate, 4)
    return {
        "ton_amount": ton_amount,
        "rate": rate,
        "address": cfg.ton_wallet_address,
        "memo": order_id,
    }


async def get_balance_ton() -> float:
    """Баланс hot-кошелька в TON."""
    wallet = await get_wallet()
    bal = await wallet.get_balance()  # nano
    return bal / 1e9


async def find_incoming_payment(memo: str, min_amount_ton: float) -> bool:
    """
    Ищет во входящих переводах hot-кошелька транзакцию с нужным комментарием и суммой.
    Comment matching работает для обычных (не encrypted) комментариев — большинство
    кошельков (Tonkeeper и т.п.) шлют обычный текстовый комментарий.
    """
    if not cfg.ton_wallet_address:
        return False

    try:
        client = await get_client()
        txs = await client.get_transactions(address=cfg.ton_wallet_address, count=30)
    except Exception as e:
        logging.error(f"[ton_wallet] get_transactions упал: {e}")
        return False

    min_nano = int(min_amount_ton * 1e9)
    for tx in txs:
        in_msg = getattr(tx, "in_msg", None)
        if in_msg is None or not getattr(in_msg, "is_internal", False):
            continue
        amount_nano = int(getattr(in_msg.info, "value_coins", 0) or 0)
        comment = _parse_comment(in_msg.body)
        if comment == memo and amount_nano >= int(min_nano * 0.99):  # 1% допуск на курс
            return True
    return False


async def _send_transfer(wallet, to_address: str, amount_nano: int, body) -> None:
    """
    Отправляет перевод с hot-кошелька.

    Важный нюанс pytoniq: обычный wallet.transfer() не прикладывает state_init,
    поэтому НЕдеплоенный кошелёк не может отправить первый исходящий перевод —
    liteserver просто отклоняет внешнее сообщение. Если аккаунт ещё не
    инициализирован, собираем сообщение вручную и прикладываем state_init.
    """
    if hasattr(wallet, "update"):
        try:
            await wallet.update()
        except Exception:
            logging.exception(
                "[ton_wallet] не удалось обновить состояние кошелька, "
                "использую последнее известное"
            )

    if not getattr(wallet, "is_uninitialized", False):
        await wallet.transfer(to_address, amount_nano, body=body)
        return

    logging.info("[ton_wallet] первый перевод: деплою hot-кошелёк state_init")
    destination = Address(to_address) if isinstance(to_address, str) else to_address
    message = wallet.create_wallet_internal_message(
        destination=destination,
        value=amount_nano,
        body=body,
    )
    # У недеплоенного аккаунта get-метод seqno возвращает exit code -256,
    # поэтому берём seqno из данных контракта (для первого перевода это 0).
    seqno = int(getattr(wallet, "seqno", 0) or 0)
    transfer_msg = wallet.raw_create_transfer_msg(
        private_key=wallet.private_key,
        seqno=seqno,
        wallet_id=wallet.wallet_id,
        messages=[message],
    )
    await wallet.send_external(body=transfer_msg, state_init=wallet.state_init)


async def transfer(to_address: str, amount_ton: float, comment: str = "") -> None:
    """Отправляет TON с hot-кошелька на to_address (подпись — pytoniq)."""
    wallet = await get_wallet()
    nano = int(round(amount_ton * 1e9))
    body = _comment_body(comment) if comment else begin_cell().end_cell()
    await _send_transfer(wallet, to_address, nano, body)


def sweep_amount(
    *,
    balance: float,
    keep_ton: float,
    gas_reserve: float = 0.05,
    reserve_ton: float = 0.0,
) -> float:
    """
    Сколько TON можно безопасно отправить на cold (чистая функция, без сети).

    Из баланса вычитаются:
    - keep_ton — рабочий буфер hot-кошелька (HOT_KEEP_TON);
    - gas_reserve — запас на комиссию самого перевода;
    - reserve_ton — сумма ещё не оплаченных инвойсов поставщика: эти деньги
      уже принадлежат поставщику, отправлять их на cold нельзя.
    """
    to_send = balance - keep_ton - gas_reserve - reserve_ton
    if to_send <= 0:
        return 0.0
    return round(to_send, 6)


async def sweep_profit(reserve_ton: float = 0.0) -> float:
    """
    Переводит весь «лишний» баланс hot-кошелька на cold, оставляя буфер HOT_KEEP_TON
    (+ запас на газ и резерв незавершённых инвойсов). Возвращает, сколько TON ушло.
    """
    if not cfg.cold_wallet_address:
        logging.warning("[ton_wallet] COLD_WALLET_ADDRESS не задан — sweep пропущен")
        return 0.0

    balance = await get_balance_ton()
    to_send = sweep_amount(
        balance=balance,
        keep_ton=cfg.hot_keep_ton,
        gas_reserve=0.05,  # запас на комиссию перевода
        reserve_ton=reserve_ton,
    )
    if to_send <= 0:
        return 0.0

    await transfer(cfg.cold_wallet_address, to_send, comment="sweep")
    logging.info(
        "[ton_wallet] sweep: %.6f TON -> cold (%s), резерв инвойсов %.6f TON",
        to_send,
        cfg.cold_wallet_address,
        reserve_ton,
    )
    return to_send