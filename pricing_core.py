"""
Чистая функция расчёта цены — без зависимости от БД/SQLAlchemy.
Вынесена отдельно, чтобы её можно было использовать и тестировать
без необходимости поднимать базу данных.
"""


def calc_price_sync(stars_qty: int, star_cost: float, margin: float) -> tuple[float, float, float]:
    """
    Возвращает (cost_rub, total_rub, profit_rub)

    cost_rub   — сколько реально стоит купить эти звёзды на Fragment
    total_rub  — сколько платит покупатель
    profit_rub — total - cost, остаётся у тебя
    """
    cost_rub = round(stars_qty * star_cost, 2)
    total_rub = round(cost_rub * (1 + margin / 100), 2)
    profit_rub = round(total_rub - cost_rub, 2)
    return cost_rub, total_rub, profit_rub


def auto_price_sync(
    *,
    stars_qty: int,
    quote_ton: float,
    ton_rub: float,
    margin_percent: float,
    buffer_percent: float = 0.0,
    fee_ton: float = 0.0,
    min_profit_rub: float = 0.0,
) -> tuple[float, float, float]:
    """
    Считает цену звезды от живой закупки поставщик.

    Возвращает (cost_per_star_rub, price_per_star_rub, effective_margin_percent).

    cost_per_star_rub — реальная закупка одной звезды в рублях;
    price_per_star_rub — цена клиенту: закупка + буфер + маржа, но не ниже
    «закупка + комиссии сети + минимальная прибыль». Буфер и min_profit
    нужны, чтобы курс/комиссии не съели прибыль между заказом и покупкой.
    """
    if stars_qty <= 0:
        raise ValueError("stars_qty must be positive")
    if quote_ton <= 0 or ton_rub <= 0:
        raise ValueError("quote_ton and ton_rub must be positive")

    cost_per_star = quote_ton / stars_qty * ton_rub
    fee_per_star = fee_ton * ton_rub / stars_qty

    by_margin = cost_per_star * (1 + margin_percent / 100) * (
        1 + buffer_percent / 100
    )
    by_min_profit = cost_per_star + fee_per_star + min_profit_rub
    price_per_star = max(by_margin, by_min_profit)

    effective_margin = (price_per_star / cost_per_star - 1) * 100
    return (
        round(cost_per_star, 4),
        round(price_per_star, 4),
        round(effective_margin, 2),
    )
