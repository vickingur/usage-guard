import os, sys, time, unittest, tempfile, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class GuardlibTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["UG_DIR"] = self.tmp.name
        for mod in [m for m in sys.modules if m == "guardlib"]:
            del sys.modules[mod]
        import guardlib
        self.g = guardlib

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("UG_DIR", None)

    def test_load_config_returns_defaults_when_no_file_exists(self):
        cfg = self.g.load_config()
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["threshold_5h"], 95.0)
        self.assertEqual(cfg["threshold_7d"], 90.0)

    def test_stored_config_overrides_only_the_keys_it_sets(self):
        self.g.save_config({"threshold_5h": 50.0})
        cfg = self.g.load_config()
        self.assertEqual(cfg["threshold_5h"], 50.0)
        self.assertEqual(cfg["threshold_7d"], 90.0)

    def test_corrupt_config_file_falls_back_to_defaults(self):
        self.g.config_path().write_text("{not json")
        self.assertEqual(self.g.load_config()["threshold_5h"], 95.0)

    def test_atomic_write_leaves_no_temp_files_behind(self):
        self.g.atomic_write(self.g.cache_path(), {"a": 1})
        names = [p.name for p in Path(self.tmp.name).iterdir()]
        self.assertEqual(names, ["usage.json"])

    def test_violations_empty_when_usage_below_threshold(self):
        now = 1_000_000
        cache = {"ts": now, "five_hour": {"used_percentage": 10.0, "resets_at": now + 3600}}
        self.assertEqual(self.g.violations(cache, self.g.load_config(), now), [])

    def test_violations_reports_window_at_or_over_threshold(self):
        now = 1_000_000
        cache = {"ts": now, "five_hour": {"used_percentage": 95.0, "resets_at": now + 3600}}
        v = self.g.violations(cache, self.g.load_config(), now)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0].label, "5h")
        self.assertEqual(v[0].resets_at, now + 3600)

    def test_violations_ignores_window_whose_reset_time_has_passed(self):
        now = 1_000_000
        cache = {"ts": now, "five_hour": {"used_percentage": 99.0, "resets_at": now - 1}}
        self.assertEqual(self.g.violations(cache, self.g.load_config(), now), [])

    def test_violations_reports_both_windows_when_both_over(self):
        now = 1_000_000
        cache = {"ts": now,
                 "five_hour": {"used_percentage": 96.0, "resets_at": now + 60},
                 "seven_day": {"used_percentage": 99.0, "resets_at": now + 600}}
        labels = sorted(v.label for v in self.g.violations(cache, self.g.load_config(), now))
        self.assertEqual(labels, ["5h", "7d"])

    def test_cache_is_stale_past_the_configured_age(self):
        cfg = self.g.load_config()
        now = 1_000_000
        self.assertFalse(self.g.is_stale({"ts": now - 599}, cfg, now))
        self.assertTrue(self.g.is_stale({"ts": now - 601}, cfg, now))

    def test_missing_cache_counts_as_stale(self):
        self.assertTrue(self.g.is_stale(None, self.g.load_config(), 1_000_000))

    def test_fmt_duration_is_compact_at_each_scale(self):
        self.assertEqual(self.g.fmt_duration(30), "<1m")
        self.assertEqual(self.g.fmt_duration(90), "1m")
        self.assertEqual(self.g.fmt_duration(3600 * 4 + 720), "4h12m")
        self.assertEqual(self.g.fmt_duration(86400 * 5 + 3600 * 3), "5d3h")


if __name__ == "__main__":
    unittest.main()
