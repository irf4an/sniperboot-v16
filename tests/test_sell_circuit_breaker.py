"""Unit test: circuit breaker jual + rem darurat monitor posisi riil.

Latar (bug produksi 2026-11-16): 7 posisi LIVE masuk koin yang dev-nya
freeze transfer. Setiap siklus monitor (poll 3 dtk) mencoba jual lagi,
gagal, dan TIDAK PERNAH berhenti — 1.481 percobaan / 2.996 revert dalam
~6 jam, plus spam notifikasi. Ganti ke mode latihan tidak menolong karena
monitor posisi riil memang tidak membaca agent_dry_run.

Yang diuji:
1) Revert permanen (freeze/blacklist/pause) dikenali, revert harga tidak.
2) Gagal 1..2 → hanya peringatan; gagal ke-N → posisi jadi `closed_stuck`
   dan percobaan otomatis berhenti.
3) `live_monitor_on=0` → _check() TIDAK menyentuh jaringan sama sekali
   untuk posisi riil, tapi posisi simulasi tetap diproses.

Jalankan: python3 tests/test_sell_circuit_breaker.py
"""
import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# DB_PATH harus diset sebelum import sniper.config (dibaca saat import)
_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmpdb.close()
os.environ["DB_PATH"] = _tmpdb.name

