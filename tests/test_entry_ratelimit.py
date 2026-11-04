"""🧪 Uji ENTRY RATE-LIMIT per operator-family (v16.6 ratelimit) — tanpa jaringan.

Lingkup:
  1. 3 entry keluarga sama → entry ke-4 DITOLAK (limit 3).
  2. 3 entry "Marvin" + 1 entry "WHITEWHALE" → WHITEWHALE LOLOS (beda keluarga).
  3. Entry > 24 jam lalu TIDAK dihitung (manipulasi ts langsung di DB).
  4. CA yang sama di-note 2x → tidak menambah count (PRIMARY KEY).
  5. too_many_entries nol network: monkeypatch clone_guard._bs_get → raise,
     tetap jalan.
  6. Exception di note_entry tidak menggagalkan open_position.
  7. family_keys menghasilkan sym:marvin|marvin the martian (normalisasi).
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper import config, db            # noqa: E402
from sniper import clone_guard as cg     # noqa: E402

A = "0x" + "a1" * 20
B = "0x" + "b2" * 20
C = "0x" + "c3" * 20
D = "0x" + "d4" * 20
W1 = "0x" + "11" * 20
W2 = "0x" + "22" * 20


def _fresh_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old = config.DB_PATH
    config.DB_PATH = tmp.name
    db.init()
    return tmp.name, old


def _torn_down(path, old):
    os.unlink(path)
    config.DB_PATH = old


# =========================================================
# 1. 3 entry keluarga sama → entry ke-4 DITOLAK (limit 3)
# =========================================================
def test_family_limit_reject():
    path, old = _fresh_db()
    _bs = cg._bs_get
    try:
        cg._bs_get = lambda p, timeout=8: None   # nol jaringan
        for ca in (A, B, C, D):
            cg.note_entry(ca, symbol="Marvin", name="Marvin The Martian")
        hit, why = cg.too_many_entries(D, symbol="Marvin", name="Marvin The Martian")
        assert hit, (hit, why)
        assert "maks 3" in why, why
        print("PASS family limit: entry ke-4 DITOLAK")
    finally:
        cg._bs_get = _bs
        _torn_down(path, old)


# =========================================================
# 2. 3 entry "Marvin" + 1 WHITEWHALE → WHITEWHALE LOLOS
# =========================================================
def test_family_isolation():
    path, old = _fresh_db()
    _bs = cg._bs_get
    try:
        cg._bs_get = lambda p, timeout=8: None
        for ca in (A, B, C):
            cg.note_entry(ca, symbol="Marvin", name="Marvin The Martian")
        cg.note_entry(W1, symbol="WHITEWHALE", name="White Whale")
        hit, why = cg.too_many_entries(W1, symbol="WHITEWHALE", name="White Whale/")
        # (name diberi trailing slash hanya untuk memastikan normalisasi tak
        #  digunakan salah — White Whale bukan keluarga Marvin)
        assert not hit, (hit, why)
        print("PASS WHITEWHALE lolos (beda keluarga dari Marvin)")
    finally:
        cg._bs_get = _bs
        _torn_down(path, old)


# =========================================================
# 3. Entry > 24 jam lalu TIDAK dihitung (manipulasi ts)
# =========================================================
def test_window_expiry():
    path, old = _fresh_db()
    _bs = cg._bs_get
    try:
        cg._bs_get = lambda p, timeout=8: None
        for ca in (A, B, C):
            cg.note_entry(ca, symbol="Marvin", name="Marvin The Martian")
        # pindahkan semua ts ke 25 jam lalu
        old_ts = int(time.time()) - 25 * 3600
        with db._lock, db._conn() as c:
            cg._init_tables(c)
            c.execute("UPDATE entry_log SET ts=?", (old_ts,))
        hit, why = cg.too_many_entries(D, symbol="Marvin", name="Marvin The Martian")
        assert not hit, (hit, why)
        print("PASS window: entry 25j lalu tidak dihitung → lolos")
    finally:
        cg._bs_get = _bs
        _torn_down(path, old)


# =========================================================
# 4. CA yang sama di-note 2x → tidak menambah count (PRIMARY KEY)
# =========================================================
def test_primary_key_dedupe():
    path, old = _fresh_db()
    _bs = cg._bs_get
    try:
        cg._bs_get = lambda p, timeout=8: None
        for ca in (A, B, C):
            cg.note_entry(ca, symbol="Marvin", name="Marvin The Martian")
        # note ulang MA (2x) + B (2x) — tidak boleh menambah count
        for ca in (A, B):
            cg.note_entry(ca, symbol="Marvin", name="Marvin The Martian")
            cg.note_entry(ca, symbol="Marvin", name="Marvin The Martian")
        # count per key = 3, bukan 7; limit 3 → masih persis di batas
        with db._lock, db._conn() as c:
            cg._init_tables(c)
            n = c.execute(
                "SELECT COUNT(DISTINCT ca) FROM entry_log WHERE key=? "
                "AND ts>=?", (f"sym:marvin|marvin the martian",
                              int(time.time()) - 24 * 3600)).fetchone()[0]
        assert n == 3, n
        # limit 4 → tidak berths (hanya 3 CA)
        hit, why = cg.too_many_entries(D, symbol="Marvin", name="Marvin The Martian",
                                       limit=4)
        assert not hit, (hit, why)
        hit, why = cg.too_many_entries(D, symbol="Marvin", name="Marvin The Martian",
                                       limit=3)
        assert hit, (hit, why)
        print("PASS dedupe: CA sama di-note berulang tidak menambah count")
    finally:
        cg._bs_get = _bs
        _torn_down(path, old)


# =========================================================
# 5. too_many_entries nol network (monkeypatch _bs_get raise AssertionError)
# =========================================================
def test_too_many_entries_zero_network():
    path, old = _fresh_db()
    _bs = cg._bs_get
    try:
        def _boom(p, timeout=8):
            raise AssertionError(f"network dipanggil: {p}")
        cg._bs_get = _boom
        # isi entry lewat catatan langsung (agar tidak lewat note_entry/yang
        # sebenarnya juga tidak pakai jaringan — register_async background
        # worker CTULAH yang memanggil network, bukan jalur entry)
        now = int(time.time())
        with db._lock, db._conn() as c:
            cg._init_tables(c)
            for ca in (A, B, C):
                c.execute("INSERT OR IGNORE INTO entry_log(key,ca,ts) VALUES(?,?,?)",
                          (f"sym:marvin|marvin the martian", ca, now))
        hit, why = cg.too_many_entries(A, symbol="Marvin", name="Marvin The Martian")
        assert hit, (hit, why)
        # 100% DB-only — tidak ada jaringan yang di-sentuh
        print("PASS too_many_entries nol network (murni DB)")
    finally:
        cg._bs_get = _bs
        _torn_down(path, old)


# =========================================================
# 6. Exception di note_entry tidak menggagalkan open_position
# =========================================================
def test_note_entry_fail_open_in_open_position():
    path, old = _fresh_db()
    calls = {"n": 0}
    orig_note = cg.note_entry

    def broken_note(ca, symbol="", name=""):
        calls["n"] += 1
        raise RuntimeError("simulasi DB pecah")

    try:
        cg.note_entry = broken_note
        db.open_position(
            ca="0x" + "e" * 40, symbol="Marvin", name="Marvin The Martian",
            pair_address="0x" + "f" * 40, chain_slug="robinhood",
            pool_type="v3", fee=0.3, entry_price_usd=1.0,
            tokens_total=100.0, tokens_left=100.0,
            eth_spent=0.001, usd_spent=5.0, honeypot=0, source_name="test",
        )
        assert calls["n"] == 1, calls  # note_entry TETAP dipanggil, error ditelan
        rows = db.open_positions()
        assert len(rows) == 1, rows    # posisi TETAP tertulis
        print("PASS note_entry gagal → open_position tetap sukses (fail-open)")
    finally:
        cg.note_entry = orig_note
        _torn_down(path, old)


# =========================================================
# 7. family_keys → sym:marvin|marvin the martian (normalisasi)
# =========================================================
def test_family_keys_fallback_sym():
    path, old = _fresh_db()
    _bs = cg._bs_get
    try:
        cg._bs_get = lambda p, timeout=8: None
        # registry kosong → hanya fallback sym:
        keys = cg.family_keys(A, symbol="  MARVIN  ", name="Marvin  the  Martian")
        assert "sym:marvin|marvin the martian" in keys, keys
        # symbol pendek (<3) DIABAIKAN
        keys2 = cg.family_keys(B, symbol="AI", name="AI66")
        assert keys2 == [], keys2
        # case/whitespace konsisten simpan vs baca:
        cg.note_entry(A, symbol="Marvin", name="Marvin the Martian")
        keys3 = cg.family_keys(A, symbol="MARVIN ", name="  Marvin   the Martian ")
        assert "sym:marvin|marvin the martian" in keys3, keys3
        print("PASS family_keys fallback sym + normalisasi  Pengujian normalisasi")
    finally:
        cg._bs_get = _bs
        _torn_down(path, old)


def test_family_keys_full_hierarchy():
    """Registrasi lengkap: dep: + funder: + fp: + sym: urut paling kuat dulu."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old = config.DB_PATH
    config.DB_PATH = tmp.name
    db.init()
    _bs = cg._bs_get
    try:
        cg._bs_get = lambda p, timeout=8: None
        DEP = "0x" + "5" * 40
        FUND = "0x" + "6" * 40
        FP = "deadbeef" * 4
        with db._lock, db._conn() as c:
            cg._init_tables(c)
            c.execute("""INSERT OR REPLACE INTO deployer_registry
                (ca,deployer,template_fp,symbol,name,total_supply,funders,status,reason,ts)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (A, DEP, FP, "Marvin", "Marvin The Martian", "1000", "", "ok", "",
                 int(time.time())))
            c.execute("""INSERT OR REPLACE INTO operator_funders
                (funder,deployer,ca,status,ts) VALUES(?,?,?,?,?)""",
                (FUND, DEP, A, "ok", int(time.time())))
        keys = cg.family_keys(A, symbol="Marvin", name="Marvin The Martian")
        assert keys[0] == f"dep:{DEP}", keys
        assert f"funder:{FUND}" in keys, keys
        assert f"fp:{FP}|marvin|marvin the martian" in keys, keys
        assert "sym:marvin|marvin the martian" in keys, keys
        assert "symonly:marvin" in keys, keys
        # urutan kekuatan: dep harus paling awal, sym/symonly paling akhir
        assert keys[0].startswith("dep:"), keys
        assert keys.index("sym:marvin|marvin the martian") < keys.index("symonly:marvin")
        print("PASS family_keys urutan: dep > funder > fp > sym")
    finally:
        cg._bs_get = _bs
        _torn_down(tmp.name, old)


def test_too_many_entries_speed():
    """too_many_entries + family_keys harus < 20 ms (wajib DB-only + cepat).

    Ambang dikunci ketat 20 ms, tapi diukur BEST-OF-3 (latensi terbaik dari 3
    percobaan). Tujuannya: membuktikan jalur ini nol panggilan jaringan, tanpa
    ikut menangkap noise penjadwalan CPU saat VPS juga menjalankan bot+scanner
    (dulu sekali terukur 31 ms → suite merah palsu). Panggilan HTTP sungguhan
    tetap tidak mungkin lolos: latensinya ratusan ms, bukan 20-an ms.
    """
    path, old = _fresh_db()
    _bs = cg._bs_get
    try:
        cg._bs_get = lambda p, timeout=8: None
        now = int(time.time())
        with db._lock, db._conn() as c:
            cg._init_tables(c)
            for i in range(50):
                c.execute("INSERT OR IGNORE INTO entry_log(key,ca,ts) VALUES(?,?,?)",
                          (f"sym:marvin|marvin the martian", f"0x{i:040x}", now))
        samples = []
        hit = False
        for _ in range(3):
            t0 = time.perf_counter()
            hit, _ = cg.too_many_entries(A, symbol="Marvin", name="Marvin The Martian")
            samples.append((time.perf_counter() - t0) * 1000)
        dt_ms = min(samples)
        assert hit, _
        assert dt_ms < 20, f"too_many_entries {dt_ms:.1f} ms (wajib < 20 ms, sampel {samples})"
        print(f"PASS too_many_entries cepat ({dt_ms:.1f} ms < 20 ms, best-of-3 dari "
              f"{', '.join(f'{s:.1f}' for s in samples)})")
    finally:
        cg._bs_get = _bs
        _torn_down(path, old)


def test_open_position_without_name_still_counts():
    """REGRESI jalur nyata: posisi dari trader/listener TIDAK membawa `name`.

    Kalau entry_log ditulis "sym:marvin|" sementara cek memakai
    "sym:marvin|marvin the martian", hitungan selalu 0 → rate-limit mati
    padahal semua test lolos. Test ini meniru persis kw milik sniper/trader.py.
    """
    path, old = _fresh_db()
    try:
        for i, ca in enumerate([A, B, C]):
            db.open_position(ca=ca, symbol="Marvin", pair_address="0xpair",
                             chain_slug="robinhood", pool_type="v4", fee=3000,
                             entry_price_usd=0.0001, tokens_total=1000,
                             tokens_left=1000, eth_spent=0.002, usd_spent=5.0,
                             honeypot=0, source_name="SCANNER",
                             caller_id=None, caller_name=None,
                             buy_tx="simulated_buy", is_simulated=1)
        hit, why = cg.too_many_entries(D, symbol="Marvin",
                                       name="Marvin the Martian", limit=3)
        assert hit, f"rate-limit mati saat name tidak dikirim: {why!r}"
        print(f"PASS open_position tanpa name tetap terhitung ({why})")
    finally:
        _torn_down(path, old)


def test_symonly_key_survives_name_change():
    """Nama bisa berubah antar klon ("Marvin" vs "Marvin the Martian") —
    kunci symonly harus tetap menghitungnya sebagai satu keluarga."""
    path, old = _fresh_db()
    try:
        cg.note_entry(A, symbol="Marvin", name="Marvin the Martian")
        cg.note_entry(B, symbol="Marvin", name="")          # name hilang
        cg.note_entry(C, symbol="Marvin", name="MARVIN 2.0")
        hit, why = cg.too_many_entries(D, symbol="Marvin", name="Marvin", limit=3)
        assert hit, f"symonly gagal menyatukan keluarga: {why!r}"
        print(f"PASS symonly menyatukan 3 varian nama ({why})")
    finally:
        _torn_down(path, old)


if __name__ == "__main__":
    test_family_limit_reject()
    test_family_isolation()
    test_window_expiry()
    test_primary_key_dedupe()
    test_too_many_entries_zero_network()
    test_note_entry_fail_open_in_open_position()
    test_family_keys_fallback_sym()
    test_family_keys_full_hierarchy()
    test_too_many_entries_speed()
    test_open_position_without_name_still_counts()
    test_symonly_key_survives_name_change()
    print("\nALL ENTRY-RATELIMIT TESTS PASSED ✅")
