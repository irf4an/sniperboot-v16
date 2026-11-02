"""Unit test antrean pending pool retry (Dexscreener indexing lag).

Firehose on-chain mendeteksi CA pool baru dalam hitungan detik, tapi
Dexscreener butuh 1-3 menit untuk mengindeks pair-nya. Tanpa antrean,
CA dibuang saat best_pair -> None danhilang selamanya.
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

from sniper import config, db  # noqa: E402
from sniper import scanner  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(f"{name}{' — ' + detail if detail and not cond else ''}")
    print(f"  {'OK' if cond else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not cond else ""))


# ======================================================================
# 1) Init: tabel pending_pools dibuat, helper bisa dipanggil
# ======================================================================
print("\n=== 1. init & helper helper antrean ===")
db.init()
CA1 = "0X" + "a" * 40  # huruf besar → harus dinormalisasi ke lower
CA1L = CA1.lower()
CA2 = "0x" + "b" * 40

db.add_pending_pool(CA1)
pools = db.get_pending_pools(max_age_sec=600)
check("add_pending_pool: CA tercatat (dinormalisasi lower)",
      len(pools) == 1 and pools[0]["ca"] == CA1L, str(pools))
check("add_pending_pool: retries awal 0", pools[0]["retries"] == 0)
check("add_pending_pool: idempotent (INSERT OR IGNORE)",
      len(db.get_pending_pools()) == 1)

# ======================================================================
# 2) Simulasi siklus 1: Dexscreener belum punya pair → tetap pending, retry nambah
# ======================================================================
print("\n=== 2. cycle 1: best_pair None → tetap di antrean ===")

_orig_best_pair = scanner.dx.best_pair
_orig_fetch_profiles = scanner.dx.profile_candidates
_orig_fetch_multipool = scanner.fetch_multipool_candidates
_orig_make_w3 = scanner._make_w3
_orig_market_ok = scanner.market_ok


class _FakePair:
    pass


def _fake_best_pair_none(ca):
    return None


def _fake_profiles():
    return []


def _fake_multipool(w3):
    return [CA1, CA2]  # 2 CA baru on-chain


def _fake_make_w3():
    return object()  # non-None → "factory on"


scanner.dx.best_pair = _fake_best_pair_none
scanner.dx.profile_candidates = _fake_profiles
scanner.fetch_multipool_candidates = _fake_multipool
scanner._make_w3 = _fake_make_w3

config.SCAN_ENABLED = False  # non-relevant tapi jaga config

# factory on
config.SETTING_DEFAULTS.setdefault("scanner_factory_on", "1")
db.set_("scanner_factory_on", "1")

r1 = scanner.collect_and_rank()
check("cycle1: tidak ada kandidat lolos (belum ada pair)", r1 == [], str(r1))
pools = {p["ca"]: p for p in db.get_pending_pools()}
check("cycle1: kedua CA tetap di pending_pools",
      set(pools) == {CA1L, CA2}, str(sorted(pools)))
check("cycle1: retry CA1 bertambah jadi 1", pools.get(CA1L, {}).get("retries") == 1)
check("cycle1: retry CA2 bertambah jadi 1", pools.get(CA2, {}).get("retries") == 1)

# ======================================================================
# 3) Simulasi siklus 2: best_pair kembali pair → keluar dari antrean
# ======================================================================
print("\n=== 3. cycle 2: best_pair mengembalikan pair → diproses, keluar dari antrean ===")

fake_pair = {
    "pairAddress": "0x" + "c" * 40,
    "pairCreatedAt": int((asyncio.get_event_loop().time() if False else __import__("time").time() * 1000) - 60_000),
    "liquidity": {"usd": 5000.0},
    "priceUsd": "0.0001",
    "marketCap": 45000.0,
    "volume": {"h1": 30000.0, "m5": 500.0},
    "txns": {"h1": {"buys": 50, "sells": 30},
             "m5": {"buys": 10, "sells": 2}},
    "priceChange": {"m5": 12.0, "h1": 30.0},
}


def _fake_best_pair_ca1(ca):
    return fake_pair if ca == CA1L else None


scanner.dx.best_pair = _fake_best_pair_ca1

# simulasikan 20+ detik berlalu (throttle retry antrean pending)
scanner._retry_last = {k: v - 25.0 for k, v in scanner._retry_last.items()}

r2 = scanner.collect_and_rank()
check("cycle2: CA1 lolos (pair ditemukan)",
      len(r2) == 1 and r2[0]["ca"] == CA1L, str(r2))
check("cycle2: score ada", r2 and isinstance(r2[0]["score"], float))
check("cycle2: item ditandai in_pending", r2 and r2[0].get("in_pending") is True, str(r2))
check("cycle2: retry=False (belum pernah dievaluasi listener)",
      r2 and r2[0].get("retry") is False, str(r2))
pools = {p["ca"]: p for p in db.get_pending_pools()}
check("cycle2: CA1 MASIH pending (menunggu status final listener)",
      CA1L in pools, str(sorted(pools)))
check("cycle2: CA2 masih pending (best_pair masih None)", CA2 in pools)
check("cycle2: retry CA2 = 2 (sudah 2 siklus None)",
      pools.get(CA2, {}).get("retries") == 2,
      str(pools.get(CA2)))

# ======================================================================
# 4) Simulasi kedaluwarsa (> 600 detik) → prune_pending_poles
# ======================================================================
print("\n=== 4. kedaluwarsa → prune_pending_pools ===")

# Backdating: langsung UPDATE first_seen lewat db internal — cara pengujian
# aman yang tidak perlu sleep 10 menit.
import sqlite3  # di atas: di-track untuk kejelasan test ini saja

with db._lock, db._conn() as c:
    c.execute("UPDATE pending_pools SET first_seen = ? WHERE ca = ?",
              (int(__import__("time").time()) - 700, CA2))

# query dengan max_age 600 → harus tidak keliatan
left = db.get_pending_pools(max_age_sec=600)
check("get_pending_pools: kedaluwarsa tak dikembalikan oleh query",
      all(p["ca"] != CA2 for p in left), str(left))

db.prune_pending_pools(max_age_sec=600)
left = db.get_pending_pools(max_age_sec=10**9)
check("prune_pending_pools: baris kedaluwarsa dihapus",
      all(p["ca"] != CA2 for p in left), str(left))

# ======================================================================
# 5) Retry: CA sudah dievaluasi tapi masih di antrean → boleh dicek ulang
# ======================================================================
print("\n=== 5. retry kandidat pending (seen_call=True, masih di antrean) ===")

db.add_pending_pool(CA1L)
db.record_call(CA1L, 1)  # simulasi listener sudah pernah mengevaluasi
scanner._retry_last.clear()
scanner.dx.best_pair = _fake_best_pair_ca1

r5 = scanner.collect_and_rank()
check("retry: CA muncul lagi walau sudah seen_call",
      [x["ca"] for x in r5] == [CA1L], str(r5))
check("retry: item.retry=True", r5 and r5[0].get("retry") is True, str(r5))
check("retry: throttle 20s → siklus berikutnya dilewati",
      scanner.collect_and_rank() == [], "throttle tidak bekerja")

db.remove_pending_pool(CA1L)
r6 = scanner.collect_and_rank()
check("final: keluar antrean → seen_call memblokir permanen", r6 == [], str(r6))

# status gate yang masih bisa berubah = retryable
check("_retryable_status: age_ → True", scanner._retryable_status("skip_fast:age_4s") is True)
check("_retryable_status: liq_rendah → True",
      scanner._retryable_status("skip_fast:liq_rendah") is True)
check("_retryable_status: filtered:liq → True",
      scanner._retryable_status("filtered:liq") is True)
check("_retryable_status: filtered:mcap → False",
      scanner._retryable_status("filtered:mcap") is False)
check("_retryable_status: bought → False",
      scanner._retryable_status("bought") is False)

# ======================================================================
# 6) Restore mock & DB — jebakan lain tidak dikotamin
# ======================================================================
scanner.dx.best_pair = _orig_best_pair
scanner.dx.profile_candidates = _orig_fetch_profiles
scanner.fetch_multipool_candidates = _orig_fetch_multipool
scanner._make_w3 = _orig_make_w3
scanner.market_ok = _orig_market_ok

print()
print(f"TOTAL: {len(PASS)} OK, {len(FAIL)} FAIL")
if FAIL:
    print(" FAIL:")
    for f in FAIL:
        print("   -", f)
    sys.exit(1)
