"""Тесты первого исходящего перевода с hot-кошелька (без сети)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from payments.ton_wallet import _send_transfer

TO = "UQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJKZ"


class FakeWallet:
    def __init__(self, uninitialized: bool):
        self.is_uninitialized = uninitialized
        self.private_key = b"k" * 64
        self.wallet_id = 2147483409
        self.state_init = object()
        self.seqno = 0
        self.updated = 0
        self.transferred = []
        self.sent = []

    async def update(self):
        self.updated += 1

    async def transfer(self, to, amount, body=None):
        self.transferred.append((to, amount, body))

    def create_wallet_internal_message(self, destination, value, body):
        return ("internal", destination, value, body)

    async def get_seqno(self):
        return self.seqno

    def raw_create_transfer_msg(self, private_key, seqno, wallet_id, messages):
        return ("external", private_key, seqno, wallet_id, messages)

    async def send_external(self, body=None, state_init=None):
        self.sent.append((body, state_init))


def test_first_transfer_deploys_wallet_with_state_init():
    wallet = FakeWallet(uninitialized=True)

    asyncio.run(_send_transfer(wallet, TO, 690_500_000, "invoice"))

    assert wallet.updated == 1
    assert wallet.transferred == []
    assert len(wallet.sent) == 1
    body, state_init = wallet.sent[0]
    assert state_init is wallet.state_init
    assert body[0] == "external"
    assert body[2] == 0  # seqno ещё 0
    assert body[3] == wallet.wallet_id


def test_initialized_wallet_uses_normal_transfer():
    wallet = FakeWallet(uninitialized=False)

    asyncio.run(_send_transfer(wallet, TO, 690_500_000, "invoice"))

    assert wallet.updated == 1
    assert wallet.sent == []
    assert len(wallet.transferred) == 1
    assert wallet.transferred[0][1] == 690_500_000
