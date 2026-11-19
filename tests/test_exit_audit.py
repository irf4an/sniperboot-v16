"""Audit exit: pastikan harga 'layar' vs on-chain tersimpan saat jual.

Konteks: notif Telegram "layar -8.4% → realized +39.4%" tidak bisa diaudit
karena angka mentahnya tidak pernah masuk DB. Tanpa jejak ini, cuan dari
wick di pool tipis tidak bisa dibedakan dari edge nyata.
"""
import os
import sys
import time
import tempfile
import asyncio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sniper import config  # noqa: E402

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
config.DB_PATH = _tmp.name          # WAJIB: jangan sentuh DB live

from sniper import db, engine  # noqa: E402

db.init()

FAIL = []


def check(name, cond):
    if cond:
        print(f"  OK {name}")
    else:
        print(f"  FAIL {name}")
        FAIL.append(name)


# ---------- 1) skema tabel exit_log ----------
print("[1] skema exit_log")
import sqlite3  # noqa: E402
c = sqlite3.connect(_tmp.name)
cols = [r[1] for r in c.execute("PRAGMA table_info(exit_log)")]
for need in ("pos_id", "symbol", "reason", "tokens_sold", "eth_out",
             "spot_pnl", "real_pnl", "px_dex", "px_onchain", "spread_pct",
             "pool_liq_usd"):
    check(f"kolom {need} ada", need in cols)

# ---------- 2) log_exit menghitung spread ----------
print("\n[2] log_exit + spread_pct")
rid = db.log_exit(1, "OWED", "0x" + "ab" * 20, "tp1", 148701.0, 0.002315,
                  1805.56, -0.084, 0.394, 1.89421e-05, 2.881e-05,
                  liq_usd=5017.25, extra="left=99134;sim=1")
check("rowid dikembalikan", rid == 1)
r = db.recent_exits(1)[0]
check("spread = onchain/dex - 1 (~52%)", abs(r["spread_pct"] - 0.521) < 0.01)
check("spot_pnl & real_pnl tersimpan", r["spot_pnl"] == -0.084 and r["real_pnl"] == 0.394)
check("liq tersimpan", r["pool_liq_usd"] == 5017.25)

# ---------- 3) spread tidak dihitung saat harga 0 ----------
print("\n[3] harga 0 → spread None (bukan inf)")
db.log_exit(2, "ZERO", "0x" + "cd" * 20, "rug", 100, 0.0, 1800,
            -1.0, -1.0, 0.0, 0.0)
r2 = db.recent_exits(1)[0]
check("spread None saat px 0", r2["spread_pct"] is None)

# ---------- 4) statistik divergensi ----------
print("\n[4] exit_audit_stats")
db.log_exit(3, "THIN", "0x" + "ee" * 20, "timeout", 1000, 0.001, 1800,
            -0.50, 0.80, 1e-5, 3.6e-5, liq_usd=180.0)
db.log_exit(4, "DUST", "0x" + "ff" * 20, "trail", 500, 0.0002, 1800,
            0.40, -0.30, 2e-5, 1e-5, liq_usd=900.0)
st = db.exit_audit_stats()
check("total = 4", st["total"] == 4)
check("divergen >25 poin = 3", st["divergen"] == 3)
check("on-chain lebih baik = 2", st["real_lebih_baik"] == 2)
check("layar lebih baik = 1", st["layar_lebih_baik"] == 1)

# ---------- 5) filter pos_id ----------
print("\n[5] recent_exits(pos_id=)")
one = db.recent_exits(5, pos_id=1)
check("hanya 1 baris", len(one) == 1)
check("baris benar", one[0]["symbol"] == "OWED")

# ---------- 6) exit ASLI lewat engine → exit_log terisi ----------
print("\n[6] jalur exit ASLI (engine) menulis exit_log")
CA = "0x4eb9ade7369a51762f913b8f96812b4ad5bd1bef"
POOL = "0xe7e4002fd378b5c5befae4e4014eaeabf83b390dd11e55237ef4a281b78c4532"

before = len(db.recent_exits(200))
db.open_position(ca=CA, symbol="E2E", pair_address=POOL,
                 chain_slug="robinhood", pool_type="v4", fee=3000,
                 entry_price_usd=2.06791084351717e-05,
                 tokens_total=247834.669278258, tokens_left=99134.0,
                 eth_spent=0.0027670372160464, usd_spent=5.0,
                 source_name="RADAR PONS")
pid = db.open_positions()[-1]["id"]
db.update_position(pid, tp1_done=1, is_simulated=1,
                   ts=int(time.time()) - 300)
db.set_("live_monitor_on", "1")
db.set_("max_hold_sec", "60")

import sniper.trader as tr  # noqa: E402
engine._trader = tr.Trader()
_msgs = []


async def _n(*a, **k):
    _msgs.append(str(a[0]) if a else "")


engine._notify = _n
engine._notify_exit = _n

asyncio.run(engine._check(db.position(pid)))

after = db.recent_exits(200)
check("exit_log bertambah 1", len(after) == before + 1)
newest = after[0]
check("reason = timeout", newest["reason"] == "timeout")
check("pos_id benar", newest["pos_id"] == pid)
check("tokens_sold > 0", (newest["tokens_sold"] or 0) > 0)
check("px_dex terisi", (newest["px_dex"] or 0) > 0)
check("px_onchain terisi", (newest["px_onchain"] or 0) > 0)
check("liq pool terisi", (newest["pool_liq_usd"] or 0) > 0)
check("posisi jadi closed", db.position(pid)["status"] != "open")

# ---------- 7) audit TIDAK boleh menggagalkan exit ----------
print("\n[7] kegagalan audit tidak memblokir exit")
_orig_log = db.log_exit


def _boom(*a, **k):
    raise RuntimeError("audit dipaksa gagal")


db.log_exit = _boom
engine.db.log_exit = _boom
db.open_position(ca=CA, symbol="SAFE", pair_address=POOL,
                 chain_slug="robinhood", pool_type="v4", fee=3000,
                 entry_price_usd=2.06791084351717e-05,
                 tokens_total=247834.669278258, tokens_left=99134.0,
                 eth_spent=0.0027670372160464, usd_spent=5.0,
                 source_name="RADAR PONS")
pid2 = db.open_positions()[-1]["id"]
db.update_position(pid2, tp1_done=1, is_simulated=1,
                   ts=int(time.time()) - 300)
r3 = asyncio.run(engine._check(db.position(pid2)))
db.log_exit = _orig_log
engine.db.log_exit = _orig_log
check("exit tetap sukses walau audit gagal", db.position(pid2)["status"] != "open")

os.unlink(_tmp.name)
print()
if FAIL:
    print(f"TOTAL: {len(FAIL)} FAIL")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("SEMUA TES AUDIT EXIT LULUS")
