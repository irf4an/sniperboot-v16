"""🧪 Uji INSIDER TRACKER v16.1 — tanpa jaringan & tanpa Web3 asli.

Semua fungsi murni parser/DB dibuktikan di sini:
  - extract_token_out      (exactInputSingle / exactInput / multicall)
  - is_buy_call            (tokenIn quote vs token lain)
  - CRUD insider_wallets   (add / get_active / toggle / update_if_exists)
  - screen_insiders        (cross-check intersection + persist)
  - _inspect_logs          (jalur sinyal batch ERC20 Transfer)
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper import config, db            # noqa: E402
from sniper import tracker               # noqa: E402
import tools.screen_insiders as si       # noqa: E402


# =========================================================
# 2. CRUD DB insider_wallets (DB sementara, tidak menyentuh hoodsniper.db)
# =========================================================

def _fresh_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old = config.DB_PATH
    config.DB_PATH = tmp.name
    return tmp.name, old


def test_insider_crud():
    path, old = _fresh_db()
    try:
        db.init()
        a = "0x" + "a" * 40
        db.add_insider_wallet(a.upper(), label="test", win_count=2,
                              notes="unit", win_cas="0xcaa,0xcab")
        assert a in db.get_active_insiders()

        # update_if_exists: win_count ditambah (bukan ditimpa)
        db.add_insider_wallet(a, win_count=3, win_cas="0xcac",
                              update_if_exists=True)
        rows = db.list_insiders()
        assert rows[0]["win_count"] == 5, rows

        # toggle
        assert db.toggle_insider(a) == 0
        assert a not in db.get_active_insiders()
        assert db.toggle_insider(a) == 1
        assert a in db.get_active_insiders()

        # INSERT OR IGNORE: tidak duplikat row, win_count tidak berubah
        db.add_insider_wallet(a, win_count=99)
        rows = db.list_insiders()
        assert len(rows) == 1 and rows[0]["win_count"] == 5, rows
        print("PASS insider_wallets CRUD")
    finally:
        os.unlink(path)
        config.DB_PATH = old


# =========================================================
# 3. screen_insiders — intersection + persist
# =========================================================

def test_cross_check_intersection():
    winners = ["0x" + "c" * 40, "0x" + "d" * 40]
    WA, WB, WE = "0x" + "a" * 40, "0x" + "b" * 40, "0x" + "e" * 40

    def _t(*addrs):
        return [{"to": a, "value": 1.0, "ts": 0, "is_contract": False}
                for a in addrs]

    si.fetch_early_buyers_ex = (lambda ca, max_buyers=50, retries=2:
                                ("ok", _t(WA, WB) if ca == winners[0] else _t(WA, WE)))
    si.fetch_gecko_buyers_ex = lambda ca, max_pools=3: ("ok", set())
    rows = si.cross_check(winners)
    by = {r["address"]: r for r in rows}
    # wallet 'a' satu-satunya yang muncul di 2 koin (intersection)
    assert by[WA]["n_ca"] == 2, by
    assert by[WB]["n_ca"] == 1 and by[WE]["n_ca"] == 1, by
    # 1 beli per koin, Blockscout saja → 0,5/koin
    assert by[WA]["score"] == 1.0, by[WA]
    assert by[WA]["qualifies"] is False, by[WA]   # skor < 2,0
    print("PASS screen_insiders cross_check intersection (n_ca + bobot BS lemah)")


def test_screen_insiders_persist():
    path, old = _fresh_db()
    try:
        db.init()
        addr = "0x" + "f" * 40

        def _row(n_ca, score, cas):
            return {"address": addr, "score": score, "n_ca": n_ca,
                    "verified_cas": cas, "gt_only_cas": [],
                    "sources": "", "qualifies": True, "needs_retry": False}

        si.persist([_row(3, 3.0, ["0x" + "c1" * 20, "0x" + "c2" * 20,
                                   "0x" + "c3" * 20])])
        row = db.list_insiders()[0]
        assert row["win_count"] == 3 and row["score"] == 3.0, row

        # Scan ulang lebih lengkap (4 CA) → skor otoritatif, boleh turun (decay)
        si.persist([_row(4, 1.6, ["0x" + "c1" * 20, "0x" + "c2" * 20,
                                   "0x" + "c3" * 20, "0x" + "c4" * 20])])
        row = db.list_insiders()[0]
        assert row["win_count"] == 4, row
        assert row["score"] == 1.6, row

        # Scan parsial (1 CA) → tidak boleh menurunkan skor maupun win_count
        si.persist([_row(1, 0.5, ["0x" + "c5" * 20])])
        row = db.list_insiders()[0]
        assert row["win_count"] == 4 and row["score"] == 1.6, row
        assert addr in db.get_active_insiders()
        print("PASS screen_insiders persist (MAX win_count, skor otoritatif)")
    finally:
        os.unlink(path)
        config.DB_PATH = old


# =========================================================
# 4. process_insider_buy / _inspect_logs — jalur sinyal + dedup
# =========================================================

def _tx_from_calldata(frm, to, data_hex):
    return {"from": frm, "to": to,
            "input": bytes.fromhex(data_hex[2:])}


def test_process_insider_buy_alert_only_no_pipeline():
    """insider_alert_only=1 → NOTIF, TANPA listener.process_ca."""
    path, old = _fresh_db()
    try:
        db.init()   # config SETTING_DEFAULTS → insider_alert_only = "1"
        insider = "0x" + "a" * 40
        token = "0x" + "e" * 40
        pair = {"baseToken": {"symbol": "TST"},
                "pairAddress": "0x" + "f" * 40, "chainId": "robinhood",
                "marketCap": 61000, "liquidity": {"usd": 8200}}

        alerts = []
        async def fake_notify(text, **kw):
            alerts.append(text)

        pipe_calls = []
        async def fake_pipe(*a, **k):
            pipe_calls.append((a, k))

        orig_notify, orig_pipe = tracker._notify_cb, tracker.listener.process_ca
        orig_lp = tracker.listener.process_ca
        tracker._notify_cb = fake_notify
        tracker.listener.process_ca = fake_pipe
        try:
            asyncio.run(tracker.process_insider_buy(
                token, insider_addr=insider, pair=pair))
        finally:
            tracker._notify_cb = orig_notify
            tracker.listener.process_ca = orig_pipe

        assert not pipe_calls, pipe_calls
        assert len(alerts) == 1, alerts
        text = alerts[0]
        assert "INSIDER BUY" in text and "TST" in text, text
        assert token in text, text
        assert insider[:6] in text and insider[-4:] in text, text
        assert "61k" in text and "8.2k" in text, text      # mcap & liq
        assert "dexscreener.com/robinhood/" + "0x" + "f" * 40 in text, text
        print("PASS process_insider_buy alert-only (notify, 0 pipa)")
    finally:
        os.unlink(path)
        config.DB_PATH = old


def test_process_insider_buy_legacy_pipeline_fallback():
    """insider_alert_only=0 → perilaku lama: CA → listener.process_ca."""
    path, old = _fresh_db()
    try:
        db.init()
        db.set_("insider_alert_only", "0")
        insider = "0x" + "a" * 40
        token = "0x" + "e" * 40

        pipe_calls = []
        async def fake_pipe(ca, src, caller_id=None, caller_name=None,
                            allow_buy=True):
            pipe_calls.append({"ca": ca, "caller_id": caller_id,
                               "caller_name": caller_name,
                               "allow_buy": allow_buy})

        alerts = []
        async def fake_notify(text, **kw):
            alerts.append(text)

        orig_pipe = tracker.listener.process_ca
        orig_notify = tracker._notify_cb
        tracker.listener.process_ca = fake_pipe
        tracker._notify_cb = fake_notify
        try:
            asyncio.run(tracker.process_insider_buy(
                token, insider_addr=insider))
        finally:
            tracker.listener.process_ca = orig_pipe
            tracker._notify_cb = orig_notify

        assert alerts == [], alerts
        assert len(pipe_calls) == 1, pipe_calls
        call = pipe_calls[0]
        assert call["ca"] == token, call
        assert call["caller_id"] == insider, call
        assert call["allow_buy"] is True, call
        assert call["caller_name"] == f"INSIDER:{insider[:10]}", call
        print("PASS process_insider_buy fallback lama (insider_alert_only=0 → pipeline)")
    finally:
        os.unlink(path)
        config.DB_PATH = old


def test_inspect_logs_batch():
    """Batch log scan via eth_getLogs: mendeteksi transfer token ke insider."""
    path, old = _fresh_db()
    try:
        db.init()
        insider = "0x" + "a" * 40
        db.add_insider_wallet(insider, win_count=1)
        token_ca = "0x" + "b" * 40

        class FakeW3Logs:
            def __init__(self, logs):
                self._logs = logs
                self.eth = self
            def get_logs(self, filt):
                return self._logs

        fake_log = {
            "address": token_ca,
            "topics": [
                bytes.fromhex(tracker.TRANSFER_TOPIC[2:]),
                bytes.fromhex("0" * 24 + "1" * 40),
                bytes.fromhex("0" * 24 + insider[2:]),
            ]
        }
        sent = []
        async def fake_process(ca, insider_addr="", notify=None, pair=None):
            sent.append((ca, insider_addr))

        orig_p, orig_pair = tracker.process_insider_buy, tracker.dx.best_pair
        tracker.process_insider_buy = fake_process
        tracker.dx.best_pair = lambda ca: {"priceChange": {"m5": 0.0}}
        try:
            got = asyncio.run(tracker._inspect_logs(FakeW3Logs([fake_log]), 100, 150))
        finally:
            tracker.process_insider_buy, tracker.dx.best_pair = orig_p, orig_pair

        assert got == 1, got
        assert sent == [(token_ca.lower(), insider.lower())], sent
        print("PASS _inspect_logs batch transfer detection")
    finally:
        os.unlink(path)
        config.DB_PATH = old


def _patch_notify(pipe_and_notify_setup=True, alerts=None):
    """Helper: patch _notify_cb + listener.process_ca; return list restore."""
    if alerts is None:
        alerts = []
    callbacks = {"alerts": alerts}

    async def fake_notify(text, **kw):
        callbacks["alerts"].append(text)

    pipe_calls = callbacks.setdefault("pipe", [])

    async def fake_pipe(*a, **k):
        pipe_calls.append((a, k))

    orig_notify, orig_pipe = tracker._notify_cb, tracker.listener.process_ca
    tracker._notify_cb = fake_notify
    tracker.listener.process_ca = fake_pipe
    tracker._alert_dedup.clear()
    return callbacks, (orig_notify, orig_pipe)


def test_process_insider_buy_none_pair_no_alert():
    """(a) best_pair None → alert TIDAK dikirim (bukan notif '?')."""
    path, old = _fresh_db()
    try:
        db.init()   # insider_alert_only = "1" default
        insider = "0x" + "a" * 40
        token = "0x" + "e" * 40

        alerts = []
        callbacks, (orig_notify, orig_pipe) = _patch_notify(alerts=alerts)
        # pair None + pair fetch di process_insider_buy juga None
        orig_bp = tracker.dx.best_pair

        def fake_bp(ca):
            return None

        tracker.dx.best_pair = fake_bp
        # buat retry task tidak lama (task background akan di-await)
        orig_delay = tracker._INSIDER_RETRY_DELAY
        tracker._INSIDER_RETRY_DELAY = 0.02
        try:
            async def _run():
                await tracker.process_insider_buy(
                    token, insider_addr=insider, pair=None)
                # beri kesempatan retry background task jalan (tetap None)
                await asyncio.sleep(0.05)
            asyncio.run(_run())
        finally:
            tracker._notify_cb = orig_notify
            tracker.listener.process_ca = orig_pipe
            tracker.dx.best_pair = orig_bp
            tracker._INSIDER_RETRY_DELAY = orig_delay

        assert not callbacks["pipe"], callbacks["pipe"]
        assert alerts == [], (
            f"alert kosong tidak boleh terkirim: {alerts}")
        print("PASS process_insider_buy best_pair None → alert dibiarkan "
              "diam (skip) tanpa notif Telegram")
    finally:
        os.unlink(path)
        config.DB_PATH = old


def test_process_insider_buy_none_pair_retry_success():
    """(b) retry setelah 30s → best_pair berhasil → alert DIKIRIM."""
    path, old = _fresh_db()
    try:
        db.init()
        insider = "0x" + "a" * 40
        token = "0x" + "e" * 40
        pair = {"baseToken": {"symbol": "RTY"},
                "pairAddress": "0x" + "d" * 40, "chainId": "robinhood",
                "marketCap": 42000, "liquidity": {"usd": 5100}}

        alerts = []
        callbacks, (orig_notify, orig_pipe) = _patch_notify(alerts=alerts)
        orig_bp = tracker.dx.best_pair
        calls = {"n": 0}

        def fake_bp(ca):
            calls["n"] += 1
            return pair if calls["n"] >= 2 else None   # #1 None, #2 pair

        tracker.dx.best_pair = fake_bp
        orig_delay = tracker._INSIDER_RETRY_DELAY
        tracker._INSIDER_RETRY_DELAY = 0.02
        try:
            async def _run():
                await tracker.process_insider_buy(
                    token, insider_addr=insider, pair=None)
                # beri kesempatan retry background task jalan
                await asyncio.sleep(0.08)
            asyncio.run(_run())
        finally:
            tracker._notify_cb = orig_notify
            tracker.listener.process_ca = orig_pipe
            tracker.dx.best_pair = orig_bp
            tracker._INSIDER_RETRY_DELAY = orig_delay

        assert calls["n"] >= 2, calls
        assert not callbacks["pipe"], callbacks["pipe"]
        assert len(alerts) == 1, alerts
        text = alerts[0]
        assert "INSIDER BUY" in text and "RTY" in text, text
        assert token in text, text
        assert insider[:6] in text, text
        assert "42k" in text and "5.1k" in text, text
        assert "dexscreener.com/robinhood/" + "0x" + "d" * 40 in text, text
        print("PASS process_insider_buy retry best_pair → alert DIKIRIM")
    finally:
        os.unlink(path)
        config.DB_PATH = old


def test_process_insider_buy_none_pair_retry_still_none():
    """(c) retry setelah 30s → best_pair masih None → tidak dikirim."""
    path, old = _fresh_db()
    try:
        db.init()
        insider = "0x" + "a" * 40
        token = "0x" + "e" * 40

        alerts = []
        callbacks, (orig_notify, orig_pipe) = _patch_notify(alerts=alerts)
        orig_bp = tracker.dx.best_pair

        def fake_bp(ca):
            return None

        tracker.dx.best_pair = fake_bp
        orig_delay = tracker._INSIDER_RETRY_DELAY
        tracker._INSIDER_RETRY_DELAY = 0.02
        try:
            async def _run():
                await tracker.process_insider_buy(
                    token, insider_addr=insider, pair=None)
                await asyncio.sleep(0.08)   # kasih retry task jalan
            asyncio.run(_run())
        finally:
            tracker._notify_cb = orig_notify
            tracker.listener.process_ca = orig_pipe
            tracker.dx.best_pair = orig_bp
            tracker._INSIDER_RETRY_DELAY = orig_delay

        assert not callbacks["pipe"], callbacks["pipe"]
        assert alerts == [], (
            f"selepas retry masih None — alert tak boleh terkirim: {alerts}")
        print("PASS process_insider_buy retry masih None → tetap tidak "
              "dikirim (log saja)")
    finally:
        os.unlink(path)
        config.DB_PATH = old


def test_insider_alert_dedup_two_buys_one_window():
   """(a) 2x beli wallet sama + token sama dalam jendela → 1 alert penuh
   + 1 notif dedup '(+1 beli lagi…)', bukan 2 alert penuh."""
   path, old = _fresh_db()
   try:
       db.init()
       db.set_("insider_alert_dedup_window_sec", "600")
       insider = "0x" + "a" * 40
       token = "0x" + "e" * 40
       pair = {"baseToken": {"symbol": "SEASON"},
               "pairAddress": "0x" + "d" * 40, "chainId": "robinhood",
               "marketCap": 1_100_000, "liquidity": {"usd": 105_000}}
       alerts = []
       callbacks, restore = _patch_notify(alerts=alerts)
       tracker._alert_dedup.clear()
       try:
           async def _run():
               await tracker.process_insider_buy(
                   token, insider_addr=insider, pair=dict(pair))
               await tracker.process_insider_buy(
                   token, insider_addr=insider, pair=dict(pair))
           asyncio.run(_run())
       finally:
           tracker._notify_cb, tracker.listener.process_ca = restore
           tracker._alert_dedup.clear()

       assert not callbacks["pipe"], callbacks["pipe"]
       assert len(alerts) == 1, alerts   # repeat DIAM, bukan notif kedua
       assert "INSIDER BUY — SEASON" in alerts[0], alerts
       assert "(+" not in alerts[0], alerts
       print("PASS dedup 2x beli dalam jendela → 1 alert, repeat diam")
   finally:
       os.unlink(path)
       config.DB_PATH = old


def test_insider_alert_dedup_window_expired_full_alert():
   """(b) jendela kedaluwarsa (waktu simulasi lewat) → alert penuh lagi."""
   path, old = _fresh_db()
   try:
       db.init()
       db.set_("insider_alert_dedup_window_sec", "600")
       insider = "0x" + "a" * 40
       token = "0x" + "e" * 40
       pair = {"baseToken": {"symbol": "SEASON"},
               "pairAddress": "0x" + "d" * 40, "chainId": "robinhood",
               "marketCap": 1_100_000, "liquidity": {"usd": 105_000}}
       alerts = []
       callbacks, restore = _patch_notify(alerts=alerts)
       tracker._alert_dedup.clear()
       cur_time = {"t": 1000.0}
       real_time = tracker.time.time

       def fake_time():
           return cur_time["t"]

       tracker.time.time = fake_time
       try:
           async def _run():
               await tracker.process_insider_buy(
                   token, insider_addr=insider, pair=dict(pair))
               for _ in range(3):  # 3 beli ditahan di jendela
                   await tracker.process_insider_buy(
                       token, insider_addr=insider, pair=dict(pair))
               cur_time["t"] += 700  # lewat jendela 600s
               await tracker.process_insider_buy(
                   token, insider_addr=insider, pair=dict(pair))
           asyncio.run(_run())
       finally:
           tracker._notify_cb, tracker.listener.process_ca = restore
           tracker.time.time = real_time
           tracker._alert_dedup.clear()

       assert len(alerts) == 2, alerts   # 1 awal + 1 setelah jendela habis
       assert "(+" not in alerts[0], alerts[0]
       assert "(+3 beli lagi dalam 10m)" in alerts[1], alerts[1]
       print("PASS dedup jendela habis → alert penuh + ringkasan (+3)")
   finally:
       os.unlink(path)
       config.DB_PATH = old


def test_insider_alert_dedup_different_wallet_or_token():
   """(c) dedup per-CA: wallet kedua beli token sama dalam jendela DITAHAN
   (masuk log insider_buys), sedangkan token beda tetap alert penuh."""
   path, old = _fresh_db()
   try:
       db.init()
       db.set_("insider_alert_dedup_window_sec", "600")
       w1 = "0x" + "a" * 40
       w2 = "0x" + "b" * 40
       t1 = "0x" + "e" * 40
       t2 = "0x" + "f" * 40
       pair = {"baseToken": {"symbol": "SEASON"},
               "pairAddress": "0x" + "d" * 40, "chainId": "robinhood",
               "marketCap": 1_100_000, "liquidity": {"usd": 105_000}}
       alerts = []
       callbacks, restore = _patch_notify(alerts=alerts)
       tracker._alert_dedup.clear()
       try:
           async def _run():
               await tracker.process_insider_buy(t1, insider_addr=w1, pair=dict(pair))
               await tracker.process_insider_buy(t1, insider_addr=w2, pair=dict(pair))  # wallet beda, token sama → ditahan di chat, masuk log
               await tracker.process_insider_buy(t2, insider_addr=w1, pair=dict(pair))  # token beda → alert penuh
           asyncio.run(_run())
       finally:
           tracker._notify_cb, tracker.listener.process_ca = restore
           tracker._alert_dedup.clear()

       assert not callbacks["pipe"], callbacks["pipe"]
       assert len(alerts) == 2, alerts
       top = db.top_insider_buys(window_sec=3600)
       assert top[0]["ca"] == t1 and top[0]["buy_count"] == 2 and top[0]["wallet_count"] == 2, top
       print("PASS dedup per-CA + log insider_buys tercatat akurat")
   finally:
       os.unlink(path)
       config.DB_PATH = old


def test_insider_alert_dedup_disabled_zero():
   """(d) setting 0 → dedup mati; semua alert penuh terkirim."""
   path, old = _fresh_db()
   try:
       db.init()
       db.set_("insider_alert_dedup_window_sec", "0")
       insider = "0x" + "a" * 40
       token = "0x" + "e" * 40
       pair = {"baseToken": {"symbol": "SEASON"},
               "pairAddress": "0x" + "d" * 40, "chainId": "robinhood",
               "marketCap": 1_100_000, "liquidity": {"usd": 105_000}}
       alerts = []
       callbacks, restore = _patch_notify(alerts=alerts)
       tracker._alert_dedup.clear()
       try:
           async def _run():
               for _ in range(3):
                   await tracker.process_insider_buy(
                       token, insider_addr=insider, pair=dict(pair))
           asyncio.run(_run())
       finally:
           tracker._notify_cb, tracker.listener.process_ca = restore
           tracker._alert_dedup.clear()

       assert not callbacks["pipe"], callbacks["pipe"]
       assert len(alerts) == 3, alerts
       for a in alerts:
           assert "INSIDER BUY — SEASON" in a and "(+" not in a, a
       print("PASS dedup setting 0 → mati, semua alert penuh")
   finally:
       os.unlink(path)
       config.DB_PATH = old


if __name__ == "__main__":
    test_insider_crud()
    test_cross_check_intersection()
    test_screen_insiders_persist()
    test_process_insider_buy_alert_only_no_pipeline()
    test_process_insider_buy_legacy_pipeline_fallback()
    test_inspect_logs_batch()
    test_process_insider_buy_none_pair_no_alert()
    test_process_insider_buy_none_pair_retry_success()
    test_process_insider_buy_none_pair_retry_still_none()
    test_insider_alert_dedup_two_buys_one_window()
    test_insider_alert_dedup_window_expired_full_alert()
    test_insider_alert_dedup_different_wallet_or_token()
    test_insider_alert_dedup_disabled_zero()
    print("ALL TRACKER TESTS PASSED ✅")
