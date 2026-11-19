"""Unit test Radar Pons V2 (alert-only).

Fokus: bagian MURNI (heuristik, komposisi teks) + lapisan DB +
satu putaran `process_candidates` dengan RPC/LLM dipalsukan.

Tidak ada test yang menyentuh jaringan. Yang WAJIB dijaga: radar tidak
pernah menulis ke tabel `positions` dan tidak pernah memanggil
`listener.process_ca` — dijaga eksplisit di test terakhir.
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper import config, db, listener, pons_radar


class TestHeuristic(unittest.TestCase):
    def test_gibberish_symbol_penalized(self):
        pen, why = pons_radar.symbol_penalty("sbdxcnm", "ascasd")
        self.assertGreaterEqual(pen, 20)
        self.assertIn("vokal", why)

    def test_short_sane_symbol_not_penalized(self):
        self.assertEqual(pons_radar.symbol_penalty("WF", "Wander Forest")[0], 0)
        self.assertEqual(pons_radar.symbol_penalty("PONKE", "Ponke")[0], 0)

    def test_consistent_consonant_run_penalized(self):
        pen, _ = pons_radar.symbol_penalty("bcdfg", "x")
        self.assertGreater(pen, 0)

    def test_long_symbol_penalized(self):
        pen, _ = pons_radar.symbol_penalty("ABCDEFGHIJKLMNOP", "name")
        self.assertGreater(pen, 0)

    def test_empty_symbol_penalized(self):
        self.assertEqual(pons_radar.symbol_penalty("", "x")[0], 30)

    def test_base_score_clean_token(self):
        score, notes = pons_radar.base_score("PONKE", "Ponke Coin")
        self.assertEqual(score, 70)
        self.assertEqual(notes, "")

    def test_base_score_spam_cluster_lowers(self):
        clean, _ = pons_radar.base_score("PONKE", "Ponke Coin")
        spam, notes = pons_radar.base_score("sbdxcnm", "ascasd",
                                            deployer_launches=8, name_cluster=5)
        self.assertLess(spam, clean)
        self.assertIn("nama sama 5x/jam", notes)
        self.assertIn("deployer 8 launch/jam", notes)

    def test_base_score_clamped(self):
        score, _ = pons_radar.base_score("", "", deployer_launches=99, name_cluster=99)
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)


class TestCompose(unittest.TestCase):
    def test_alert_contains_core_fields(self):
        rec = {"ca": "0x" + "ab" * 20, "symbol": "TEST", "name": "Test Coin",
               "score": 82, "verdict": "AMAN", "reason": "deployer bersih",
               "deployer": "0x" + "cd" * 20, "deployer_launches": 2,
               "age_sec": 95, "launch_block": 103456789}
        text = pons_radar.compose_alert(rec)
        self.assertIn("TEST", text)
        self.assertIn("0x" + "ab" * 20, text)
        self.assertIn("82", text)
        self.assertIn("AMAN", text)
        self.assertIn("1m 35s", text)
        self.assertIn("alert-only", text)

    def test_digest_empty_is_none(self):
        self.assertIsNone(pons_radar.compose_digest([]))

    def test_digest_lists_alerts(self):
        rows = [{"symbol": "AAA", "score": 70, "verdict": "AMAN"},
                {"symbol": "BBB", "score": 65, "verdict": "WASPADA"}]
        text = pons_radar.compose_digest(rows)
        self.assertIn("2 sinyal", text)
        self.assertIn("AAA", text)
        self.assertIn("BBB", text)


class TestDBLayer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        db.init()

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_add_dedup_and_pending(self):
        now = int(time.time())
        ca = "0x" + "aa" * 20
        self.assertTrue(db.upsert_pons_curve_candidate(ca, deployer="0x" + "bb" * 20,
                                          curve="0x" + "cc" * 20,
                                          launch_block=10, launch_ts=now))
        self.assertFalse(db.upsert_pons_curve_candidate(ca, deployer="0x" + "bb" * 20,
                                           launch_block=10, launch_ts=now))
        pending = db.get_pons_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["ca"], ca)
        self.assertEqual(pending[0]["status"], "new")

        db.mark_pons_alerted(ca, symbol="AAA", name="Aaa Coin", score=77,
                             verdict="AMAN", reason="ok", llm_model="m")
        self.assertEqual(db.get_pons_pending(), [])
        st = db.pons_stats()
        self.assertEqual(st["alerted"], 1)
        self.assertEqual(st["alerts_24h"], 1)
        self.assertEqual(len(db.recent_pons_alerts()), 1)
        self.assertEqual(db.pons_alert_count_since(now - 60), 1)

    def test_reject_and_expire(self):
        now = int(time.time())
        ca1, ca2 = "0x" + "11" * 20, "0x" + "22" * 20
        db.upsert_pons_curve_candidate(ca1, launch_ts=now)
        db.upsert_pons_curve_candidate(ca2, launch_ts=now)
        # TTL dihitung dari queued_at (saat kandidat masuk antrean), bukan
        # launch_ts — token yang butuh berjam-jam mencapai ambang tetap hidup.
        with db._lock, db._conn() as c:
            c.execute("UPDATE pons_queue SET queued_at=? WHERE ca=?",
                      (now - 7200, ca2))
        db.reject_pons(ca1, "skor rendah", score=10)
        self.assertEqual(db.pons_stats()["rejected"], 1)
        expired = db.expire_pons_queue(max_age_sec=3600)
        self.assertEqual(expired, 1)
        self.assertEqual(db.pons_stats()["expired"], 1)

    def test_name_cluster_and_deployer_history(self):
        now = int(time.time())
        dep = "0x" + "dd" * 20
        for i in range(3):
            db.upsert_pons_curve_candidate("0x" + f"{i:02x}" * 20, deployer=dep,
                              launch_ts=now - i)
        db.update_pons_token("0x" + "00" * 20, name="Wander Forest")
        db.update_pons_token("0x" + "01" * 20, name="Wander Forest")
        self.assertEqual(db.pons_name_cluster("wander forest", now - 3600), 2)
        self.assertEqual(db.pons_deployer_history(dep)["launches"], 3)


class TestProcessCandidates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        db.init()
        db.set_("pons_alert_min_score", "60")
        db.set_("pons_alert_max_per_hour", "2")
        db.set_("pons_llm_on", "0")
        db.set_("pons_enrich_concurrency", "2")

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _seed(self, n):
        now = int(time.time())
        out = []
        for i in range(n):
            ca = "0x" + f"{i + 1:02x}" * 20
            db.upsert_pons_curve_candidate(ca, deployer="0x" + f"{i + 50:02x}" * 20,
                              launch_ts=now - 30)
            out.append({"ca": ca, "deployer": "0x" + f"{i + 50:02x}" * 20,
                        "launch_ts": now - 30, "launch_block": 1000 + i})
        return db.get_pons_pending()

    def test_rejects_when_rpc_fails(self):
        self._seed(1)
        with mock.patch.object(pons_radar, "_enrich_many", return_value={}):
            alerts = pons_radar.process_candidates(db.get_pons_pending())
        self.assertEqual(alerts, [])
        self.assertEqual(db.pons_stats()["rejected"], 1)

    def test_alerts_clean_token_and_respects_cap(self):
        pending = self._seed(4)
        fake = {r["ca"]: {"symbol": f"TKN{i}", "name": f"Token Nomor {i}",
                          "total_supply": 1e9}
                for i, r in enumerate(pending)}
        with mock.patch.object(pons_radar, "_enrich_many", return_value=fake), \
             mock.patch.object(pons_radar, "llm_verdict", return_value=None):
            alerts = pons_radar.process_candidates(pending)
        self.assertEqual(len(alerts), 2)  # cap pons_alert_max_per_hour = 2
        self.assertTrue(all(a["score"] >= 60 for a in alerts))
        self.assertEqual(db.pons_stats()["rejected"], 2)

    def test_name_cluster_detected_inside_one_batch(self):
        """3 koin bernama sama dalam SATU batch = sindikat spam → ditolak."""
        pending = self._seed(3)
        fake = {r["ca"]: {"symbol": "WANDER", "name": "Wander Forest",
                          "total_supply": 1e9} for r in pending}
        with mock.patch.object(pons_radar, "_enrich_many", return_value=fake), \
             mock.patch.object(pons_radar, "llm_verdict", return_value=None):
            alerts = pons_radar.process_candidates(pending)
        # base 70 − 12 (nama sama 2x lain) = 58 < ambang 60 → semua ditolak
        self.assertEqual(alerts, [])
        self.assertEqual(db.pons_stats()["rejected"], 3)

    def test_llm_buang_rejects_even_with_high_score(self):
        pending = self._seed(1)
        fake = {pending[0]["ca"]: {"symbol": "PONKE", "name": "Ponke Coin",
                                   "total_supply": 1e9}}
        with mock.patch.object(pons_radar, "_enrich_many", return_value=fake), \
             mock.patch.object(pons_radar, "llm_verdict",
                               return_value={"verdict": "BUANG", "score": 95,
                                             "reason": "sindikat spam",
                                             "model": "test"}):
            alerts = pons_radar.process_candidates(pending)
        self.assertEqual(alerts, [])
        st = db.pons_stats()
        self.assertEqual(st["rejected"], 1)
        self.assertEqual(st["alerted"], 0)

    def test_radar_never_touches_trading_tables(self):
        """Bukti keras: radar tidak membuat posisi & tidak memanggil pipa beli."""
        from sniper import listener
        pending = self._seed(1)
        fake = {pending[0]["ca"]: {"symbol": "PONKE", "name": "Ponke Coin",
                                   "total_supply": 1e9}}
        with mock.patch.object(pons_radar, "_enrich_many", return_value=fake), \
             mock.patch.object(listener, "process_ca",
                               side_effect=AssertionError("radar memanggil pipa beli!")):
            alerts = pons_radar.process_candidates(pending)
        self.assertEqual(len(alerts), 1)
        conn = db._conn()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0], 0)


class TestVetoBridge(unittest.TestCase):
    """Jembatan Radar Pons → jalur beli: hanya penilaian yang mem-blacklist."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        db.init()
        db.set_("pons_alert_min_score", "60")
        db.set_("pons_alert_max_per_hour", "2")
        db.set_("pons_llm_on", "0")
        db.set_("pons_enrich_concurrency", "2")

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _seed(self, n, ts_offset=30):
        now = int(time.time())
        for i in range(n):
            ca = "0x" + f"{i + 1:02x}" * 20
            db.upsert_pons_curve_candidate(ca, deployer="0x" + f"{i + 50:02x}" * 20,
                              launch_ts=now - ts_offset)
        return db.get_pons_pending()

    def test_block_buy_flag_semantics(self):
        """Veto SUDAH DIHAPUS: penolakan apa pun tidak boleh memblokir beli.

        `reject_pons` masih menerima argumen `block_buy` demi kompatibilitas
        skema lama, tapi radar tidak lagi memakainya — dan tidak ada lagi
        API veto yang dibaca jalur beli.
        """
        ca = "0x" + "bb" * 20
        db.upsert_pons_curve_candidate(ca, launch_ts=int(time.time()))
        db.reject_pons(ca, "cap alert per jam penuh", block_buy=False)
        self.assertFalse(hasattr(pons_radar, "is_blacklisted"))
        self.assertFalse(hasattr(pons_radar, "refresh_blacklist"))
        self.assertFalse(hasattr(db, "get_pons_blocked_cas"))

    def test_low_score_rejected_but_not_blocked(self):
        """Skor rendah = tidak dapat alert, tapi TIDAK memblokir jalur beli."""
        pending = self._seed(1)
        fake = {pending[0]["ca"]: {"symbol": "sbdxcnm", "name": "ascasd",
                                   "total_supply": 1e9}}
        with mock.patch.object(pons_radar, "_enrich_many", return_value=fake), \
             mock.patch.object(pons_radar, "llm_verdict", return_value=None):
            alerts = pons_radar.process_candidates(pending)
        self.assertEqual(alerts, [])
        with db._conn() as c:
            row = c.execute("SELECT status, COALESCE(block_buy,0) bb "
                            "FROM pons_queue WHERE ca=?",
                            (pending[0]["ca"],)).fetchone()
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(row["bb"], 0, "penolakan tidak boleh memblokir beli")

    def test_cap_per_hour_does_not_block(self):
        db.set_("pons_alert_max_per_hour", "1")
        pending = self._seed(2, ts_offset=20)
        fake = {r["ca"]: {"symbol": "PONKE", "name": f"Ponke {i}",
                          "total_supply": 1e9}
                for i, r in enumerate(pending)}
        with mock.patch.object(pons_radar, "_enrich_many", return_value=fake), \
             mock.patch.object(pons_radar, "llm_verdict", return_value=None):
            alerts = pons_radar.process_candidates(pending)
        self.assertEqual(len(alerts), 1)
        with db._conn() as c:
            blocked = c.execute(
                "SELECT COUNT(*) n FROM pons_queue WHERE COALESCE(block_buy,0)=1"
            ).fetchone()["n"]
        self.assertEqual(blocked, 0, "cap tidak boleh memblokir beli")

    def test_graduation_detection(self):
        self.assertTrue(pons_radar.is_graduated({"stage": "graduated"}))
        self.assertTrue(pons_radar.is_graduated({"graduatedAt": 1791151305}))
        self.assertFalse(pons_radar.is_graduated({"stage": "curve"}))
        self.assertFalse(pons_radar.is_graduated({}))
        self.assertFalse(pons_radar.is_graduated(None))

    def test_graduation_queue_and_mark(self):
        ca = "0x" + "ee" * 20
        db.upsert_pons_curve_candidate(ca, launch_ts=int(time.time()))
        db.mark_pons_alerted(ca, symbol="TST", score=80)
        rows = db.pons_alerted_for_graduation(limit=10)
        self.assertEqual([r["ca"] for r in rows], [ca])
        db.mark_pons_graduated(ca, pool_id="0x" + "11" * 32)
        self.assertEqual(db.pons_alerted_for_graduation(limit=10), [])

    def test_graduation_buttons_carry_buy_callback(self):
        btns = pons_radar._graduation_buttons("0x" + "ab" * 20)
        self.assertIsNotNone(btns)
        payloads = []
        for row in btns:
            for b in row:
                t = getattr(b, "type", None)
                d = getattr(t, "data", None)
                if d:
                    payloads.append(d if isinstance(d, str) else d.decode())
        self.assertIn("ponsbuy:0x" + "ab" * 20, payloads)
        # Telegram membatasi callback_data 64 byte.
        self.assertTrue(all(len(p) <= 64 for p in payloads))
        # Tombol konfirmasi harus ada dan juga muat.
        ok = ("ponsbuyok:0x" + "ab" * 20)
        self.assertLessEqual(len(ok), 64)

    def test_stale_alerts_are_suppressed_not_sent(self):
        """Backlog historis tidak boleh jadi spam tombol beli."""
        old = "0x" + "01" * 20
        fresh = "0x" + "02" * 20
        db.upsert_pons_curve_candidate(old, launch_ts=int(time.time()))
        db.upsert_pons_curve_candidate(fresh, launch_ts=int(time.time()))
        db.mark_pons_alerted(old, symbol="OLD", score=70)
        db.mark_pons_alerted(fresh, symbol="NEW", score=70)
        with db._conn() as c:
            c.execute("UPDATE pons_queue SET alerted_at=? WHERE ca=?",
                      (int(time.time()) - 5 * 86400, old))
        n = db.suppress_stale_pons_graduation(86400)
        self.assertEqual(n, 1)
        rows = db.pons_alerted_for_graduation(limit=10, max_age_sec=86400)
        self.assertEqual([r["ca"] for r in rows], [fresh])


