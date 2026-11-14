"""Тесты расчёта автоцены от живой закупки поставщик (без сети)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pricing_core import auto_price_sync


def test_auto_price_adds_buffer_and_margin():
    cost, price, margin = auto_price_sync(
        stars_qty=50,
        quote_ton=0.69,
        ton_rub=95.5,
        margin_percent=22,
        buffer_percent=5,
        fee_ton=0.02,
        min_profit_rub=0.10,
    )

    # 0.69 / 50 * 95.5 = 1.3179 ₽ за звезду
    assert cost == 1.3179
    # 1.3179 * 1.05 * 1.22 = 1.6882 ₽
    assert price == 1.6882
    assert margin == 28.1


def test_auto_price_never_below_fees_and_min_profit():
    cost, price, margin = auto_price_sync(
        stars_qty=50,
        quote_ton=0.69,
        ton_rub=95.5,
        margin_percent=0,
        buffer_percent=0,
        fee_ton=0.02,
        min_profit_rub=0.20,
    )

    fee_per_star = 0.02 * 95.5 / 50
    assert price == round(cost + fee_per_star + 0.20, 4)
    assert margin > 0


def test_auto_price_rejects_bad_input():
    with pytest.raises(ValueError):
        auto_price_sync(stars_qty=0, quote_ton=1, ton_rub=100, margin_percent=20)
    with pytest.raises(ValueError):
        auto_price_sync(stars_qty=50, quote_ton=0, ton_rub=100, margin_percent=20)
    with pytest.raises(ValueError):
        auto_price_sync(stars_qty=50, quote_ton=1, ton_rub=0, margin_percent=20)
