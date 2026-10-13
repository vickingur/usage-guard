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

    def test_an_old_config_with_shared_keys_only_reads_the_same_for_every_vendor(self):
        self.g.config_path().write_text(json.dumps({"enabled": False, "pace_mode": "delay", "threshold_5h": 80}))
        shared = self.g.load_config()
        self.assertEqual((shared["enabled"], shared["pace_mode"], shared["threshold_5h"]), (False, "delay", 80))
        self.assertEqual(self.g.load_config("claude"), shared)
        self.assertEqual(self.g.load_config("codex"), shared)

    def test_a_vendor_override_beats_the_shared_key_for_that_vendor_alone(self):
        self.g.config_path().write_text(json.dumps({
            "enabled": True, "pace_mode": "hold", "threshold_7d": 80,
            "claude": {"enabled": False}, "codex": {"pace_mode": "delay"}}))
        claude, codex, shared = self.g.load_config("claude"), self.g.load_config("codex"), self.g.load_config()
        self.assertEqual((claude["enabled"], claude["pace_mode"], claude["threshold_7d"]), (False, "hold", 80))
        self.assertEqual((codex["enabled"], codex["pace_mode"], codex["threshold_7d"]), (True, "delay", 80))
        self.assertEqual((shared["enabled"], shared["pace_mode"]), (True, "hold"))
        self.assertNotIn("claude", shared)
        self.assertEqual(self.g.overrides("claude"), {"enabled": False})

    def test_saving_for_a_vendor_writes_its_override_and_leaves_the_rest(self):
        self.g.save_config({"threshold_5h": 80.0})
        cfg = self.g.save_config({"enabled": False}, "claude")
        self.assertFalse(cfg["enabled"])
        self.assertEqual(json.loads(self.g.config_path().read_text()), {"threshold_5h": 80.0, "claude": {"enabled": False}})
        self.assertTrue(self.g.load_config("codex")["enabled"])

    def test_a_shared_save_clears_that_key_from_both_overrides(self):
        self.g.save_config({"enabled": False}, "claude")
        self.g.save_config({"enabled": False, "pace_mode": "delay"}, "codex")
        self.g.save_config({"enabled": True})
        self.assertEqual(json.loads(self.g.config_path().read_text()), {"codex": {"pace_mode": "delay"}, "enabled": True})
        self.assertTrue(self.g.load_config("claude")["enabled"])
        self.assertTrue(self.g.load_config("codex")["enabled"])

    def test_a_vendor_set_back_to_the_shared_value_follows_it_again(self):
        self.g.save_config({"enabled": False}, "claude")
        self.g.save_config({"enabled": False}, "codex")
        self.g.save_config({"enabled": True}, "claude")
        self.assertEqual(json.loads(self.g.config_path().read_text()), {"codex": {"enabled": False}})

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
                 "seven_day": {"used_percentage": 99.0, "resets_at": now + 2 * 86400}}
        labels = sorted(v.label for v in self.g.violations(cache, self.g.load_config(), now))
        self.assertEqual(labels, ["5h", "7d"])

    def test_the_weekly_hold_level_climbs_to_100_over_the_last_day(self):
        cfg = self.g.load_config()
        reset = 1_000_000
        self.assertEqual(self.g.hold_level(cfg, "seven_day", reset, reset - 3 * 86400), 90.0)
        self.assertEqual(self.g.hold_level(cfg, "seven_day", reset, reset - 12 * 3600), 95.0)
        self.assertEqual(self.g.hold_level(cfg, "seven_day", reset, reset), 100.0)
        self.assertEqual(self.g.hold_level(cfg, "five_hour", reset, reset - 60), 95.0)
        cache = {"ts": 0, "seven_day": {"used_percentage": 99.0, "resets_at": reset}}
        self.assertEqual(self.g.violations(cache, cfg, reset - 600), [])  # ten minutes before the reset, 99% is allowed
        self.assertEqual([v.label for v in self.g.violations(cache, cfg, reset - 86400)], ["7d"])

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
