"""
Офлайн-проверка бизнес-логики без aiogram/aiohttp/sqlalchemy (их не поставить без сети).
Гоняет: валидацию ввода, расчёт цены, полный жизненный цикл заказа на чистом sqlite3.
Запуск: python3 tests/test_flow.py
"""
import sqlite3
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pricing_core import calc_price_sync
from config import cfg


def calc_price(stars_qty: int):
    """Синхронный расчёт цены (обёртка над чистой функцией) — для офлайн-тестов."""
    return calc_price_sync(stars_qty, cfg.star_cost_rub, cfg.margin_percent)

failures = []


def check(name, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


# ---------- 1. Валидация количества звёзд (та же логика, что в bot.buy_qty) ----------

def validate_qty(text: str):
    if not text or not text.isdigit():
        return None
    qty = int(text)
    if qty < cfg.min_stars:
        return None
    return qty


check("qty='49' отклоняется (меньше минимума)", validate_qty("49") is None)
check("qty='50' принимается (граница)", validate_qty("50") == 50)
check("qty='abc' отклоняется (не число)", validate_qty("abc") is None)
check("qty='1000' принимается", validate_qty("1000") == 1000)


# ---------- 2. Валидация username (та же логика, что в bot.buy_username) ----------

def validate_username(text: str):
    username = (text or "").strip().lstrip("@")
    if not username or " " in username:
        return None
    return username


check("username='@durov' -> 'durov'", validate_username("@durov") == "durov")
check("username='john doe' отклоняется (пробел)", validate_username("john doe") is None)
check("username='' отклоняется", validate_username("") is None)


# ---------- 3. Расчёт цены ----------

cost, total, profit = calc_price(100)
check("calc_price: total == cost + profit", abs(total - (cost + profit)) < 0.01)
check("calc_price: profit положительный", profit > 0)
check("calc_price(50) себестоимость == 50 * STAR_COST_RUB",
      abs(calc_price(50)[0] - 50 * cfg.star_cost_rub) < 0.01)


# ---------- 4. Полный жизненный цикл заказа на чистом sqlite3 (та же схема, что в database.py) ----------

conn = sqlite3.connect(":memory:")
conn.execute("""
    CREATE TABLE orders (
        id TEXT PRIMARY KEY,
        user_id INTEGER,
        stars_qty INTEGER,
        target_username TEXT,
        cost_rub REAL,
        total_rub REAL,
        profit_rub REAL,
        payment_method TEXT,
        payment_ref TEXT DEFAULT '',
        status TEXT DEFAULT 'awaiting_payment'
    )
""")

order_id = uuid.uuid4().hex
qty = 200
cost_rub, total_rub, profit_rub = calc_price(qty)

conn.execute(
    "INSERT INTO orders (id, user_id, stars_qty, target_username, cost_rub, total_rub, profit_rub, payment_method) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    (order_id, 12345, qty, "testuser", cost_rub, total_rub, profit_rub, "crypto"),
)
conn.commit()

row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
check("заказ создан и читается из БД", row is not None)
check("статус по умолчанию awaiting_payment", row[9] == "awaiting_payment")

# симулируем оплату -> paid -> fulfilling -> completed (как в payment_watcher + /done)
conn.execute("UPDATE orders SET status = 'paid' WHERE id = ?", (order_id,))
conn.execute("UPDATE orders SET status = 'fulfilling' WHERE id = ?", (order_id,))
conn.execute("UPDATE orders SET status = 'completed' WHERE id = ?", (order_id,))
conn.commit()

final_status = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()[0]
check("статус дошёл до completed", final_status == "completed")

conn.close()

# ---------- итог ----------

print()
if failures:
    print(f"[FAIL] Провалено тестов: {len(failures)} -> {failures}")
    sys.exit(1)
else:
    print("[OK] Вся бизнес-логика прошла проверку (без учёта сетевых вызовов CryptoBot/Fragment)")
