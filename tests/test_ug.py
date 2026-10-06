import json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UG = ROOT / "ug"


class UgTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "UG_DIR": str(self.dir), "NO_COLOR": "1"}

    def tearDown(self):
        self.tmp.cleanup()

    def ug(self, *args):
        return subprocess.run([sys.executable, str(UG), *args], capture_output=True,
                              text=True, env=self.env, timeout=15)

    def config(self):
        return json.loads((self.dir / "config.json").read_text())

    def write_cache(self, pct=42.0, resets_in=3600):
        (self.dir / "usage.json").write_text(json.dumps({
            "ts": time.time(),
            "five_hour": {"used_percentage": pct, "resets_at": int(time.time() + resets_in)},
        }))

    # --- on / off -----------------------------------------------------------

    def test_off_disables_the_guard(self):
        self.assertEqual(self.ug("off").returncode, 0)
        self.assertFalse(self.config()["enabled"])

    def test_on_reenables_the_guard(self):
        self.ug("off")
        self.assertEqual(self.ug("on").returncode, 0)
        self.assertTrue(self.config()["enabled"])

    # --- thresholds ---------------------------------------------------------

    def test_threshold_sets_the_five_hour_window(self):
        self.assertEqual(self.ug("threshold", "5h", "80").returncode, 0)
        self.assertEqual(self.config()["threshold_5h"], 80.0)

    def test_threshold_sets_the_weekly_window(self):
        self.assertEqual(self.ug("threshold", "7d", "72.5").returncode, 0)
        self.assertEqual(self.config()["threshold_7d"], 72.5)

    def test_threshold_setting_one_window_leaves_the_other_untouched(self):
        self.ug("threshold", "5h", "50")
        out = self.ug("config").stdout
        self.assertEqual(json.loads(out)["threshold_7d"], 90.0)

    def test_threshold_with_no_arguments_reports_both_windows(self):
        proc = self.ug("threshold")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("5h", proc.stdout)
        self.assertIn("7d", proc.stdout)

    def test_threshold_rejects_an_unknown_window(self):
        proc = self.ug("threshold", "3d", "80")
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.dir / "config.json").exists())

    def test_threshold_rejects_a_non_numeric_value(self):
        proc = self.ug("threshold", "5h", "high")
        self.assertNotEqual(proc.returncode, 0)

    def test_threshold_rejects_a_value_outside_zero_to_one_hundred(self):
        self.assertNotEqual(self.ug("threshold", "5h", "150").returncode, 0)
        self.assertNotEqual(self.ug("threshold", "5h", "-5").returncode, 0)

    # --- release ------------------------------------------------------------

    def test_release_records_a_timestamp_the_hook_can_see(self):
        self.assertEqual(self.ug("release").returncode, 0)
        state = json.loads((self.dir / "state.json").read_text())
        self.assertAlmostEqual(state["release_at"], time.time(), delta=10)

    # --- status / config ----------------------------------------------------

    def test_status_reports_enabled_state_and_thresholds(self):
        self.write_cache()
        proc = self.ug("status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("on", proc.stdout)
        self.assertIn("95", proc.stdout)

    def test_status_shows_current_usage_from_the_cache(self):
        self.write_cache(pct=42.0)
        self.assertIn("42", self.ug("status").stdout)

    def test_status_works_with_no_cache_present(self):
        proc = self.ug("status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no usage data", proc.stdout.lower())

    def test_status_reports_an_active_hold(self):
        self.write_cache(pct=99.0)
        (self.dir / "blocked.json").write_text(json.dumps(
            {"until": int(time.time() + 600), "label": "5h", "pct": 99.0}))
        self.assertIn("HOLD", self.ug("status").stdout)

    def test_status_ignores_an_expired_hold(self):
        self.write_cache(pct=99.0)
        (self.dir / "blocked.json").write_text(json.dumps(
            {"until": int(time.time() - 5), "label": "5h", "pct": 99.0}))
        self.assertNotIn("HOLD", self.ug("status").stdout)

    def test_config_emits_valid_json_of_effective_settings(self):
        cfg = json.loads(self.ug("config").stdout)
        self.assertEqual(cfg["threshold_5h"], 95.0)
        self.assertTrue(cfg["enabled"])

    # --- argument handling --------------------------------------------------

    def test_bare_invocation_prints_usage_and_succeeds(self):
        proc = self.ug()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("release", proc.stdout)

    def test_unknown_command_fails_with_a_message(self):
        proc = self.ug("frobnicate")
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(proc.stderr.strip())


if __name__ == "__main__":
    unittest.main()
