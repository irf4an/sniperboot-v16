"""🧪 Uji guard dompet non-trader (burn/zero) — tanpa jaringan.

Temuan produksi: 0x000…dead masuk daftar insider lewat auto_rotate dan
mengisi 18.007 baris insider_buys (60,5% tabel, 1.337 CA). Alamat burn
tidak punya private key, jadi mustahil benar-benar membeli.

Lingkup:
  1. is_trackable_wallet menolak zero / …dead / 0xdead… / precompile / format salah.
  2. is_trackable_wallet menerima alamat normal.
  3. record_insider_buy mengabaikan wallet burn.
  4. top_insider_buys menyaring baris lama (yang ditulis lewat SQL langsung).
  5. add_insider_wallet menolak merekrut burn address.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper import config, db   # noqa: E402

BURN = "0x000000000000000000000000000000000000dead"
ZERO = "0x0000000000000000000000000000000000000000"
PRECOMPILE = "0x0000000000000000000000000000000000000001"
DEAD_VAR = "0xdead000000000000000042069420694206942069"
GOOD = "0x2c4563d12e5d73784c9d1835b00ccfac439b97f1"
CA = "0x" + "ab" * 20


def _fresh_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old = config.DB_PATH
    config.DB_PATH = tmp.name
    db.init()
    return tmp.name, old


def _raw_insert(ca, wallet, ts):
    """Tulis langsung ke SQL, meniru data lama sebelum guard dipasang."""
    import sqlite3
    db.top_insider_buys(window_sec=60, limit=1)   # pastikan tabel sudah dibuat
    c = sqlite3.connect(config.DB_PATH)
    c.execute("INSERT INTO insider_buys(ca,wallet,symbol,mcap,liq,pair_address,ts) "
              "VALUES(?,?,?,?,?,?,?)", (ca, wallet, "OLD", 0, 0, "", ts))
    c.commit()
    c.close()


def test_detector():
    for bad in (BURN, ZERO, PRECOMPILE, "", None, "0x123", "not-an-address"):
        assert not db.is_trackable_wallet(bad), bad
    # vanity 0xdead… (mis. 0xdeadbeef…) bisa jadi dompet sungguhan,
    # jadi TIDAK diblokir — hanya pola burn 0x000…dead / 0xdead+36 nol.
    assert db.is_trackable_wallet(GOOD)
    assert db.is_trackable_wallet(GOOD.upper().replace("0X", "0x"))
    print("PASS detektor menolak burn/zero/precompile, menerima dompet normal")


def test_record_ignores_burn():
    path, old = _fresh_db()
    try:
        now = int(time.time())
        db.record_insider_buy(CA, BURN, symbol="X", ts=now)
        db.record_insider_buy(CA, GOOD, symbol="X", ts=now)
        import sqlite3
        c = sqlite3.connect(config.DB_PATH)
        n = c.execute("SELECT COUNT(*) FROM insider_buys").fetchone()[0]
        w = [r[0] for r in c.execute("SELECT wallet FROM insider_buys")]
        c.close()
        assert n == 1 and w == [GOOD], (n, w)
        print("PASS ingestion mengabaikan wallet burn")
    finally:
        os.unlink(path)
        config.DB_PATH = old


def test_top_filters_legacy_rows():
    path, old = _fresh_db()
    try:
        now = int(time.time())
        for _ in range(50):
            _raw_insert(CA, BURN, now - 5)
        _raw_insert(CA, GOOD, now - 10)
        rows = db.top_insider_buys(window_sec=3600, limit=10, now=now)
        assert rows and rows[0]["buy_count"] == 1, rows
        assert rows[0]["wallet_count"] == 1, rows
        print("PASS panel menyaring baris burn lama (50 baris palsu diabaikan)")
    finally:
        os.unlink(path)
        config.DB_PATH = old


def test_add_wallet_refuses_burn():
    path, old = _fresh_db()
    try:
        assert db.add_insider_wallet(BURN, label="auto_rotate") in (False, None)
        addrs = db.get_active_insiders()
        assert BURN not in [a.lower() for a in addrs], addrs
        db.add_insider_wallet(GOOD, label="manual")
        assert GOOD in [a.lower() for a in db.get_active_insiders()]
        print("PASS rotasi tidak bisa merekrut burn address")
    finally:
        os.unlink(path)
        config.DB_PATH = old


if __name__ == "__main__":
    test_detector()
    test_record_ignores_burn()
    test_top_filters_legacy_rows()
    test_add_wallet_refuses_burn()
    print("\n✅ SEMUA TES GUARD DOMPET LULUS")
