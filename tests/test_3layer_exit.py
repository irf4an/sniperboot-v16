"""Uji Skema Exit 3 Lapis (Opsi B):
  Lapis 1: TP1 → jual 50% posisi awal (tp1_done=1, posisi tetap OPEN)
  Lapis 2: TP2 → jual 50% dari SISA (tp2_done=1, posisi tetap OPEN moonbag)
  Lapis 3: Moonbag → trailing stop 20% dari puncak, TANPA batas TP,
           DIBEBASKAN dari time-stop (max_hold_sec).

Tidak menyentuh jaringan/Web3/Telethon — engine.db dipatching FakeDB
(pola mengikuti test_time_stop_and_intervals.py). Database asli
hoodsniper.db TIDAK disentuh.
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper import config, engine


TOKENS_AWAL = 1000.0


def _make_pos(ts=None, tp1_done=0, tp2_done=None, tokens_left=TOKENS_AWAL):
    pos = {
        "id": 1, "status": "open", "symbol": "TEST", "ca": "0x" + "a" * 40,
        "pair_address": "0x" + "b" * 40, "chain_slug": "robinhood",
        "entry_price_usd": 0.001, "tokens_total": TOKENS_AWAL,
        "tokens_left": tokens_left,
        "eth_spent": 0.01, "usd_spent": 5.0, "tp1_done": tp1_done,
        "is_simulated": 1, "ts": int(ts if ts is not None else time.time()),
        "peak_price": 0.001,
    }
    if tp2_done is not None:
        pos["tp2_done"] = tp2_done
    return pos


class FakeDB:
    def __init__(self, settings=None, pos=None):
        self.settings = settings or {}
        self.updates = []
        self.pos = pos or _make_pos()

    def get(self, key, cast=float):
        v = self.settings.get(key, config.SETTING_DEFAULTS.get(key, 0))
        try:
            return cast(v)
        except (TypeError, ValueError):
            return cast(0)

    def position(self, pid):
        return dict(self.pos) if pid == self.pos["id"] else None

    def update_position(self, pid, **fields):
        self.updates.append(fields)
        self.pos.update(fields)

    def add_realized(self, pid, eth):
        pass

    def mark_decision_outcome(self, ca, outcome):
        pass

    def stats(self):
        return {"tp2": 0, "sl": 0, "manual": 0, "rug": 0, "trail": 0, "timeout": 0}

    def count_closed(self):
        return 7

    def add_paper_cash(self, amount):
        return 100.0

    def statuses(self):
        return [u["status"] for u in self.updates if "status" in u]


def _install_engine(fakedb, sent, price=0.001):
    class FakeTrader:
        def sell(self, pos, fraction, price_native):
            sold = pos["tokens_left"] * fraction
            return (sold, pos["eth_spent"] * fraction, 0.0, "simulated_sell")
        def exec_pnl(self, pos, fraction):
            return 1.0

    async def fake_pair(pos):
        return {"priceUsd": str(price), "priceNative": "0.001",
                "liquidity": {"usd": 50000}}

    async def notify(text, *a, **kw):
        sent.append(str(text))

    engine.db = fakedb
    engine._trader = FakeTrader()
    engine.current_pair = fake_pair
    engine.bind(engine._trader, notify)
    engine._thin_warned.clear()
    engine._liq_last.clear()
    engine._cleanup_marks.clear()
    return fakedb


def _last_update_value(fakedb, key):
    """Nilai key terakhir dari update_position (None jika tak pernah diset)."""
    val = None
    for u in fakedb.updates:
        if key in u:
            val = u[key]
    return val


# ---------- Lapis 1: TP1 jual 50% posisi awal ----------

def test_layer1_tp1_sells_50pct_position_stays_open():
    # harga +50% → pnl 0.5 >= tp1 0.5, belum tp1_done
    sent = []
    pos = _make_pos(tokens_left=1000.0)
    pos["tp1_x"] = 0.5
    db = FakeDB({"tp1": "0.5", "tp2": "1.0", "tp1_sell_pct": "0.50",
                 "tp_verify": "0"}, pos=pos)
    _install_engine(db, sent, price=0.0015)
    asyncio.run(engine._check(db.pos))
    # update terakhir tokens_left = 500 (jual 500 dari 1000)
    assert _last_update_value(db, "tokens_left") == 500.0, db.updates
    assert _last_update_value(db, "tp1_done") == 1, db.updates
    assert "status" not in db.updates[-1], db.updates  # tetap OPEN
    assert db.pos["status"] == "open", db.pos
    assert any("TP1" in s for s in sent), sent


# ---------- Lapis 2: TP2 jual 50% dari sisa, jadi moonbag ----------

def test_layer2_tp2_sells_half_of_remainder_marks_tp2_done_stays_open():
    # posisi sudah tp1_done, sisa 500, pnl >= tp2 → jual 50% x 500 = 250
    sent = []
    pos = _make_pos(tokens_left=500.0, tp1_done=1, tp2_done=0)
    db = FakeDB({"tp2": "1.0", "tp2_on": "1", "tp2_sell_pct": "0.50",
                 "moonbag_trail_pct": "0.20", "tp_verify": "0"}, pos=pos)
    _install_engine(db, sent, price=0.0025)  # +150% > tp2
    asyncio.run(engine._check(db.pos))
    assert _last_update_value(db, "tokens_left") == 250.0, db.updates
    assert _last_update_value(db, "tp2_done") == 1, db.updates
    assert "status" not in db.updates[-1], db.updates  # tetap OPEN (moonbag)
    assert db.pos["status"] == "open", db.pos
    assert any("MOONBAG LAPIS 3" in s for s in sent), sent
    assert any("Bebas dari Time-Stop" in s for s in sent), sent


def test_layer2_tp2_full_close_when_fraction_1():
    sent = []
    pos = _make_pos(tokens_left=500.0, tp1_done=1, tp2_done=0)
    db = FakeDB({"tp2": "1.0", "tp2_on": "1", "tp2_sell_pct": "1.0",
                 "tp_verify": "0"}, pos=pos)
    _install_engine(db, sent, price=0.0025)
    asyncio.run(engine._check(db.pos))
    assert _last_update_value(db, "tokens_left") == 0, db.updates
    assert _last_update_value(db, "tp2_done") == 1, db.updates
    assert db.pos["status"] == "closed_tp2", db.pos


def test_layer2_no_retrigger_tp2_after_tp2_done():
    # tp2_done=1 → guard tidak boleh jual TP2 lagi; trailing jarak 20% belum
    # tersentuh (peak 0.0025, stop 0.0020, harga 0.00245) → posisi tetap open.
    sent = []
    pos = _make_pos(tokens_left=250.0, tp1_done=1, tp2_done=1)
    db = FakeDB({"tp2": "1.0", "tp2_on": "1", "tp2_sell_pct": "0.50",
                 "moonbag_trail_pct": "0.20", "trail_on": "1", "tp_verify": "0"},
                pos=pos)
    _install_engine(db, sent, price=0.00245)
    asyncio.run(engine._check(db.pos))
    closed = [u for u in db.updates if "status" in u]
    assert not closed, db.updates


# ---------- Time-stop exemption ----------

def test_layer2_tp2done_exempt_from_time_stop():
    # posisi umur 25 menit (> 1200 dtk) tapi tp2_done=1 → TIDAK timeout.
    # v16.7: moonbag dual-guard aktif → txns m5 ramai supaya inactivity
    # guard tidak picu, dan umur < moonbag_max_hold_sec (2700).
    sent = []
    pos = _make_pos(ts=int(time.time()) - 1500, tokens_left=250.0,
                    tp1_done=1, tp2_done=1)
    pos["peak_price"] = 0.0025
    db = FakeDB({"max_hold_sec": "1200", "trail_on": "1",
                 "moonbag_trail_pct": "0.20"}, pos=pos)
    _install_engine(db, sent, price=0.0021)
    orig = engine.current_pair
    async def active_pair(p):
        return {"priceUsd": "0.0021", "priceNative": "0.001",
                "liquidity": {"usd": 50000},
                "txns": {"m5": {"buys": 8, "sells": 8}}}
    engine.current_pair = active_pair
    try:
        asyncio.run(engine._check(db.pos))
    finally:
        engine.current_pair = orig
    closed = [u for u in db.updates if "status" in u]
    assert not closed, db.updates  # tidak ada closed_timeout dsb
    assert db.pos["status"] == "open", db.pos
    assert not any("TIMEOUT" in s for s in sent), sent


def test_time_stop_still_applies_without_tp2():
    # tanpa tp2_done, posisi tua tetap dijual timeout (regresi)
    sent = []
    pos = _make_pos(ts=int(time.time()) - 1500)
    db = FakeDB({"max_hold_sec": "1200"}, pos=pos)
    _install_engine(db, sent, price=0.001)
    asyncio.run(engine._check(db.pos))
    assert "closed_timeout" in db.statuses(), db.updates


# ---------- Lapis 3: moonbag trailing stop 20% dari puncak ----------

def test_layer3_moonbag_peak_tracking_and_trailing_exit():
    # 4a) puncak naik: peak ikut update
    sent = []
    pos = _make_pos(tokens_left=250.0, tp1_done=1, tp2_done=1)
    pos["peak_price"] = 0.0025
    db = FakeDB({"trail_on": "1", "moonbag_trail_pct": "0.20"}, pos=pos)
    _install_engine(db, sent, price=0.0040)
    asyncio.run(engine._check(db.pos))
    assert db.pos["status"] == "open", db.pos
    assert abs(db.pos["peak_price"] - 0.0040) < 1e-12, db.pos

    # 4b) harga jatuh 20% dari peak 0.0040 → 0.0032 → jual semua sisa 250
    sent = []
    db2 = FakeDB({"trail_on": "1", "moonbag_trail_pct": "0.20"},
                 pos=_make_pos(tokens_left=250.0, tp1_done=1, tp2_done=1,
                               ts=int(time.time()) - 60))
    db2.pos["peak_price"] = 0.0040
    _install_engine(db2, sent, price=0.0032)
    orig = engine.current_pair
    async def active_pair(p):
        return {"priceUsd": "0.0032", "priceNative": "0.001",
                "liquidity": {"usd": 50000},
                "txns": {"m5": {"buys": 5, "sells": 5}}}
    engine.current_pair = active_pair
    try:
        asyncio.run(engine._check(db2.pos))
    finally:
        engine.current_pair = orig
    assert _last_update_value(db2, "tokens_left") == 0, db2.updates
    assert db2.pos["status"] == "closed_trail", db2.pos
    assert any("TRAILING STOP" in s for s in sent), sent


def test_layer3_moonbag_uses_20pct_not_25pct():
    # peak 0.0040, moonbag trail 20% → stop 0.0032 (bukan 25% → 0.0030).
    # harga 0.0031: harus TRIGGER pada jarak 20% (walau belum kena 25%).
    sent = []
    pos = _make_pos(tokens_left=250.0, tp1_done=1, tp2_done=1)
    pos["peak_price"] = 0.0040
    db = FakeDB({"trail_on": "1", "moonbag_trail_pct": "0.20",
                 "trail_pct": "0.25"}, pos=pos)
    _install_engine(db, sent, price=0.0031)
    asyncio.run(engine._check(db.pos))
    assert db.pos["status"] == "closed_trail", db.pos


def test_layer3_no_tp_ceiling_on_moonbag():
    # moonbag di +10x tanpa batas TP: tidak ada jual "tp2" kedua; hanya
    # trailing yang bisa menutup. peak mengikuti, posisi tetap open.
    sent = []
    pos = _make_pos(tokens_left=250.0, tp1_done=1, tp2_done=1)
    pos["peak_price"] = 0.009
    db = FakeDB({"tp2": "1.0", "tp2_on": "1", "tp2_sell_pct": "0.50",
                 "trail_on": "1", "moonbag_trail_pct": "0.20",
                 "tp_verify": "0"}, pos=pos)
    _install_engine(db, sent, price=0.010)  # +10x
    asyncio.run(engine._check(db.pos))
    assert not [u for u in db.updates
                if "tokens_left" in u or "status" in u], db.updates
    assert db.pos["status"] == "open", db.pos


# ---------- v16.7: Moonbag Dual-Guard ----------

def test_moonbag_inactivity_guard_exit_inactive():
    """Moonbag umur >420s dan txns.m5 = 0 → EXIT INACTIVE (closed_inactive)."""
    sent = []
    pos = _make_pos(ts=int(time.time()) - 500, tokens_left=250.0,
                    tp1_done=1, tp2_done=1)
    db = FakeDB({"trail_on": "1", "moonbag_trail_pct": "0.20",
                 "moonbag_inactivity_sec": "420",
                 "moonbag_max_hold_sec": "2700"}, pos=pos)
    _install_engine(db, sent, price=0.0016)  # harga masih di atas stop 0.0020? tidak — di bawah peak, tapi di atas entry; trailing 20% dari peak 0.0011 → stop 0.00088, tidak trigger
    asyncio.run(engine._check(db.pos))
    assert "closed_inactive" in db.statuses(), db.updates
    assert any("EXIT INACTIVE" in s for s in sent), sent
    assert _last_update_value(db, "tokens_left") == 0, db.updates


def test_moonbag_inactivity_guard_not_triggered_young_or_active():
    """Moonbag muda (<420s) ATAU txns m5 ramai → tidak dijual inactive."""
    # 1) moonbag muda tapi volume mati → belum guard
    sent = []
    pos = _make_pos(ts=int(time.time()) - 60, tokens_left=250.0,
                    tp1_done=1, tp2_done=1)
    db = FakeDB({"trail_on": "1", "moonbag_trail_pct": "0.20",
                 "moonbag_inactivity_sec": "420"}, pos=pos)
    _install_engine(db, sent, price=0.0016)
    asyncio.run(engine._check(db.pos))
    assert "closed_inactive" not in db.statuses(), db.updates

    # 2) moonbag tua tapi txns m5 ramai → tetap open
    sent = []
    pos = _make_pos(ts=int(time.time()) - 600, tokens_left=250.0,
                    tp1_done=1, tp2_done=1)
    db = FakeDB({"trail_on": "1", "moonbag_trail_pct": "0.20",
                 "moonbag_inactivity_sec": "420"}, pos=pos)
    _install_engine(db, sent, price=0.0016)
    orig = engine.current_pair
    async def busy_pair(p):
        return {"priceUsd": "0.0016", "priceNative": "0.001",
                "liquidity": {"usd": 50000},
                "txns": {"m5": {"buys": 12, "sells": 30}}}
    engine.current_pair = busy_pair
    try:
        asyncio.run(engine._check(db.pos))
    finally:
        engine.current_pair = orig
    assert "closed_inactive" not in db.statuses(), db.updates


def test_moonbag_hardcap_45m_harvest_full():
    """Moonbag umur >= 2700s → HARVEST FULL via timeout path (closed_timeout)."""
    sent = []
    pos = _make_pos(ts=int(time.time()) - 2800, tokens_left=250.0,
                    tp1_done=1, tp2_done=1)
    db = FakeDB({"trail_on": "1", "moonbag_trail_pct": "0.20",
                 "moonbag_max_hold_sec": "2700"}, pos=pos)
    _install_engine(db, sent, price=0.0016)
    orig = engine.current_pair
    async def active_pair(p):
        return {"priceUsd": "0.0016", "priceNative": "0.001",
                "liquidity": {"usd": 50000},
                "txns": {"m5": {"buys": 5, "sells": 5}}}
    engine.current_pair = active_pair
    try:
        asyncio.run(engine._check(db.pos))
    finally:
        engine.current_pair = orig
    assert "closed_timeout" in db.statuses(), db.updates
    assert any("HARVEST FULL" in s for s in sent), sent


def test_moonbag_exempt_from_normal_time_stop():
    """tanggung jawab reguler: max_hold_sec 420 tidak memukul moonbag (tp2_done=1)."""
    sent = []
    pos = _make_pos(ts=int(time.time()) - 500, tokens_left=250.0,
                    tp1_done=1, tp2_done=1)
    db = FakeDB({"max_hold_sec": "420", "trail_on": "1",
                 "moonbag_trail_pct": "0.20",
                 "moonbag_inactivity_sec": "420"}, pos=pos)
    _install_engine(db, sent, price=0.0016)
    orig = engine.current_pair
    async def active_pair(p):
        return {"priceUsd": "0.0016", "priceNative": "0.001",
                "liquidity": {"usd": 50000},
                "txns": {"m5": {"buys": 5, "sells": 5}}}
    engine.current_pair = active_pair
    try:
        asyncio.run(engine._check(db.pos))
    finally:
        engine.current_pair = orig
    assert "closed_timeout" not in db.statuses(), db.updates
    assert "closed_inactive" not in db.statuses(), db.updates
    assert db.pos["status"] == "open", db.pos


# ---------- konfigurasi ----------

def test_config_defaults_and_labels():
    assert config.SETTING_DEFAULTS["tp2_sell_pct"] == "1.0"
    assert config.SETTING_DEFAULTS["moonbag_trail_pct"] == "0.20"
    assert "tp2_sell_pct" in config.SETTING_LABELS
    assert "moonbag_trail_pct" in config.SETTING_LABELS


def test_db_migration_tp2_done():
    """Kolom tp2_done ada di tabel positions; DB asli TIDAK dipakai di sini."""
    import sqlite3, tempfile
    from sniper import db as real_db
    tmp = tempfile.mktemp(suffix=".db")
    orig = real_db.config.DB_PATH
    try:
        real_db.config.DB_PATH = tmp
        real_db.init()
        with sqlite3.connect(tmp) as c:
            cols = [r[1] for r in c.execute("PRAGMA table_info(positions)")]
        assert "tp2_done" in cols, cols
        # idempotent: init kedua tidak error
        real_db.init()
        real_db.open_position(ca="0x" + "a" * 40, symbol="X",
                              pair_address="0x" + "b" * 40,
                              chain_slug="robinhood", pool_type="v4", fee=0,
                              entry_price_usd=0.001, tokens_total=1000,
                              tokens_left=1000, eth_spent=0.01, usd_spent=5,
                              honeypot=0, source_name="t")
        p = real_db.position(1)
        assert p.get("tp2_done") == 0, p
        real_db.update_position(1, tp2_done=1)
        assert real_db.position(1)["tp2_done"] == 1
    finally:
        real_db.config.DB_PATH = orig
        os.remove(tmp)


if __name__ == "__main__":
    test_layer1_tp1_sells_50pct_position_stays_open()
    print("PASS lapis 1: TP1 50% posisi tetap open")
    test_layer2_tp2_sells_half_of_remainder_marks_tp2_done_stays_open()
    print("PASS lapis 2: TP2 jual 50% sisa, moonbag terbentuk")
    test_layer2_tp2_full_close_when_fraction_1()
    print("PASS lapis 2: tp2_sell_pct=1.0 full close")
    test_layer2_no_retrigger_tp2_after_tp2_done()
    print("PASS lapis 2: tp2_done tidak dijual TP2 lagi")
    test_layer2_tp2done_exempt_from_time_stop()
    print("PASS time-stop exemption moonbag tp2_done")
    test_time_stop_still_applies_without_tp2()
    print("PASS time-stop tetap aktif tanpa tp2_done")
    test_layer3_moonbag_peak_tracking_and_trailing_exit()
    print("PASS lapis 3: peak tracking + trailing 20% jual semua sisa")
    test_layer3_moonbag_uses_20pct_not_25pct()
    print("PASS lapis 3: jarak trailing moonbag 20% (bukan 25%)")
    test_layer3_no_tp_ceiling_on_moonbag()
    print("PASS lapis 3: moonbag bebas batas TP")
    test_moonbag_inactivity_guard_exit_inactive()
    print("PASS moonbag inactivity guard → closed_inactive")
    test_moonbag_inactivity_guard_not_triggered_young_or_active()
    print("PASS moonbag inactivity guard tidak salah picu")
    test_moonbag_hardcap_45m_harvest_full()
    print("PASS moonbag hard-cap 45m → HARVEST FULL")
    test_moonbag_exempt_from_normal_time_stop()
    print("PASS moonbag bebas time-stop biasa (420s)")
    test_config_defaults_and_labels()
    print("PASS config defaults & labels")
    test_db_migration_tp2_done()
    print("PASS db migrasi tp2_done (DB sementara, asli tidak disentuh)")
    print("ALL 3-LAYER EXIT TESTS PASSED")