from sniper import config, db, engine  # noqa: E402
from sniper.trader import _permanent_revert  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(f"{name}{' — ' + detail if detail and not cond else ''}")
    print(f"  {'OK' if cond else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not cond else ""))


DB_INIT = False


def open_pos(symbol, sim, ca):
    db.open_position(
        ca=ca, symbol=symbol, name=symbol, pair_address="0x" + "1" * 40,
        chain_slug="robinhood", pool_type="v4", fee=0,
        entry_price_usd=1e-5, tokens_total=3000.0, tokens_left=3000.0,
        eth_spent=0.000113, usd_spent=0.2, honeypot=0,
        source_name="SCANNER", caller_id=None, caller_name="SCANNER",
        buy_tx="0x" + "2" * 64, is_simulated=sim)
    return max(p["id"] for p in db.open_positions() if p["ca"] == ca)


db.init()

# ======================================================================
# 1) default setting baru terpasang & terbaca sebagai angka
# ======================================================================
print("\n[1] setting baru")
check("SETTING_DEFAULTS sell_max_fail = 3",
      config.SETTING_DEFAULTS.get("sell_max_fail") == "3")
check("db.get(sell_max_fail, int) == 3", int(db.get("sell_max_fail", int)) == 3)
check("default live_monitor_on = 1 (monitor riil nyala)",
      float(db.get("live_monitor_on") or 1) == 1)
check("default prebuy_sell_probe = 1",
      float(db.get("prebuy_sell_probe") or 0) == 1)
check("sell_max_fail punya label panel",
      "sell_max_fail" in config.SETTING_LABELS)

# ======================================================================
# 2) deteksi revert permanen vs revert harga
# ======================================================================
print("\n[2] _permanent_revert")
check("'token frozen' → permanen",
      _permanent_revert(RuntimeError("execution reverted: token frozen")))
check("'address is blacklisted' → permanen",
      _permanent_revert(RuntimeError("Address is Blacklisted")))
check("'Transfers disabled' → permanen",
      _permanent_revert(RuntimeError("Transfers disabled by owner")))
check("'trading paused' → permanen",
      _permanent_revert(RuntimeError("trading paused")))
check("revert slippage TIDAK permanen (panic exit masih boleh)",
      not _permanent_revert(RuntimeError("TooLittleReceived(1, 1000)")))
check("revert generik TIDAK permanen",
      not _permanent_revert(RuntimeError("execution reverted")))

# ======================================================================
# 3) circuit breaker: gagal 1..2 → warning, gagal ke-3 → stuck
# ======================================================================
print("\n[3] circuit breaker")
pid = open_pos("SCAM", 1, "0x" + "a" * 40)
err = RuntimeError("execution reverted: token frozen")
m1 = engine._handle_sell_failure(pid, err)
check("gagal #1 → peringatan 1/3", "1/3" in m1, m1[:120])
check("gagal #1 → posisi MASIH open", db.position(pid)["status"] == "open")
m2 = engine._handle_sell_failure(pid, err)
check("gagal #2 → peringatan 2/3", "2/3" in m2, m2[:120])
m3 = engine._handle_sell_failure(pid, err)
check("gagal #3 → posisi ditandai MACET", "MACET" in m3, m3[:160])
check("gagal #3 → status closed_stuck",
      db.position(pid)["status"] == "closed_stuck",
      str(db.position(pid)["status"]))
check("gagal #3 → closed_at diisi", bool(db.position(pid)["closed_at"]))
check("penghitung gagal di-reset setelah stuck",
      pid not in engine._sell_fails)
check("posisi stuck tidak muncul di open_positions()",
      all(p["id"] != pid for p in db.open_positions()))

# posisi stuck tidak dicoba jual lagi oleh alur normal (tanpa jaringan)
out = asyncio.run(engine._execute_sell_locked(pid, 1.0, "timeout"))
check("execute_sell pada posisi stuck → ditolak tanpa jaringan",
      "sudah closed" in out, out[:120])

# ======================================================================
# 4) rem darurat: live_monitor_on=0 → posisi RIIL tak disentuh
# ======================================================================
print("\n[4] rem darurat live_monitor_on")
_net = {"n": 0}


async def _boom(*a, **k):
    _net["n"] += 1
    raise RuntimeError("jaringan dipanggil — seharusnya TIDAK terjadi")


_orig_pair = engine.current_pair
engine.current_pair = _boom
try:
    db.set_("live_monitor_on", "0")
    live_id = open_pos("LIVE", 0, "0x" + "b" * 40)
    sim_id = open_pos("SIM", 1, "0x" + "c" * 40)
    asyncio.run(engine._check(db.position(live_id)))
    check("posisi RIIL dilewati tanpa panggilan jaringan", _net["n"] == 0,
          f"current_pair terpanggil {_net['n']}x")
    check("posisi RIIL tetap open (bukan ditutup paksa)",
          db.position(live_id)["status"] == "open")

    db.set_("live_monitor_on", "1")
    try:
        asyncio.run(engine._check(db.position(live_id)))
    except RuntimeError:
        pass          # memang diharapkan: jaringan dipanggil lagi
    check("live_monitor_on=1 → posisi RIIL kembali diproses",
          _net["n"] > 0)
finally:
    engine.current_pair = _orig_pair
    db.set_("live_monitor_on", "1")
    try:
        db.update_position(live_id, status="closed_manual")
        db.update_position(sim_id, status="closed_manual")
    except Exception:
        pass

# ======================================================================
# 5) LP v4 = 0 harus TERDETEKSI walau quote_sell_eth melempar exception
# ======================================================================
print("\n[5] rug on-chain tetap terdeteksi saat quote raise")

import sniper.v4 as v4mod  # noqa: E402


class _FakeFn:
    def __init__(self, val):
        self.val = val

    def call(self):
        return self.val


class _FakeTok:
    functions = type("F", (), {"decimals": staticmethod(lambda: _FakeFn(18))})


class _FakeTrader:
    w3 = object()

    def erc20(self, ca):
        return _FakeTok()

    def quote_sell_eth(self, *a, **k):
        # kondisi nyata: pool kosong → quoter/router revert (V4TooLittleReceived)
        raise RuntimeError("V4TooLittleReceived(1,0)")


_notifs, _sells = [], []
_orig = (engine.current_pair, engine.execute_sell, engine._trader,
         engine._notify, v4mod.liquidity)


async def _pair_stub(pos):
    return {"priceUsd": "0.00001", "priceNative": "0.00000001",
            "liquidity": {"usd": 8000.0},
            "txns": {"m5": {"buys": 3, "sells": 3}}}


async def _sell_stub(pid, frac, reason):
    _sells.append(reason)
    return "ok"


async def _notify_stub(text, buttons=None):
    _notifs.append(text)
    return None


try:
    engine.current_pair = _pair_stub
    engine.execute_sell = _sell_stub
    engine._trader = _FakeTrader()
    engine._notify = _notify_stub
    v4mod.liquidity = lambda pool_id, w3: 0

    rug_id = open_pos("RUGGED", 1, "0x" + "d" * 40)
    asyncio.run(engine._check(db.position(rug_id)))

    check("quote raise + LP 0 → jalur RUG dipicu", "rug" in _sells,
          f"sells={_sells}")
    check("ada notifikasi RUG PULL ON-CHAIN",
          any("LP v4 on-chain = 0" in t for t in _notifs),
          f"notifs={len(_notifs)}")
    check("tidak jatuh ke time-stop / fallback harga layar",
          "timeout" not in _sells, f"sells={_sells}")
finally:
    (engine.current_pair, engine.execute_sell, engine._trader,
     engine._notify, v4mod.liquidity) = _orig

# ======================================================================
# 6) AKUNTANSI PAPER JUJUR: tanpa fallback harga layar
# ======================================================================
print("\n[6] akuntansi paper jujur (tanpa fallback harga layar)")


class _HonestTrader(_FakeTrader):
    def quote_sell_eth(self, *a, **k):
        raise RuntimeError("quoter/RPC gagal")


async def _pair_naik(pos):
    # harga layar 2x entry -> di mode lama ini memicu TP1 "menang"
    return {"priceUsd": "0.00002", "priceNative": "0.00000002",
            "liquidity": {"usd": 18000.0},
            "txns": {"m5": {"buys": 5, "sells": 5}}}


check("default SETTING_DEFAULTS paper_honest = 1",
      config.SETTING_DEFAULTS.get("paper_honest") == "1")
check("_honest_paper() True secara default", engine._honest_paper() is True)

_orig2 = (engine.current_pair, engine.execute_sell, engine._trader,
          engine._notify, v4mod.liquidity)
try:
    engine.current_pair = _pair_naik
    engine.execute_sell = _sell_stub
    engine._trader = _HonestTrader()
    engine._notify = _notify_stub
    v4mod.liquidity = lambda pool_id, w3: 12345   # pool MASIH berisi

    # --- 6a) jual simulasi saat quote gagal -> TIDAK dikarang, status khusus
    h_id = open_pos("HONEST", 1, "0x" + "e" * 40)
    msg = asyncio.run(engine._execute_sell_locked(h_id, 1.0, "tp1"))
    row = db.position(h_id)
    check("quote gagal -> status closed_unsellable",
          row["status"] == "closed_unsellable", str(row["status"]))
    check("quote gagal -> realized 0 (rugi penuh, bukan profit layar)",
          float(row["realized_eth"] or 0) == 0.0, str(row["realized_eth"]))
    check("pesan menyebut TIDAK BISA DIJUAL", "TIDAK BISA DIJUAL" in msg,
          msg[:160])
    check("pesan menyatakan harga layar diabaikan",
          "DIABAIKAN" in msg, msg[:200])

    # --- 6b) _check TIDAK memicu TP/SL dari harga layar saat quote gagal
    _sells.clear()
    c_id = open_pos("CHECK6", 1, "0x" + "f" * 40)
    asyncio.run(engine._check(db.position(c_id)))
    check("mode jujur: TP tidak dipicu dari harga layar", _sells == [],
          f"sells={_sells}")
    check("posisi tetap open (tidak ada keputusan palsu)",
          db.position(c_id)["status"] == "open")

    # --- 6c) mode lama (paper_honest=0) memang jatuh ke harga layar
    db.set_("paper_honest", "0")
    check("_honest_paper() False saat setting 0", engine._honest_paper() is False)
    _sells.clear()
    l_id = open_pos("LEGACY", 1, "0x" + "1" * 40)
    asyncio.run(engine._check(db.position(l_id)))
    check("mode lama: exit dipicu dari harga layar (perilaku yang dibuang)",
          len(_sells) > 0, f"sells={_sells}")
finally:
    db.set_("paper_honest", "1")
    (engine.current_pair, engine.execute_sell, engine._trader,
     engine._notify, v4mod.liquidity) = _orig2

# ======================================================================
print(f"\n=== PASS {len(PASS)} / FAIL {len(FAIL)} ===")
if FAIL:
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1)
print("SEMUA OK")