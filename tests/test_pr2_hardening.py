"""Regresi untuk 3 perbaikan review PR #2 (Haikal Javier).

Yang dijaga:
1) `buy_slippage` TIDAK dimigrasi otomatis oleh db.init().
   Mengubah toleransi slippage = mengubah risiko modal, jadi harus lewat
   keputusan operator (panel Settings), bukan efek samping restart.
2) `_permanent_revert` tidak boleh menelan error transport.
   403/405 "method not allowed", 429 rate-limit, timeout, RPC error bukan
   sifat token — percobaan kedua (min_out=1) masih punya peluang berhasil.
   Kalau ini dianggap permanen, bot berhenti menjual token yang sehat.
3) Posisi `closed_stuck` bisa dilihat & dipicu ulang dari panel Posisi.
   Tanpa ini janji "tekan jual manual untuk reset" di notifikasi macet
   tidak punya tombol yang bisa ditekan.

Jalankan: python3 tests/test_pr2_hardening.py
"""
import asyncio
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK' if cond else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not cond else ""))


# ======================================================================
print("\n[1] buy_slippage tidak dimigrasi senyap")
# DB "lama" dengan nilai operator 0.60 — harus dipertahankan.
_p = tempfile.mktemp(suffix=".db")
_c = sqlite3.connect(_p)
_c.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
_c.executemany("INSERT INTO settings VALUES(?,?)",
               [("buy_slippage", "0.60"), ("scanner_interval_sec", "15")])
_c.commit()
_c.close()

# config.DB_PATH dibaca saat import, jadi set dulu sebelum import sniper.db
os.environ["DB_PATH"] = _p
for m in [k for k in list(sys.modules) if k.startswith("sniper")]:
    del sys.modules[m]

from sniper import db  # noqa: E402

db.init()
_c = sqlite3.connect(_p)
slip = _c.execute("SELECT value FROM settings WHERE key='buy_slippage'").fetchone()[0]
scanner = _c.execute("SELECT value FROM settings WHERE key='scanner_interval_sec'").fetchone()[0]
_c.close()
check("buy_slippage operator (0.60) dipertahankan", slip == "0.60", f"jadi {slip}")
check("scanner_interval_sec tetap dimigrasi 15 -> 5", scanner == "5", f"jadi {scanner}")

# Instalasi baru tetap dapat default baru 0.25
_p2 = tempfile.mktemp(suffix=".db")
os.environ["DB_PATH"] = _p2
for m in [k for k in list(sys.modules) if k.startswith("sniper")]:
    del sys.modules[m]
from sniper import db as db2  # noqa: E402

db2.init()
_c = sqlite3.connect(_p2)
fresh = _c.execute("SELECT value FROM settings WHERE key='buy_slippage'").fetchone()[0]
_c.close()
check("instalasi baru pakai default 0.25", fresh == "0.25", f"jadi {fresh}")

# ======================================================================
print("\n[2] _permanent_revert: revert kontrak saja, bukan error transport")
for m in [k for k in list(sys.modules) if k.startswith("sniper")]:
    del sys.modules[m]
from sniper.trader import _permanent_revert  # noqa: E402

PERMANENT = [
    "ContractLogicError: execution reverted: ERC20: transfer not allowed",
    "ContractLogicError: execution reverted: token is frozen",
    "ContractLogicError: execution reverted: Address is blacklisted",
    "ContractLogicError: execution reverted: TOKEN: NOT TRANSFERABLE",
    "ContractLogicError: execution reverted: transfer is paused",
    "ContractLogicError: execution reverted: Trading disabled",
    "ContractLogicError: execution reverted: transfers are disabled",
    # Varian tanpa prefix `execution reverted` — ini yang bikin percobaan
    # pertama (wajib konteks EVM) gagal menangkap revert permanen.
    "Address is Blacklisted",
    "Transfers disabled by owner",
    "trading paused",
    # ⚠️ Jebakan: pesan revert sah yang kebetulan memuat kata/angka mirip
    # transport. Hint transport harus spesifik supaya revert permanen TIDAK
    # dibaca sebagai gangguan sementara (kalau salah → RPC dibanjiri lagi).
    "execution reverted: transfers disabled; rpc=https://x.io",
    "execution reverted: token frozen (connection id 7)",
    "execution reverted: Trading is disabled (block 405)",
    "execution reverted: token is frozen for 403 days",
]
TRANSIENT = [
    "HTTPError: 403 Client Error: Forbidden for url: https://rpc.example",
    "HTTPError: 405 method not allowed",
    "Too Many Requests: rate limit exceeded",
    "ConnectionError: HTTPSConnectionPool read timed out",
    "ReadTimeout: max retries exceeded at https://rpc.example",
    "Web3RPCError: nonce too low",
    "Web3RPCError: gas required exceeds allowance",
    "ContractLogicError: insufficient output amount",
    "ContractLogicError: execution reverted: TransferHelper: TRANSFER_FROM_FAILED",
    "ContractLogicError: execution reverted",   # generik → panic exit masih boleh
]
for s in PERMANENT:
    check(f"permanen: {s[-38:]}", _permanent_revert(Exception(s)))
for s in TRANSIENT:
    check(f"transien: {s[:38]}", not _permanent_revert(Exception(s)))

# ======================================================================
print("\n[3] posisi closed_stuck tampil di panel + tombol coba jual")
_p3 = tempfile.mktemp(suffix=".db")
os.environ["DB_PATH"] = _p3
for m in [k for k in list(sys.modules) if k.startswith("sniper")]:
    del sys.modules[m]
from sniper import db as db3, control_bot, engine  # noqa: E402

db3.init()


async def _fake_pair(pos):
    return {"priceUsd": "0.002", "priceNative": "0.000001"}


engine.current_pair = _fake_pair


def _open(sym, ca_byte):
    db3.open_position(ca="0x" + ca_byte * 40, symbol=sym,
                      pair_address="0x" + ca_byte * 39 + "f",
                      chain_slug="robinhood", pool_type="v4", fee=3000,
                      entry_price_usd=1e-6, tokens_total=1000, tokens_left=1000,
                      eth_spent=0.01, usd_spent=10, honeypot=0,
                      source_name="T", ts=0, is_simulated=1)
    return db3.open_positions()[0]["id"]


_open("HEALTHY", "c")
pid = _open("STUCKT", "a")
db3.update_position(pid, status="closed_stuck", tokens_left=880, closed_at=1000)

check("stuck_positions() mengembalikan 1 baris", len(db3.stuck_positions()) == 1)
check("open_positions() tidak mencampur stuck",
      all(p["status"] == "open" for p in db3.open_positions()))

text, buttons = asyncio.run(control_bot.positions_panel())
labels = [b.text for row in buttons for b in row]
check("panel menyebut posisi macet", "Macet" in text, text[:200])
check("tombol coba jual ada", any("Coba jual" in x for x in labels), str(labels))
check("posisi sehat tetap punya tombol jual",
      any("HEALTHY" in x for x in labels), str(labels))

# umur tidak dikarang dari timestamp epoch-0
check("closed_at tak wajar tidak bikin umur ngawur", "m lalu" not in text, text[:200])

# tombol recovery benar-benar mengubah status (bukan no-op)
asyncio.run(engine.execute_sell(pid, 1.0, "manual"))
after = db3.position(pid)["status"]
check("klik jual manual memproses ulang posisi stuck",
      after == "closed_unsellable", f"status jadi {after}")

# ======================================================================
print(f"\n=== PASS={len(PASS)} FAIL={len(FAIL)} ===")
if FAIL:
    for f in FAIL:
        print("  GAGAL:", f)
    sys.exit(1)
