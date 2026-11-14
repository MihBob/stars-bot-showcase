"""Тесты расчёта суммы sweep (чистая функция, без сети)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from payments.ton_wallet import sweep_amount


def test_sweep_amount_leaves_buffer_gas_and_pending_invoices():
    assert sweep_amount(balance=10, keep_ton=5, gas_reserve=0.05, reserve_ton=2) == 2.95


def test_sweep_amount_never_goes_negative():
    assert sweep_amount(balance=10, keep_ton=5, gas_reserve=0.05, reserve_ton=5) == 0
    assert sweep_amount(balance=1, keep_ton=5, gas_reserve=0.05, reserve_ton=0) == 0


def test_sweep_amount_rounds_to_six_decimals():
    assert sweep_amount(balance=10.123456789, keep_ton=5, gas_reserve=0.05) == 5.073457
