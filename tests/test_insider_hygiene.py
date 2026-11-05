"""🧪 Uji higiene tabel insider (v16.12) — TANPA jaringan.

Lingkup:
  1. Wallet yang membiayai >= N deployer DITOLAK saat rekrutmen (add_insider_wallet).
  2. add_insider_buy TIDAK mencatat beli dari wallet funder (bukan sinyal organik).
  3. add_insider_buy meng-update last_seen wallet insider (basis prune).
  4. purge_operator_funders: nonaktifkan + blacklist + hapus insider_buys.
  5. prune_stale_insiders BENAR-BENAR jalan setelah last_seen terisi:
     wallet tak aktif > 3 hari & win_count < 2 → enabled=0.
  6. Wallet bagus (win >= 2) TIDAK ikut ke-prune meski lama tak terlihat.
"""
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper import config, db  # noqa: E402

LAUNCHER = "0x" + "1a" * 20   # membiayai 3 deployer → funder/launcher
HONEST = "0x" + "2b" * 20     # trader biasa
GOOD = "0x" + "3c" * 20       # trader bagus (win 5)
CA1 = "0x" + "aa" * 20


def _fresh_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old = config.DB_PATH
    config.DB_PATH = tmp.name
    db.init()
    with db._lock, db._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS operator_funders(
            funder TEXT, deployer TEXT, ca TEXT, status TEXT DEFAULT 'ok',
            ts INTEGER, PRIMARY KEY (funder, deployer))""")
        for i in range(3):
            c.execute("INSERT OR IGNORE INTO operator_funders(funder,deployer,ca,ts) "
                      "VALUES(?,?,?,?)",
                      (LAUNCHER, "0x" + f"{i:040x}", CA1, int(time.time())))
    return tmp.name, old


def _restore(path, old):
    config.DB_PATH = old
    try:
        Path(path).unlink()
    except OSError:
        pass


def test_funder_rejected_on_recruit():
    path, old = _fresh_db()
    try:
        assert db.add_insider_wallet(LAUNCHER, label="auto") is False, "funder harus ditolak"
        assert db.add_insider_wallet(HONEST, label="auto") is True, "trader biasa harus lolos"
        addrs = {r["address"] for r in db.list_insiders()}
        assert LAUNCHER not in addrs and HONEST in addrs
        print("PASS funder ditolak saat rekrutmen, trader biasa lolos")
    finally:
        _restore(path, old)


def test_funder_buy_not_recorded():
    path, old = _fresh_db()
    try:
        db.add_insider_wallet(HONEST, label="auto")
        # funder belum tentu ada di tabel insider, tapi belinya tetap harus ditolak
        db.record_insider_buy(CA1, LAUNCHER, symbol="RUG", mcap=15000, liq=12000)
        db.record_insider_buy(CA1, HONEST, symbol="RUG", mcap=15000, liq=12000)
        with db._lock, db._conn() as c:
            wallets = [r[0] for r in c.execute("SELECT wallet FROM insider_buys")]
        assert LAUNCHER not in wallets, "beli funder tidak boleh tercatat"
        assert HONEST in wallets, "beli trader biasa harus tercatat"
        print("PASS beli dari funder tidak masuk insider_buys")
    finally:
        _restore(path, old)


def test_buy_updates_last_seen():
    path, old = _fresh_db()
    try:
        db.add_insider_wallet(HONEST, label="auto")
        with db._lock, db._conn() as c:
            c.execute("UPDATE insider_wallets SET last_seen=0 WHERE address=?", (HONEST,))
        t0 = int(time.time())
        db.record_insider_buy(CA1, HONEST, symbol="X", mcap=1, liq=1)
        with db._lock, db._conn() as c:
            ls = c.execute("SELECT last_seen FROM insider_wallets WHERE address=?",
                           (HONEST,)).fetchone()[0]
        assert ls >= t0, f"last_seen harus terisi (dapat {ls}, harap >= {t0})"
        print(f"PASS last_seen ter-update saat wallet terlihat beli (delta {ls - t0}s)")
    finally:
        _restore(path, old)


def test_purge_operator_funders():
    path, old = _fresh_db()
    try:
        # paksa masuk lewat INSERT langsung (menembus guard rekrutmen)
        with db._lock, db._conn() as c:
            db._init_insider_wallets(c)
            c.execute("""INSERT OR REPLACE INTO insider_wallets
                         (address,label,win_count,enabled,added_at,last_seen)
                         VALUES(?,?,?,?,?,?)""",
                      (LAUNCHER, "auto", 3, 1, int(time.time()), int(time.time())))
        db.record_insider_buy(CA1, HONEST, symbol="X", mcap=1, liq=1)  # baris pembanding
        res = db.purge_operator_funders()
        assert res["banned"] == 1, f"harus 1 wallet di-ban, dapat {res}"
        assert db.is_sybil_blacklisted(LAUNCHER) is True
        with db._lock, db._conn() as c:
            en = c.execute("SELECT enabled,label FROM insider_wallets WHERE address=?",
                           (LAUNCHER,)).fetchone()
        assert en["enabled"] == 0 and en["label"] == "operator_funder"
        # setelah di-ban, tidak bisa direkrut lagi
        assert db.add_insider_wallet(LAUNCHER) is False
        print("PASS purge_operator_funders: nonaktif + blacklist + tidak bisa direkrut ulang")
    finally:
        _restore(path, old)


def test_prune_now_works_and_protects_winners():
    path, old = _fresh_db()
    try:
        now = int(time.time())
        stale = now - 5 * 86400
        with db._lock, db._conn() as c:
            db._init_insider_wallets(c)
            for addr, win in ((HONEST, 0), (GOOD, 5)):
                c.execute("""INSERT OR REPLACE INTO insider_wallets
                             (address,label,win_count,enabled,added_at,last_seen)
                             VALUES(?,?,?,?,?,?)""",
                          (addr, "auto", win, 1, stale, stale))
        n = db.prune_stale_insiders(3)
        assert n == 1, f"hanya wallet boncos yang boleh ke-prune, dapat {n}"
        with db._lock, db._conn() as c:
            st = {r["address"]: r["enabled"] for r in
                  c.execute("SELECT address,enabled FROM insider_wallets")}
        assert st[HONEST] == 0, "wallet lunglai (win<2, 5 hari) harus dinonaktifkan"
        assert st[GOOD] == 1, "wallet bagus (win>=2) harus tetap aktif"
        print("PASS prune jalan (last_seen terisi) & wallet bagus dilindungi")
    finally:
        _restore(path, old)


if __name__ == "__main__":
    test_funder_rejected_on_recruit()
    test_funder_buy_not_recorded()
    test_buy_updates_last_seen()
    test_purge_operator_funders()
    test_prune_now_works_and_protects_winners()
    print("\n✅ SEMUA TES HIGIENE INSIDER LULUS")
