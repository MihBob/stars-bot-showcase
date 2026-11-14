"""Тесты вывода ключа/адреса hot-кошелька из seed-фразы (без сети).

12 слов — multichain/BIP39-фраза нового Tonkeeper (m/44'/607'/0').
24 слова — классическая TON-фраза (PBKDF2 "TON default seed").
"""
import sys
from pathlib import Path

import pytest
from pytoniq_core.crypto.keys import private_key_to_public_key

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from payments.ton_wallet import _private_key_from_mnemonic, _wallet_candidates

# Публичный тестовый вектор BIP39 (не чей-то реальный кошелёк).
BIP39_VECTOR = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about".split()


def test_12_word_bip39_derivation_matches_bip44_vector():
    private_key = _private_key_from_mnemonic(BIP39_VECTOR)

    # Значения сверены с независимой реализацией BIP44 (coin 607, account 0).
    assert private_key[:32].hex() == (
        "b477ef5ed17fb8a2b8faddd7a9835a227243a82c70b190c7af4896155aa7df9f"
    )
    assert private_key_to_public_key(private_key).hex() == (
        "7952e94118f34607c75e23258dd9220d66ccac5a3ee074125c25068e8107bfbf"
    )


def test_12_word_seed_has_three_wallet_versions():
    candidates = _wallet_candidates(_private_key_from_mnemonic(BIP39_VECTOR))

    assert [c["version"] for c in candidates] == ["v3r2", "v4r2", "v5r1"]
    addresses = {c["address"].to_str() for c in candidates}
    assert len(addresses) == 3


def test_wrong_word_count_is_rejected():
    with pytest.raises(RuntimeError, match="12"):
        _private_key_from_mnemonic(["one", "two", "three"])