class TestCurveIntake(unittest.TestCase):
    """Pengujian intake kurva Pons V2: API progress filter & alert format."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        db.init()
        db.set_("pons_min_progress", "0.75")

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_scan_curve_progress_filters_threshold(self):
        fake_items = [
            {"address": "0x" + "11" * 20, "symbol": "HIGH", "name": "High Progress",
             "progress": 0.85, "raisedQuote": 3.5, "volumeUsd": 12000, "tradeCount": 150},
            {"address": "0x" + "22" * 20, "symbol": "BORDER", "name": "Borderline",
             "progress": 0.75, "raisedQuote": 3.15, "volumeUsd": 8000, "tradeCount": 90},
            {"address": "0x" + "33" * 20, "symbol": "LOW", "name": "Low Progress",
             "progress": 0.50, "raisedQuote": 2.1, "volumeUsd": 3000, "tradeCount": 40},
            {"address": "0x" + "44" * 20, "symbol": "ZERO", "name": "Zero Progress",
             "progress": 0.0, "raisedQuote": 0.0, "volumeUsd": 0, "tradeCount": 0},
        ]
        with mock.patch.object(pons_radar, "fetch_curve_candidates", return_value=fake_items):
            count = pons_radar.scan_curve_progress()
        self.assertEqual(count, 2)  # hanya HIGH (0.85) dan BORDER (0.75) yang lolos
        pending = db.get_pons_pending()
        self.assertEqual(len(pending), 2)
        symbols = [p["symbol"] for p in pending]
        self.assertIn("HIGH", symbols)
        self.assertIn("BORDER", symbols)
        self.assertNotIn("LOW", symbols)
        self.assertNotIn("ZERO", symbols)

    def test_compose_alert_includes_curve_metrics(self):
        rec = {
            "ca": "0x" + "55" * 20,
            "symbol": "BCAT",
            "name": "Boo Cat",
            "progress": 0.954,
            "raised_eth": 4.01,
            "threshold_quote": 4.2,
            "quote_symbol": "ETH",
            "quote_asset_class": "native",
            "volume_usd": 58500,
            "trade_count": 1838,
            "market_cap_usd": 44200,
            "creator_tax_bps": 0,
            "score": 85,
            "verdict": "AMAN",
            "reason": "momentum kuat mendekati graduasi",
            "deployer": "0x" + "66" * 20,
        }
        text = pons_radar.compose_alert(rec)
        self.assertIn("95.4%", text)
        self.assertIn("4.01/4.2 ETH", text)
        self.assertIn("58,500", text)
        self.assertIn("1,838 trade", text)
        self.assertIn("44,200", text)
        self.assertIn("Tax 0.0%", text)
        buttons = pons_radar._explorer_buttons(rec["ca"])
        if buttons:
            urls = []
            for row in buttons:
                for b in row:
                    url = getattr(b, "url", None) or getattr(getattr(b, "type", None), "url", None) or ""
                    urls.append(url)
            self.assertTrue(any("ponsfamily.com/launchpad" in u for u in urls))


class TestNewLaunchMode(unittest.TestCase):
    """Mode `pons_newest_mode` — alert setiap launch baru (sort=newest)."""

    def setUp(self):
        # ISOLASI WAJIB: tanpa ini test menulis ke DB LIVE (hoodsniper.db)
        # dan menyisakan CA palsu yang lalu di-fetch radar → error 404.
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        db.init()

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _item(self, ca, age_sec, prog=0.0, sym="NEW"):
        return {
            "address": ca, "symbol": sym, "name": sym + " Token",
            "deployer": "0x" + "d1" * 20, "curve": "0x" + "c1" * 20,
            "totalSupply": 1e9, "launchedBlock": 123, 
            "createdAt": int(time.time()) - age_sec,
            "progress": prog, "volumeUsd": 0.0, "tradeCount": 0,
            "marketCapUsd": 5000.0, "creatorTaxBps": 100,
            "socials": {}, "quote": {"symbol": "ETH", "assetClass": "native"},
            "thresholdQuote": 5.0, "raisedQuote": 0.0, "raisedUsd": 0.0,
        }

    def test_fresh_launch_detected_old_skipped(self):
        fresh = "0x" + "a1" * 20
        old = "0x" + "a2" * 20
        items = [self._item(old, 4000, sym="OLD"),
                 self._item(fresh, 60, sym="FRESH")]
        db.set_("pons_new_max_age_sec", "900")
        db.set_("pons_new_alert_max_per_hour", "0")
        with mock.patch.object(pons_radar, "fetch_curve_candidates",
                               return_value=items), \
             mock.patch.object(db, "pons_deployer_history",
                               return_value={"launches": 0}):
            out = pons_radar.scan_new_launches()
        cas = [r["ca"] for r in out]
        self.assertIn(fresh, cas)
        self.assertNotIn(old, cas, "token 66 menit bukan launch baru")

    def test_already_alerted_not_repeated(self):
        ca = "0x" + "b1" * 20
        db.set_("pons_new_max_age_sec", "900")
        db.set_("pons_new_alert_max_per_hour", "0")
        db.upsert_pons_curve_candidate(ca, launch_ts=int(time.time()))
        db.mark_pons_alerted(ca, symbol="X", score=0, verdict="BARU")
        with mock.patch.object(pons_radar, "fetch_curve_candidates",
                               return_value=[self._item(ca, 30)]), \
             mock.patch.object(db, "pons_deployer_history",
                               return_value={"launches": 0}):
            self.assertEqual(pons_radar.scan_new_launches(), [])

    def test_cap_limits_volume(self):
        db.set_("pons_new_max_age_sec", "900")
        db.set_("pons_new_alert_max_per_hour", "2")
        items = [self._item("0x" + ("c%d" % i).ljust(2, "0") * 20, 10 * i,
                           sym="T%d" % i) for i in range(6)]
        with mock.patch.object(pons_radar, "fetch_curve_candidates",
                               return_value=items), \
             mock.patch.object(db, "pons_deployer_history",
                               return_value={"launches": 0}):
            out = pons_radar.scan_new_launches()
        self.assertEqual(len(out), 2)

    def test_compose_new_launch_text(self):
        rec = {"ca": "0x" + "zz" * 20, "symbol": "PET", "name": "Petcoin",
               "deployer": "0x" + "ab" * 20, "age_sec": 245,
               "progress": 0.002, "volume_usd": 0.0, "trade_count": 0,
               "market_cap_usd": 4200.0, "creator_tax_bps": 100,
               "deployer_launches": 3}
        txt = pons_radar.compose_new_launch(rec)
        self.assertIn("LAUNCH BARU", txt)
        self.assertIn("PET", txt)
        self.assertIn("4m 5s", txt)
        self.assertIn("alert-only", txt)


class TestPonsAgeGate(unittest.TestCase):
    """Bypass gate UMUR untuk token Pons — terukur, bukan asumsi."""

    def _pair(self, age_sec, liq=9500.0, mcap=14000.0):
        return {
            "pairCreatedAt": int((time.time() - age_sec) * 1000),
            "liquidity": {"usd": liq},
            "fdv": mcap, "marketCap": mcap,
            "priceUsd": "0.001",
            "baseToken": {"symbol": "T", "name": "T"},
        }

    def test_gate_normal_rejects_old_pool(self):
        ok, tag, _ = listener.goldilocks_eval(self._pair(4600))
        self.assertFalse(ok)
        self.assertIn("age_", tag)

    def test_pons_bypasses_upper_age_bound(self):
        ok, tag, metr = listener.goldilocks_eval(self._pair(4600), pons_token=True)
        self.assertTrue(ok, f"token Pons umur 77 menit harus lolos, tag={tag}")
        self.assertTrue(metr["pons_bypass_age"])

    def test_pons_still_blocked_by_low_liquidity(self):
        ok, tag, _ = listener.goldilocks_eval(self._pair(4600, liq=100.0),
                                              pons_token=True)
        self.assertFalse(ok)
        self.assertEqual(tag, "liq_rendah")

    def test_pons_still_blocked_by_mcap_range(self):
        ok, tag, _ = listener.goldilocks_eval(
            self._pair(4600, liq=9500.0, mcap=200000.0), pons_token=True)
        self.assertFalse(ok)
        self.assertEqual(tag, "mcap_out_of_range")

    def test_pons_age_gate_removed_entirely(self):
        # Operator: "umur pool untuk token dari pons radar hapus aja".
        # Batas bawah DAN atas tidak berlaku untuk token Pons — pool 2 detik
        # pun tidak ditolak karena radar baru alert setelah token graduate.
        ok, tag, _ = listener.goldilocks_eval(self._pair(2), pons_token=True)
        self.assertTrue(ok, f"umur tidak boleh menahan token Pons, tag={tag}")

    def test_pons_age_unknown_does_not_block(self):
        # pairCreatedAt hilang → token Pons tetap lolos tahap umur.
        p = self._pair(4600)
        p.pop("pairCreatedAt", None)
        ok, tag, _ = listener.goldilocks_eval(p, pons_token=True)
        self.assertTrue(ok, f"age_unknown tidak boleh menahan token Pons, tag={tag}")

    def test_non_pons_age_gate_still_active(self):
        # Gate umur WAJIB tetap berlaku untuk sinyal non-Pons.
        ok, tag, _ = listener.goldilocks_eval(self._pair(2), pons_token=False)
        self.assertFalse(ok)
        self.assertIn("age_", tag)


if __name__ == "__main__":
    unittest.main(verbosity=2)
