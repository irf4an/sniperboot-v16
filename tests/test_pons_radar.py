"""Unit test Radar Pons V2 (alert-only).

Fokus: bagian MURNI (parser, heuristik, komposisi teks) + lapisan DB +
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

from sniper import config, db, pons_radar


def _topic(addr):
    return bytes.fromhex("0" * 24 + addr.replace("0x", ""))


class TestParser(unittest.TestCase):
    def test_parse_valid_log(self):
        log = {
            "topics": [_topic("11" * 32), _topic("aa" * 20),
                       _topic("bb" * 20), _topic("cc" * 20)],
            "blockNumber": 123,
        }
        rec = pons_radar.parse_launch_log(log)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["ca"], "0x" + "aa" * 20)
        self.assertEqual(rec["curve"], "0x" + "bb" * 20)
        self.assertEqual(rec["deployer"], "0x" + "cc" * 20)
        self.assertEqual(rec["block"], 123)

    def test_parse_rejects_short_topics(self):
        self.assertIsNone(pons_radar.parse_launch_log({"topics": [_topic("11" * 32)]}))
        self.assertIsNone(pons_radar.parse_launch_log({}))

    def test_parse_rejects_zero_token(self):
        log = {"topics": [_topic("11" * 32), _topic("00" * 20),
                          _topic("bb" * 20), _topic("cc" * 20)],
               "blockNumber": 1}
        self.assertIsNone(pons_radar.parse_launch_log(log))

    def test_parse_rejects_token_equal_deployer(self):
        log = {"topics": [_topic("11" * 32), _topic("aa" * 20),
                          _topic("bb" * 20), _topic("aa" * 20)],
               "blockNumber": 1}
        self.assertIsNone(pons_radar.parse_launch_log(log))


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
        self.assertTrue(db.add_pons_token(ca, deployer="0x" + "bb" * 20,
                                          curve="0x" + "cc" * 20,
                                          launch_block=10, launch_ts=now))
        self.assertFalse(db.add_pons_token(ca, deployer="0x" + "bb" * 20,
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
        db.add_pons_token(ca1, launch_ts=now)
        db.add_pons_token(ca2, launch_ts=now - 7200)
        db.reject_pons(ca1, "skor rendah", score=10)
        self.assertEqual(db.pons_stats()["rejected"], 1)
        expired = db.expire_pons_queue(max_age_sec=3600)
        self.assertEqual(expired, 1)
        self.assertEqual(db.pons_stats()["expired"], 1)

    def test_name_cluster_and_deployer_history(self):
        now = int(time.time())
        dep = "0x" + "dd" * 20
        for i in range(3):
            db.add_pons_token("0x" + f"{i:02x}" * 20, deployer=dep,
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
            db.add_pons_token(ca, deployer="0x" + f"{i + 50:02x}" * 20,
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
            db.add_pons_token(ca, deployer="0x" + f"{i + 50:02x}" * 20,
                              launch_ts=now - ts_offset)
        return db.get_pons_pending()

    def test_block_buy_flag_semantics(self):
        ca_ok = "0x" + "aa" * 20
        ca_no = "0x" + "bb" * 20
        db.add_pons_token(ca_ok, launch_ts=int(time.time()))
        db.add_pons_token(ca_no, launch_ts=int(time.time()))
        db.reject_pons(ca_ok, "LLM BUANG: sindikat", block_buy=True)
        db.reject_pons(ca_no, "cap alert per jam penuh", block_buy=False)
        self.assertEqual(db.get_pons_blocked_cas(), {ca_ok})

    def test_low_score_blocks_but_cap_does_not(self):
        """Skor rendah = penilaian → diblokir. Cap per jam = operasional → tidak."""
        pending = self._seed(1)
        fake = {pending[0]["ca"]: {"symbol": "sbdxcnm", "name": "ascasd",
                                   "total_supply": 1e9}}
        with mock.patch.object(pons_radar, "_enrich_many", return_value=fake), \
             mock.patch.object(pons_radar, "llm_verdict", return_value=None):
            alerts = pons_radar.process_candidates(pending)
        self.assertEqual(alerts, [])
        self.assertIn(pending[0]["ca"], db.get_pons_blocked_cas())

        with db._conn() as c:
            c.execute("DELETE FROM pons_queue")
        db.set_("pons_alert_max_per_hour", "1")
        pending = self._seed(2, ts_offset=20)
        fake = {r["ca"]: {"symbol": "PONKE", "name": f"Ponke {i}",
                          "total_supply": 1e9}
                for i, r in enumerate(pending)}
        with mock.patch.object(pons_radar, "_enrich_many", return_value=fake), \
             mock.patch.object(pons_radar, "llm_verdict", return_value=None):
            alerts = pons_radar.process_candidates(pending)
        self.assertEqual(len(alerts), 1)
        blocked = db.get_pons_blocked_cas()
        self.assertEqual(blocked, set(), f"cap tidak boleh mem-blacklist: {blocked}")

    def test_is_blacklisted_memory_and_refresh(self):
        ca = "0x" + "cc" * 20
        db.add_pons_token(ca, launch_ts=int(time.time()))
        db.reject_pons(ca, "skor rendah", block_buy=True)
        pons_radar._blacklist = set()
        pons_radar._blacklist_ts = 0.0
        pons_radar.refresh_blacklist(force=True)
        self.assertTrue(pons_radar.is_blacklisted(ca))
        self.assertTrue(pons_radar.is_blacklisted(ca.upper()))
        self.assertFalse(pons_radar.is_blacklisted("0x" + "dd" * 20))

    def test_listener_pons_veto_and_fail_open(self):
        from sniper import listener
        ca = "0x" + "ee" * 20
        pons_radar._blacklist = {ca}
        pons_radar._blacklist_ts = time.time()
        self.assertTrue(listener.pons_veto(ca))
        self.assertTrue(listener.pons_veto(ca.upper()))
        self.assertFalse(listener.pons_veto("0x" + "ff" * 20))

        pons_radar._blacklist_ts = 0.0
        with mock.patch.object(pons_radar, "refresh_blacklist",
                               side_effect=RuntimeError("db mati")):
            self.assertFalse(listener.pons_veto(ca))  # fail-open


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
