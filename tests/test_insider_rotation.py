"""🧪 Uji ROTASI WALLET INSIDER v16.3 — tanpa jaringan.

Lingkup:
  1. record_insider_result : 1x loss tidak mematikan, 2x loss auto-disable,
     win reset loss_count.
  2. Alur _inspect_block → process_insider_buy: alamat insider di-embed ke
     caller_id/caller_name → tersimpan di posisi trade.
  3. prune_stale_insiders : wallet lunglai >14 hari (win<2) → enabled=0.
  4. tools.rotate_insiders.rotate : prune + rekrut + ringkasan.
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
import tools.rotate_insiders as rot      # noqa: E402


def _fresh_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old = config.DB_PATH
    config.DB_PATH = tmp.name
    return tmp.name, old


def _insider_row(addr):
    rows = [r for r in db.list_insiders() if r["address"] == addr]
    return rows[0] if rows else None


# =========================================================
# 1. record_insider_result — loss streak & win reset
# =========================================================

def test_record_loss_then_disable():
    """1x loss tidak mematikan; 2x loss berturut → enabled = 0."""
    path, old = _fresh_db()
    try:
        db.init()
        addr = "0x" + "b" * 40
        db.add_insider_wallet(addr, win_count=1)

        r1 = db.record_insider_result(addr, won=False)
        assert r1 == {"address": addr, "action": "loss", "disabled": False}, r1
        row = _insider_row(addr)
        assert row["loss_count"] == 1 and row["enabled"] == 1, row

        r2 = db.record_insider_result(addr, won=False)
        assert r2 == {"address": addr, "action": "loss", "disabled": True}, r2
        row = _insider_row(addr)
        assert row["loss_count"] == 2 and row["enabled"] == 0, row
        assert addr not in db.get_active_insiders()
        print("PASS record_insider_result: 2x loss → auto-disable")
    finally:
        os.unlink(path)
        config.DB_PATH = old


def test_record_win_resets_loss():
    """Win (TP): win_count +1 dan loss_count direset ke 0."""
    path, old = _fresh_db()
    try:
        db.init()
        addr = "0x" + "c" * 40
        db.add_insider_wallet(addr, win_count=0)

        db.record_insider_result(addr, won=False)     # loss 1
        r = db.record_insider_result(addr, won=True)  # win → reset
        assert r == {"address": addr, "action": "win", "disabled": False}, r
        row = _insider_row(addr)
        assert row["win_count"] == 1 and row["loss_count"] == 0, row
        assert row["enabled"] == 1 and row["last_seen"] > 0, row

        # setelah win, loss perlu 2x LAGI sebelum mati
        db.record_insider_result(addr, won=False)
        row = _insider_row(addr)
        assert row["enabled"] == 1, row
        db.record_insider_result(addr, won=False)
        row = _insider_row(addr)
        assert row["enabled"] == 0, row
        print("PASS record_insider_result: win reset loss_count")
    finally:
        os.unlink(path)
        config.DB_PATH = old


def test_record_normalizes_case_and_unknown():
    """Address di-normalisasi lower(); address tak dikenal tidak crash.
    (UPDATE pada row tidak ada → no-op, return tetap konsisten.)"""
    path, old = _fresh_db()
    try:
        db.init()
        addr = "0x" + "d" * 40
        db.add_insider_wallet(addr.upper(), win_count=0)
        r = db.record_insider_result(addr.upper(), won=False)
        assert r["address"] == addr, r
        r = db.record_insider_result("0x" + "e" * 40, won=True)
        assert r["action"] == "win", r  # no-op update, tetap tercatat
        print("PASS record_insider_result: normalisasi address")
    finally:
        os.unlink(path)
        config.DB_PATH = old


# =========================================================
# 2. process_insider_buy → perilaku mode (alert-only vs legacy pipa)
# =========================================================

def test_insider_alert_only_no_pipeline():
    """DEFAULT insider_alert_only=1 → alert dikirim, 0 panggilan listener.process_ca."""
    path, old = _fresh_db()
    try:
        db.init()
        insider = "0x" + "a" * 40
        db.add_insider_wallet(insider, win_count=1)
        token = "0x" + "f" * 40

        calls, sent = [], []

        async def fake_listener_process_ca(*a, **kw):
            calls.append((a, kw))

        orig_l = tracker.listener.process_ca
        orig_dedup = tracker._dedup_check_send
        orig_deliver = tracker._deliver_alert
        tracker.listener.process_ca = fake_listener_process_ca
        tracker._dedup_check_send = lambda *a, **kw: "alert-teks"
        tracker._deliver_alert = lambda text: sent.append(text)
        try:
            asyncio.run(tracker.process_insider_buy(
                token, insider, pair={"priceChange": {"m5": 0.5}}))
        finally:
            tracker.listener.process_ca = orig_l
            tracker._dedup_check_send = orig_dedup
            tracker._deliver_alert = orig_deliver

        assert sent == ["alert-teks"], sent
        assert calls == [], calls       # TAPI tidak masuk pipa listener
        print("PASS alert-only default: 1 alert, 0 panggilan listener.process_ca")
    finally:
        os.unlink(path)
        config.DB_PATH = old


def test_legacy_pipeline_embeds_insider_addr():
    """insider_alert_only=0 (perilaku lama): CA diteruskan ke
    listener.process_ca dengan caller_id = alamat insider."""
    path, old = _fresh_db()
    try:
        db.init()
        db.set_("insider_alert_only", "0")   # jalur legacy → pipa
        insider = "0x" + "a" * 40
        db.add_insider_wallet(insider, win_count=1)
        token = "0x" + "f" * 40

        calls = []

        async def fake_listener_process_ca(ca, src, caller_id=None,
                                           caller_name=None,
                                           allow_buy=None, **kw):
            calls.append({"ca": ca, "caller_id": caller_id,
                          "caller_name": caller_name,
                          "allow_buy": allow_buy if allow_buy is not None
                          else kw.get("allow_buy", True)})

        orig_l = tracker.listener.process_ca
        tracker.listener.process_ca = fake_listener_process_ca

        async def _run():
            await tracker.process_insider_buy(
                token, insider, pair={"priceChange": {"m5": 0.5}})
            await asyncio.sleep(0)          # biarkan create_task jalan

        try:
            asyncio.run(_run())
        finally:
            tracker.listener.process_ca = orig_l

        assert len(calls) == 1, calls
        call = calls[0]
        assert call["ca"] == token.lower(), call
        assert call["caller_id"] == insider, call          # alamat insider!
        assert call["caller_name"] == f"INSIDER:{insider[:10]}", call
        assert call["allow_buy"] is True, call

        # posisi dari listener akan menyimpan caller_id ini — simulasi
        # seperti listener._do_buy: db.update_position(pid, caller_id=...)
        db.open_position(ca=call["ca"], symbol="TEST", pair_address="p",
                         chain_slug="robinhood", pool_type="v3", fee=3000,
                         entry_price_usd=1.0, tokens_total=100,
                         tokens_left=100, eth_spent=0.1, usd_spent=1,
                         honeypot=0, source_name="INSIDER_TRACKER",
                         caller_id=call["caller_id"],
                         caller_name=call["caller_name"])
        pos_rows = [p for p in db.open_positions()]
        assert pos_rows and pos_rows[0]["caller_id"] == insider, pos_rows
        assert pos_rows[0]["caller_name"].startswith("INSIDER:"), pos_rows
        print("PASS alur legacy (insider_alert_only=0): insider address ter-embed di posisi trade")
    finally:
        os.unlink(path)
        config.DB_PATH = old


# =========================================================
# 3. prune_stale_insiders
# =========================================================

def test_prune_stale_insiders():
    """last_seen lama + win_count < 2 → nonaktif; wallet berprestasi dan
    yang last_seen=0 (belum pernah dipakai) tetap hidup."""
    path, old = _fresh_db()
    try:
        db.init()
        now = int(time.time())
        stale = "0x" + "1" * 40       # lunglai 20 hari, cuma 1 win → mati
        fresh = "0x" + "2" * 40       # baru terlihat → hidup
        champ = "0x" + "3" * 40       # lunglai, tapi 5 win → hidup
        never = "0x" + "4" * 40       # last_seen=0 → tak tersentuh prune
        for a, wc in ((stale, 1), (fresh, 1), (champ, 5), (never, 0)):
            db.add_insider_wallet(a, win_count=wc)
        db.set_insider_last_seen = None  # bukan API; pakai SQL langsung
        import sqlite3
        with db._lock, db._conn() as c:
            c.execute("UPDATE insider_wallets SET last_seen=? WHERE address=?",
                      (now - 20 * 86400, stale))
            c.execute("UPDATE insider_wallets SET last_seen=? WHERE address=?",
                      (now - 1 * 86400, fresh))
            c.execute("UPDATE insider_wallets SET last_seen=? WHERE address=?",
                      (now - 20 * 86400, champ))
            c.execute("UPDATE insider_wallets SET last_seen=0 WHERE address=?",
                      (never,))

        n = db.prune_stale_insiders(14)
        assert n == 1, n
        assert _insider_row(stale)["enabled"] == 0
        assert _insider_row(fresh)["enabled"] == 1
        assert _insider_row(champ)["enabled"] == 1
        assert _insider_row(never)["enabled"] == 1
        print("PASS prune_stale_insiders")
    finally:
        os.unlink(path)
        config.DB_PATH = old


# =========================================================
# 4. rotate() — ringkasan & rekrut (dengan network stub)
# =========================================================

def test_rotate_recruits_and_reports():
    path, old = _fresh_db()
    try:
        db.init()
        now = int(time.time())
        boncos = "0x" + "9" * 40
        db.add_insider_wallet(boncos, win_count=0)
        with db._lock, db._conn() as c:
            c.execute("UPDATE insider_wallets SET last_seen=? WHERE address=?",
                      (now - 30 * 86400, boncos))

        w1, w2 = "0x" + "5" * 40, "0x" + "6" * 40
        runner1 = "0x" + "7" * 40
        runner2 = "0x" + "8" * 40

        rot.si = type("SI", (), {})()          # namespace stub
        rot.si.fetch_gainers = (lambda limit=15: [runner1, runner2])
        rot.si.fetch_early_buyers = (lambda ca, max_buyers=50:
                                     [] if ca == runner1 else [])
        weak = "0x" + "a" * 40
        rot.si.cross_check = (lambda cas: [
            {"address": w1, "score": 4.0, "n_ca": 2,
             "verified_cas": [runner1, runner2], "gt_only_cas": [],
             "sources": f"{runner1}|bs+gt,{runner2}|bs+gt",
             "qualifies": True, "needs_retry": False},
            {"address": w2, "score": 2.0, "n_ca": 2,
             "verified_cas": [runner1, runner2], "gt_only_cas": [],
             "sources": f"{runner1}|bs,{runner2}|bs",
             "qualifies": True, "needs_retry": False},
            # tidak lolos gate → TIDAK boleh direkrut
            {"address": weak, "score": 1.0, "n_ca": 2,
             "verified_cas": [runner1, runner2], "gt_only_cas": [],
             "sources": f"{runner1}|bs,{runner2}|bs",
             "qualifies": False, "needs_retry": False},
        ])

        summary = rot.rotate(runner_limit=2, max_inactive_days=14)
        assert summary["disabled"] == 1, summary       # boncos di-prune
        assert summary["qualified"] == 2, summary      # gate skor 2 sumber
        assert summary["added"] == 2, summary          # 2 rekrutan baru
        assert summary["total_active"] == 2, summary   # boncos mati, 2 hidup
        assert _insider_row(w1)["label"] == "auto_rotate"
        assert _insider_row(w1)["score"] == 4.0, _insider_row(w1)
        assert _insider_row(weak) is None, "wallet di bawah gate ikut direkrut"
        assert _insider_row(boncos)["enabled"] == 0
        assert w1 in db.get_active_insiders() and w2 in db.get_active_insiders()

        # putaran kedua: wallet sama → added=0 (update, bukan duplikat)
        summary2 = rot.rotate(runner_limit=2)
        assert summary2["added"] == 0, summary2
        assert summary2["total_active"] == 2, summary2
        print("PASS rotate() prune + rekrut + ringkasan")
    finally:
        os.unlink(path)
        config.DB_PATH = old


if __name__ == "__main__":
    test_record_loss_then_disable()
    test_record_win_resets_loss()
    test_record_normalizes_case_and_unknown()
    test_insider_alert_only_no_pipeline()
    test_legacy_pipeline_embeds_insider_addr()
    test_prune_stale_insiders()
    test_rotate_recruits_and_reports()
    print("ALL INSIDER ROTATION TESTS PASSED ✅")
