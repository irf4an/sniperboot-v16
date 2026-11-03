"""Unit test Adaptive Auto-Tuner (Opsi A) — usulan parameter + tombol persetujuan."""
import sys
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper import agent, config, db  # noqa: E402


class TestAutoTuner(unittest.TestCase):
    def setUp(self):
        # simpan nilai asli
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
        # key di SETTING_DEFAULTS diizinkan
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
        with mock.patch.object(agent, "_call", return_value=fake_llm_resp):
            lessons, proposals = agent.derive_lessons()

            self.assertEqual(len(lessons), 2)
            self.assertEqual(lessons[0], "Pola 1: pool <20s sering rug")

            # illegal_param harus dibuang
            self.assertEqual(len(proposals), 1)
            p = proposals[0]
            self.assertEqual(p["param"], "goldilocks_min_age_sec")

            val_int = int(p["val"])
            self.assertLessEqual(val_int, 33)
            self.assertGreaterEqual(val_int, 10)


if __name__ == "__main__":
    unittest.main()
