"""Unit test Adaptive Auto-Tuner (Opsi A) — usulan parameter + tombol persetujuan + auto-dismiss 5m."""
import asyncio
import sys
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper import agent, config, control_bot, db  # noqa: E402


class TestAutoTuner(unittest.TestCase):
    def setUp(self):
        self.orig_min_age = db.get("goldilocks_min_age_sec")
        self.orig_tp1 = db.get("goldilocks_tp1")

    def tearDown(self):
        db.set_("goldilocks_min_age_sec", str(self.orig_min_age))
        db.set_("goldilocks_tp1", str(self.orig_tp1))

    def test_tunable_bounds_exist(self):
        self.assertIn("goldilocks_min_age_sec", agent.TUNABLE_BOUNDS)
        self.assertIn("goldilocks_tp1", agent.TUNABLE_BOUNDS)
        self.assertIn("goldilocks_max_mcap", agent.TUNABLE_BOUNDS)
        self.assertIn("agent_max_drift", agent.TUNABLE_BOUNDS)

    def test_applyset_allowed_keys(self):
        self.assertIn("goldilocks_min_age_sec", config.SETTING_DEFAULTS)
        db.set_("goldilocks_min_age_sec", "25")
        self.assertEqual(int(float(db.get("goldilocks_min_age_sec"))), 25)

    def test_derive_lessons_parse_and_clamp(self):
        fake_llm_resp = """{
            "lessons": ["Pola 1: pool <20s sering rug", "Pola 2: mcap aman"],
            "proposals": [
                {"param": "goldilocks_min_age_sec", "val": 999, "reason": "naikkan umur pool"},
                {"param": "illegal_param", "val": 123, "reason": "injeksi dilarang"}
            ]
        }"""
        fake_trades = [{"symbol": f"COIN{i}", "status": "closed_tp2", "eth_spent": 0.002, "realized_eth": 0.003, "tp1_done": 1, "entry_reason": "test"} for i in range(15)]
        with mock.patch.object(db, "closed_positions_with_decisions", return_value=fake_trades):
            with mock.patch.object(agent, "_call", return_value=fake_llm_resp):
                lessons, proposals = agent.derive_lessons()

                self.assertEqual(len(lessons), 2)
                self.assertEqual(lessons[0], "Pola 1: pool <20s sering rug")

                self.assertEqual(len(proposals), 1)
                p = proposals[0]
                self.assertEqual(p["param"], "goldilocks_min_age_exp" if "goldilocks_min_age_exp" in p["param"] else "goldilocks_min_age_sec")

                val_int = int(p["val"])
                self.assertLessEqual(val_int, 33)
                self.assertGreaterEqual(val_int, 10)

    def test_schedule_auto_dismiss_timeout(self):
        """Uji auto-dismiss jika tidak ada aksi dalam interval batas waktu."""
        class FakeMessage:
            def __init__(self):
                self.id = 1234
                self.chat_id = 999
                self.text = "Usulan Tuning"
                self.button_count = 2
                self.client = mock.AsyncMock()

        fake_msg = FakeMessage()
        fake_latest = FakeMessage()
        fake_latest.edit = mock.AsyncMock()
        fake_msg.client.get_messages = mock.AsyncMock(return_value=fake_latest)

        async def run_test():
            await control_bot.schedule_auto_dismiss(fake_msg, timeout_sec=0.01)

        asyncio.run(run_test())

        fake_latest.edit.assert_awaited_once()
        args, kwargs = fake_latest.edit.call_args
        self.assertIn("diabaikan otomatis", args[0])
        self.assertIsNone(kwargs.get("buttons"))

    def test_schedule_auto_dismiss_already_handled(self):
        """Jika operator sudah klik (button_count == 0), auto-dismiss tidak mengedit lagi."""
        class FakeMessage:
            def __init__(self):
                self.id = 1234
                self.chat_id = 999
                self.text = "Usulan Diterapkan"
                self.button_count = 0
                self.client = mock.AsyncMock()

        fake_msg = FakeMessage()
        fake_latest = FakeMessage()
        fake_latest.edit = mock.AsyncMock()
        fake_msg.client.get_messages = mock.AsyncMock(return_value=fake_latest)

        async def run_test():
            await control_bot.schedule_auto_dismiss(fake_msg, timeout_sec=0.01)

        asyncio.run(run_test())

        fake_latest.edit.assert_not_awaited()


    def test_count_closed_matches_positions_table(self):
        """Pemicu 15-close pakai hitungan status closed* yang sebenarnya."""
        import sqlite3
        c = sqlite3.connect(config.DB_PATH)
        try:
            real = c.execute(
                "SELECT COUNT(*) FROM positions WHERE status LIKE 'closed%'").fetchone()[0]
        finally:
            c.close()
        self.assertEqual(db.count_closed(), real)
        # 391 closed → pemicu berikutnya di kelipatan 15
        self.assertGreater(db.count_closed(), 0)


if __name__ == "__main__":
    unittest.main()
