"""Uji aturan v16.5.1: Time-Based Stop (timeout max hold 20 menit) +
percepatan interval (poll 5 dtk, rotasi insider 6 jam, max age scanner 5 mnt).

Tidak menyentuh jaringan/Web3/Telethon — db/trader engine dipatching FakeDB.
Pola mengikuti test_hardening_v161.py (patch engine.db per-uji).
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper import config, engine, tracker


# ---------- FakeDB util ----------

def _make_pos(ts=None):
    return {
        "id": 1, "status": "open", "symbol": "TEST", "ca": "0x" + "a" * 40,
        "pair_address": "0x" + "b" * 40, "chain_slug": "robinhood",
        "entry_price_usd": 0.001, "tokens_total": 1e6, "tokens_left": 1e6,
        "eth_spent": 0.01, "usd_spent": 5.0, "tp1_done": 0,
        "is_simulated": 1, "ts": int(ts if ts is not None else time.time()),
    }


class FakeDB:
    """db minimal untuk engine._check / _execute_sell_locked."""

    def __init__(self, settings=None, pos=None):
        self.settings = settings or {}
        self.updates = []          # (fields_dict) per update_position
        self.pos = pos or _make_pos()

    def get(self, key, cast=float):
        v = self.settings.get(key, config.SETTING_DEFAULTS.get(key, 0))
        try:
            return cast(v)
        except (TypeError, ValueError):
            return cast(0)

    def position(self, pid):
        return dict(self.pos) if pid == self.pos["id"] else None

    def position_row(self):
        return self.pos

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
        # pemicu Auto-Tuner (kelipatan 15) — di tes ini tidak boleh memicu
        return 7

    def add_paper_cash(self, amount):
        return 100.0

    # util cek
    def statuses(self):
        return [u["status"] for u in self.updates if "status" in u]


def _install_engine(fakedb, sent):
    class FakeTrader:
        def sell(self, pos, fraction, price_native):
            return (pos["tokens_left"], pos["eth_spent"], 0.0, "simulated_sell")
        def exec_pnl(self, pos, fraction):
            return 1.0

    async def fake_pair(pos):
        return {"priceUsd": "0.0011", "priceNative": "0.001",
                "liquidity": {"usd": 50000}}

    async def notify(text, *a, **kw):
        sent.append(str(text))

    engine.db = fakedb
    engine._trader = FakeTrader()
    engine.current_pair = fake_pair
    engine.bind(engine._trader, notify)
    # bersihkan state lintas-uji
    engine._thin_warned.clear()
    engine._liq_last.clear()
    engine._cleanup_marks.clear()
    return fakedb


# ---------- Poin 1: max hold timeout ----------

def test_old_position_triggers_timeout_sell_and_closed_timeout():
    """Posisi umur >= 1200 dtk → execute_sell(...,'timeout') → closed_timeout."""
    sent = []
    db = FakeDB({"max_hold_sec": 1200},
                pos=_make_pos(ts=int(time.time()) - 1300))  # 21 menit
    _install_engine(db, sent)

    async def run():
        await engine._check(db.pos)

    asyncio.run(run())
    statuses = [u["status"] for u in db.updates if "status" in u]
    assert "closed_timeout" in statuses, db.updates
    assert any("TIMEOUT" in s for s in sent), sent
    assert db.pos["tokens_left"] == 0, db.pos


def test_old_position_calls_execute_sell_with_timeout_reason():
    """Bukti reason 'timeout' lewat teks notifikasi sell (status lengkap)."""
    sent = []
    db = FakeDB({"max_hold_sec": 1200},
                pos=_make_pos(ts=int(time.time()) - 1200))   # tepat 1200
    _install_engine(db, sent)

    async def run():
        await engine._check(db.pos)

    asyncio.run(run())
    assert any("TIME STOP" in s for s in sent), sent
    assert any("closed_timeout" in str(u) for u in [db.updates]) or \
        any(u.get("status") == "closed_timeout" for u in db.updates), db.updates


def test_young_position_not_timeout():
    """Posisi umur < 1200 dtk → tidak dijual (tak ada status closed_*)."""
    sent = []
    db = FakeDB({"max_hold_sec": 1200},
                pos=_make_pos(ts=int(time.time()) - 60))    # 1 menit
    _install_engine(db, sent)

    async def run():
        await engine._check(db.pos)

    asyncio.run(run())
    statuses = [u["status"] for u in db.updates if "status" in u]
    assert "closed_timeout" not in statuses, db.updates
    assert not any("TIMEOUT" in s for s in sent), sent


def test_max_hold_zero_disables_timeout():
    """max_hold_sec=0 → fitur off, posisi tua pun tak dijual timeout."""
    sent = []
    db = FakeDB({"max_hold_sec": "0"},
                pos=_make_pos(ts=int(time.time()) - 999999))
    _install_engine(db, sent)

    async def run():
        await engine._check(db.pos)

    asyncio.run(run())
    statuses = db.statuses()
    assert "closed_timeout" not in statuses, db.updates


def test_timeout_reason_recorded_in_daily_recap():
    """build_recap menampilkan baris timeout bila ada closed_timeout."""
    from sniper import db as real_db

    class FakeRecapDB:
        def recap_data(self, since_ts):
            return {
                "calls": 3, "buys": 1, "usd_spent": 5.0, "eth_spent_new": 0.01,
                "closed": [{"status": "closed_timeout", "n": 2,
                            "eth_in": 0.0, "eth_out": 0.0}],
                "open": 0, "top_caller": None,
            }

    orig = engine.db
    engine.db = FakeRecapDB()
    try:
        text = engine.build_recap(0)
    finally:
        engine.db = orig
    assert "timeout" in text.lower(), text
    assert "2" in text, text   # jumlah closed_timeout muncul di rekap


# ---------- Poin 2, 3, 4: default setting / konstanta ----------

def test_defaults_poin234():
    assert config.SETTING_DEFAULTS["poll_seconds"] == "3"
    assert config.SETTING_DEFAULTS["max_hold_sec"] == "240"
    assert config.SETTING_DEFAULTS["scanner_max_age_min"] == "5"


def test_moonbag_dual_guard_defaults():
    """v16.7: default guard moonbag ada di config."""
    assert config.SETTING_DEFAULTS["moonbag_inactivity_sec"] == "420"
    assert config.SETTING_DEFAULTS["moonbag_max_hold_sec"] == "2700"
    assert "moonbag_inactivity_sec" in config.SETTING_LABELS
    assert "moonbag_max_hold_sec" in config.SETTING_LABELS


def test_max_hold_label_present():
    assert "max_hold_sec" in config.SETTING_LABELS
    assert "7m" in config.SETTING_LABELS["max_hold_sec"]


def test_rotate_interval_sec_six_hours():
    assert tracker.ROTATE_INTERVAL_SEC == 21600


if __name__ == "__main__":
    test_old_position_triggers_timeout_sell_and_closed_timeout()
    print("PASS timeout triggers")
    test_old_position_calls_execute_sell_with_timeout_reason()
    print("PASS timeout reason")
    test_young_position_not_timeout()
    print("PASS young not timeout")
    test_max_hold_zero_disables_timeout()
    print("PASS max_hold 0 off")
    test_timeout_reason_recorded_in_daily_recap()
    print("PASS recap timeout")
    test_defaults_poin234()
    print("PASS defaults poin 2/3/4")
    test_moonbag_dual_guard_defaults()
    print("PASS moonbag dual-guard defaults")
    test_max_hold_label_present()
    print("PASS label")
    test_rotate_interval_sec_six_hours()
    print("PASS rotate interval")
    print("ALL TIME-STOP & INTERVAL TESTS PASSED")
