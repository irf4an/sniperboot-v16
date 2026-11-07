"""🧪 Uji fallback holder on-chain saat Blockscout belum meng-index token baru.

Lingkup:
  1. Endpoint /tokens/{ca}/holders kosong (items=[]) → saldo direkonstruksi
     dari event Transfer via RPC, bukan langsung dianggap "tidak bisa
     diverifikasi" (yang berujung SKIP fail-closed di koin baru).
  2. Kalau riwayat Transfer belum lengkap (tidak ada mint genesis), fallback
     MENOLAK dipakai — lebih baik "tidak tahu" daripada menghitung porsi dari
     data separuh.
  3. Pool/dev/burn tetap dikecualikan dari hitungan top-10.
"""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper import safety  # noqa: E402

POOL = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
DEV = "0x" + "d1" * 20
BURN = "0x000000000000000000000000000000000000dead"
SUPPLY = 1_000_000_000 * 10**18


class _Log(dict):
    pass


def _mk_log(frm, to, value):
    return _Log({
        "topics": [
            "0x" + "00" * 32,
            bytes.fromhex("00" * 12 + frm[2:]),
            bytes.fromhex("00" * 12 + to[2:]),
        ],
        "data": bytes.fromhex(format(value, "064x")),
        "blockNumber": 1,
        "logIndex": 0,
    })


class _FakeEth:
    def __init__(self, chunks):
        self._chunks = chunks
        self.block_number = 100

    def get_logs(self, filt):
        lo = filt["fromBlock"]
        return self._chunks.get(lo, [])

    def contract(self, address, abi=None):
        class _Fn:
            def call(self_inner):
                return SUPPLY

        class _Fns:
            def totalSupply(self_inner):
                return _Fn()

        class _C:
            functions = _Fns()
        return _C()


class _FakeW3:
    def __init__(self, eth):
        self.eth = eth


class _FakeTrader:
    def __init__(self, eth):
        self.w3 = _FakeW3(eth)


def _patch_get(items):
    calls = {"n": 0}

    def _fake_get(path, params=None, timeout=10):
        calls["n"] += 1
        return {"items": items}

    safety._get = _fake_get
    return calls


def test_fallback_pakai_saldo_onchain():
    # mint genesis: 1B ke dev; dev kirim 60% ke pool, 15% burn, sisanya dibagi
    logs = [
        _mk_log("0x" + "00" * 20, DEV, SUPPLY),
        _mk_log(DEV, POOL, SUPPLY * 60 // 100),
        _mk_log(DEV, BURN, SUPPLY * 15 // 100),
    ]
    for i in range(10):
        logs.append(_mk_log(DEV, "0x" + f"{i+1:02x}" * 20, SUPPLY // 100))
    eth = _FakeEth({0: logs})
    safety._get = lambda *a, **k: {"items": []}          # Blockscout kosong
    safety._ONCHAIN_HOLDERS.clear()

    tp = safety.top_holders_pct(_FakeTrader(eth), "0x" + "aa" * 20,
                                {POOL, DEV}, n=10)
    # sisa di dev = 1B - 60% - 15% - 10*1% = 15% → masih dihitung (dev tak
    # dimasukkan ke exclude di sini? dev ADA di exclude) → 10 wallet @1%
    assert abs(tp - 10.0) < 0.01, f"harus 10.0% (pool+dev+burn dikecualikan), dapat {tp}"
    print(f"PASS fallback on-chain: top10={tp:.2f}% (Blockscout kosong)")


def test_riwayat_tidak_lengkap_ditolak():
    # tidak ada mint genesis & riwayat mentok → jangan dipakai
    logs = [_mk_log(DEV, "0x" + "11" * 20, SUPPLY // 2)]
    eth = _FakeEth({0: logs})
    safety._get = lambda *a, **k: {"items": []}
    safety._ONCHAIN_HOLDERS.clear()

    tp = safety.top_holders_pct(_FakeTrader(eth), "0x" + "bb" * 20, {POOL})
    assert tp == 0.0, f"riwayat separuh harus ditolak (0.0), dapat {tp}"
    print("PASS riwayat tidak lengkap → fallback menolak (0.0)")


def test_blockscout_normal_tidak_pakai_rpc():
    items = [{"address": {"hash": "0x" + f"{i+1:02x}" * 20},
              "value": str(SUPPLY // 100)} for i in range(10)]
    eth = _FakeEth({})
    safety._get = lambda *a, **k: {"items": items}
    safety._ONCHAIN_HOLDERS.clear()

    tp = safety.top_holders_pct(_FakeTrader(eth), "0x" + "cc" * 20, {POOL})
    assert abs(tp - 10.0) < 0.01, f"harus 10.0%, dapat {tp}"
    print("PASS data Blockscout tersedia → tidak menyentuh RPC")


if __name__ == "__main__":
    test_fallback_pakai_saldo_onchain()
    test_riwayat_tidak_lengkap_ditolak()
    test_blockscout_normal_tidak_pakai_rpc()
    print("\n✅ SEMUA TES HOLDER FALLBACK LULUS")
