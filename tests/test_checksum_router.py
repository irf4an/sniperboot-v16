"""Regresi: spender non-checksum tidak boleh menggagalkan approval pasca-buy.

Insiden CB (2026-11-12): swap Uniswap v4 SUKSES on-chain, lalu
`_ensure_approval()` melempar
    "web3.py only accepts checksum addresses ... 0x8876...0904"
karena `v4.UNIVERSAL_ROUTER` ditulis lowercase. Exception terjadi SETELAH
ETH terpakai, sehingga posisi tidak pernah masuk DB dan TP/SL otomatis mati.

Tes ini mengunci dua hal:
  1. konstanta UNIVERSAL_ROUTER sudah checksum;
  2. `_ensure_approval()` menormalkan spender sebelum menyentuh web3.
"""
from web3 import Web3

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper import trader as trader_mod  # noqa: E402
from sniper import v4  # noqa: E402


LOWER_ROUTER = "0x8876789976decbfcbbbe364623c63652db8c0904"


class _Allowance:
    def __init__(self, sink):
        self._sink = sink

    def call(self):
        return 0


class _FakeFns:
    def __init__(self, sink):
        self._sink = sink

    def allowance(self, owner, spender):
        self._sink["spender"] = spender
        return _Allowance(self._sink)

    def approve(self, spender, amount):
        self._sink["approve_spender"] = spender
        return ("approve", spender, amount)


class _FakeToken:
    def __init__(self, sink):
        self.functions = _FakeFns(sink)


class _FakeSelf:
    """Cukup untuk memanggil Trader._ensure_approval sebagai fungsi lepas."""

    def __init__(self, sink):
        self._sink = sink
        self.approved = []
        self.addr = "0x000000000000000000000000000000000000dEaD"

    def erc20(self, addr):
        self._sink["token"] = addr
        return _FakeToken(self._sink)

    def _send(self, fn):
        self._sink["approved"] = True


def test_konstanta_router_checksum():
    assert Web3.is_checksum_address(v4.UNIVERSAL_ROUTER), v4.UNIVERSAL_ROUTER
    assert v4.UNIVERSAL_ROUTER.lower() == LOWER_ROUTER
    print("PASS UNIVERSAL_ROUTER checksum")


def test_konstanta_pool_manager_checksum():
    assert Web3.is_checksum_address(v4.POOL_MANAGER), v4.POOL_MANAGER
    print("PASS POOL_MANAGER checksum")


def test_ensure_approval_menormalkan_spender():
    sink = {}
    fake = _FakeSelf(sink)
    trader_mod.Trader._ensure_approval(fake, LOWER_ROUTER, LOWER_ROUTER)
    spender = sink.get("spender")
    assert spender is not None, "allowance() tidak pernah dipanggil"
    assert Web3.is_checksum_address(spender), f"spender masih non-checksum: {spender}"
    assert spender.lower() == LOWER_ROUTER
    assert sink.get("approved") is True, "allowance 0 seharusnya memicu approve()"
    print("PASS _ensure_approval menormalkan spender lowercase")


def test_ensure_approval_spender_checksum_tidak_berubah():
    sink = {}
    fake = _FakeSelf(sink)
    good = Web3.to_checksum_address(LOWER_ROUTER)
    trader_mod.Trader._ensure_approval(fake, good, good)
    assert sink.get("spender") == good
    print("PASS spender checksum diteruskan apa adanya")


if __name__ == "__main__":
    test_konstanta_router_checksum()
    test_konstanta_pool_manager_checksum()
    test_ensure_approval_menormalkan_spender()
    test_ensure_approval_spender_checksum_tidak_berubah()
    print("\n✅ SEMUA TES CHECKSUM ROUTER LULUS")
