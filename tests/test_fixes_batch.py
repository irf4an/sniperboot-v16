"""Uji batch perbaikan bug + fitur self-check source Telegram.

Menguji hal yang sebelumnya tidak terjaga:
1) monitor_loop: satu posisi error tidak membatalkan cek posisi lain
2) verify_sources: source yang entity-nya mati dimatikan otomatis + dilaporkan
3) dev_pct: "tidak tahu" mengembalikan None (bukan 0.0 = seolah aman)
4) audit_contract: fail-CLOSED saat ABI tidak terbaca (safety_fail_closed=1)
5) buy_slippage default tidak lagi 0.60
6) kontrak return process_ca: tidak ada lagi `return (False, ...)`

Jalankan: python3 tests/test_fixes_batch.py
"""
import asyncio
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmpdb.close()
os.environ["DB_PATH"] = _tmpdb.name

from sniper import config, db, engine, listener, safety  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK' if cond else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not cond else ""))


db.init()

# ======================================================================
print("\n[1] monitor: satu posisi error tidak membatalkan sisanya")
_dilihat = []


async def _fake_check(pos):
    _dilihat.append(pos["id"])
    if pos["id"] == 1:
        raise RuntimeError("posisi rusak (sengaja)")


_orig_check = engine._check
engine._check = _fake_check
try:
    asyncio.run(engine._check_all([{"id": 1}, {"id": 2}, {"id": 3}]))
finally:
    engine._check = _orig_check
check("posisi setelah yang error tetap dicek", _dilihat == [1, 2, 3],
      f"dicek={_dilihat}")

# ======================================================================
print("\n[2] startup self-check source Telegram")
db.add_source(-1001111111111, 0, "GRUP HIDUP", 45000)
db.add_source(-1002222222222, 0, "GRUP MATI", 45000)
rows_all = db.sources(only_enabled=False)
hidup = next(r for r in rows_all if r["name"] == "GRUP HIDUP")
mati = next(r for r in rows_all if r["name"] == "GRUP MATI")


class _FakeUserClient:
    async def get_entity(self, cid):
        if cid == -1001111111111:
            return type("E", (), {"title": "GRUP HIDUP"})()
        raise ValueError("Could not find the input entity")


res = asyncio.run(listener.verify_sources(_FakeUserClient()))
by_name = {r["name"]: r for r in res}
check("source hidup ditandai ok", by_name["GRUP HIDUP"]["ok"] is True)
check("source mati ditandai tidak ok", by_name["GRUP MATI"]["ok"] is False)
check("source mati otomatis di-disable", by_name["GRUP MATI"]["disabled"] is True)
after = {r["name"]: r for r in db.sources(only_enabled=False)}
check("DB menyimpan enabled=0 untuk source mati",
      after["GRUP MATI"]["enabled"] == 0, str(after["GRUP MATI"]["enabled"]))
check("source hidup tetap enabled", after["GRUP HIDUP"]["enabled"] == 1)
txt = listener.sources_report(res)
_h = sum(1 for r in res if r["ok"])
_m = sum(1 for r in res if not r["ok"])
check("laporan menyebut jumlah hidup/mati",
      f"{_h} hidup, {_m} mati" in txt, txt[:160])
check("laporan menyebut grup yang mati", "GRUP MATI" in txt)
check("verify_sources(None) aman (userbot off)", asyncio.run(
    listener.verify_sources(None)) == [])

# ======================================================================
print("\n[3] dev_pct: tidak tahu = None, bukan 0.0")
_orig_creator = safety.creator_of
try:
    safety.creator_of = lambda ca, cache=True: None
    check("deployer tak ketemu → None", safety.dev_pct(None, "0x" + "a" * 40) is None)
    safety.creator_of = lambda ca, cache=True: "0x" + "0" * 40
    check("deployer = alamat DEAD → 0.0 (memang nol)",
          safety.dev_pct(None, "0x" + "a" * 40) == 0.0)
finally:
    safety.creator_of = _orig_creator

# ======================================================================
print("\n[4] audit_contract: fail-closed saat ABI tak terbaca")


class _FakeW3:
    class eth:
        @staticmethod
        def get_code(addr):
            return bytes.fromhex("6080604052348015")   # bytecode wajar, tanpa selector bahaya


class _FakeTrader:
    w3 = _FakeW3()


_orig_get = safety._get
try:
    def _boom(path, params=None, timeout=10):
        raise RuntimeError("Blockscout tidak terjangkau")

    safety._get = _boom
    db.set_("safety_fail_closed", "1")
    ok, why = safety.audit_contract(_FakeTrader(), "0x" + "b" * 40)
    check("fail-closed: ABI tak terbaca → TOLAK", ok is False, why)
    check("alasan menyebut ABI tidak terbaca", "ABI tidak terbaca" in why, why)

    db.set_("safety_fail_closed", "0")
    ok2, why2 = safety.audit_contract(_FakeTrader(), "0x" + "b" * 40)
    check("fail-open dipilih operator → lolos", ok2 is True, why2)
finally:
    safety._get = _orig_get
    db.set_("safety_fail_closed", "1")

# ======================================================================
print("\n[5] setting slippage & kontrak return")
check("default buy_slippage bukan lagi 0.60",
      config.SETTING_DEFAULTS.get("buy_slippage") != "0.60",
      str(config.SETTING_DEFAULTS.get("buy_slippage")))
check("default buy_slippage = 0.25",
      config.SETTING_DEFAULTS.get("buy_slippage") == "0.25")
check("nilai aktif di DB ikut 0.25", abs(db.get("buy_slippage") - 0.25) < 1e-9,
      str(db.get("buy_slippage")))

_src = open(os.path.join(ROOT, "sniper", "listener.py"), encoding="utf-8").read()
check("tidak ada lagi 'return (False,' di listener.process_ca",
      not re.search(r"return \(False,", _src))
check("fee posisi simulasi memakai fee_dx, bukan hardcode 3000",
      '"pool_type": ptype_dx, "fee": fee_dx,' in _src)

_trad = open(os.path.join(ROOT, "sniper", "trader.py"), encoding="utf-8").read()
check("docstring trader.sell menyebut 4 nilai kembalian",
      "Return (tokens_sold, eth_realized, gas_eth, swap_tx)" in _trad)

# ======================================================================
print(f"\n=== PASS {len(PASS)} / FAIL {len(FAIL)} ===")
if FAIL:
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1)
print("SEMUA OK")