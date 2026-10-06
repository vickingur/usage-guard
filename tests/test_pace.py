import json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import guardlib as g  # noqa: E402

H5, D7 = g.WINDOW_SECONDS["five_hour"], g.WINDOW_SECONDS["seven_day"]


def cache(now, five=None, seven=None):
    out = {"ts": now}
    if five:
        out["five_hour"] = {"used_percentage": five[0], "resets_at": int(now + five[1])}
    if seven:
        out["seven_day"] = {"used_percentage": seven[0], "resets_at": int(now + seven[1])}
    return out


class PaceMathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["UG_DIR"] = self.tmp.name
        self.cfg = g.load_config()
        self.now = 1_800_000_000.0

    def tearDown(self):
        os.environ.pop("UG_DIR", None)
        self.tmp.cleanup()

    def test_pace_line_is_zero_at_window_start_and_threshold_at_reset(self):
        self.assertAlmostEqual(g.pace_line(95, self.now + H5, H5, self.now), 0.0)
        self.assertAlmostEqual(g.pace_line(95, self.now, H5, self.now), 95.0)
        self.assertAlmostEqual(g.pace_line(90, self.now + D7 / 2, D7, self.now), 45.0)

    def test_pace_line_clamps_a_reset_further_away_than_the_window(self):
        self.assertAlmostEqual(g.pace_line(95, self.now + 2 * H5, H5, self.now), 0.0)

    def test_usage_within_margin_of_the_line_is_not_pacing(self):
        # 2h into 5h: line = 95 * 0.4 = 38; 50% used is 12 ahead, under the 20 margin
        [p] = g.paces(cache(self.now, five=(50, 3 * 3600)), self.cfg, self.now)
        self.assertAlmostEqual(p.line, 38.0, places=1)
        self.assertAlmostEqual(p.ahead, 12.0, places=1)
        self.assertFalse(p.active)
        self.assertEqual(p.delay_seconds, 0.0)

    def test_usage_past_the_margin_engages_pacing_with_a_proportional_delay(self):
        # 2h into 5h: line 38; 70% used is 32 ahead, 12 over the margin -> 60s capped at 30
        [p] = g.paces(cache(self.now, five=(70, 3 * 3600)), self.cfg, self.now)
        self.assertTrue(p.active)
        self.assertEqual(p.delay_seconds, 30.0)
        self.cfg["pace_max_delay_seconds"] = 1000
        [p] = g.paces(cache(self.now, five=(70, 3 * 3600)), self.cfg, self.now)
        self.assertAlmostEqual(p.delay_seconds, 12 * 5, places=0)

    def test_catchup_is_when_the_line_reaches_usage_minus_margin(self):
        [p] = g.paces(cache(self.now, five=(70, 3 * 3600)), self.cfg, self.now)
        expected_remaining = H5 * (1 - (70 - 20) / 95)
        self.assertAlmostEqual(p.catchup_at, p.resets_at - expected_remaining, delta=1)
        self.assertAlmostEqual(g.pace_line(95, p.resets_at, H5, p.catchup_at), 50.0, delta=0.1)

    def test_catchup_is_the_reset_when_the_line_can_never_reach_usage_minus_margin(self):
        self.cfg["pace_margin_5h"] = 0
        [p] = g.paces(cache(self.now, five=(96, 1800)), self.cfg, self.now)
        self.assertTrue(p.active)
        self.assertEqual(p.catchup_at, p.resets_at)

    def test_low_usage_never_paces_even_right_after_a_reset(self):
        # 1 minute into a 5h window at 25%: far ahead of a near-zero line, but under pace_min_used_pct
        [p] = g.paces(cache(self.now, five=(25, H5 - 60)), self.cfg, self.now)
        self.assertGreater(p.ahead, 20)
        self.assertFalse(p.active)

    def test_weekly_window_uses_its_own_margin(self):
        # 2 days into 7d: line 90 * 2/7 = 25.7; 44% is 18.3 ahead, over the 15 margin
        [p] = g.paces(cache(self.now, seven=(44, 5 * 86400)), self.cfg, self.now)
        self.assertTrue(p.active)
        self.cfg["pace_margin_7d"] = 25
        [p] = g.paces(cache(self.now, seven=(44, 5 * 86400)), self.cfg, self.now)
        self.assertFalse(p.active)

    def test_pacing_disabled_in_config_deactivates_every_window(self):
        self.cfg["pace_enabled"] = False
        ps = g.paces(cache(self.now, five=(70, 3 * 3600), seven=(60, 86400)), self.cfg, self.now)
        self.assertEqual(len(ps), 2)
        self.assertFalse(any(p.active for p in ps))

    def test_an_already_reset_window_does_not_pace(self):
        [p] = g.paces(cache(self.now, five=(99, -10)), self.cfg, self.now)
        self.assertFalse(p.active)

    def test_pace_delay_takes_the_worst_window(self):
        self.cfg["pace_max_delay_seconds"] = 1000
        ps = g.paces(cache(self.now, five=(70, 3 * 3600), seven=(40, 5 * 86400)), self.cfg, self.now)
        self.assertAlmostEqual(g.pace_delay(ps), max(p.delay_seconds for p in ps))
        self.assertEqual(g.pace_delay([]), 0.0)

    def test_unknown_pace_mode_in_config_falls_back_to_delay(self):
        (Path(self.tmp.name) / "config.json").write_text(json.dumps({"pace_mode": "sideways"}))
        self.assertEqual(g.load_config()["pace_mode"], "delay")


class CodexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sessions = Path(self.tmp.name) / "sessions"
        os.environ["UG_CODEX_SESSIONS"] = str(self.sessions)
        os.environ["UG_DIR"] = str(Path(self.tmp.name) / "ug")
        self.now = time.time()

    def tearDown(self):
        os.environ.pop("UG_CODEX_SESSIONS", None)
        os.environ.pop("UG_DIR", None)
        self.tmp.cleanup()

    def write_log(self, name, limits, extra_lines=200, mtime=None):
        day = self.sessions / "2026" / "10" / "06"
        day.mkdir(parents=True, exist_ok=True)
        path = day / name
        lines = [json.dumps({"timestamp": "t", "type": "session_meta", "payload": {"id": name}})]
        lines += [json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                                 "content": [{"type": "output_text", "text": "x" * 500}]}})
                  for _ in range(extra_lines)]
        lines.append(json.dumps({"timestamp": "t", "type": "event_msg",
                                 "payload": {"type": "token_count", "info": {}, "rate_limits": limits}}))
        lines.append(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                                                                     "content": [{"type": "input_text", "text": "later"}]}}))
        path.write_text("\n".join(lines) + "\n")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def limits(self, primary_pct=2.0, secondary=None):
        out = {"limit_id": "codex", "primary": {"used_percent": primary_pct, "window_minutes": 10080,
                                                  "resets_at": int(self.now + 3 * 86400)}, "secondary": secondary}
        return out

    def test_reads_the_last_rate_limits_of_the_newest_log(self):
        self.write_log("old.jsonl", self.limits(50.0), mtime=self.now - 7200)
        self.write_log("new.jsonl", self.limits(7.0), mtime=self.now - 60)
        got = g.codex_limits(self.now, 86400)
        self.assertEqual(got["seven_day"]["used_percentage"], 7.0)
        self.assertNotIn("five_hour", got)

    def test_maps_the_secondary_five_hour_window_too(self):
        sec = {"used_percent": 33.0, "window_minutes": 300, "resets_at": int(self.now + 1800)}
        self.write_log("s.jsonl", self.limits(4.0, sec), mtime=self.now - 10)
        got = g.codex_limits(self.now, 86400)
        self.assertEqual(got["five_hour"], {"used_percentage": 33.0, "resets_at": int(self.now + 1800)})

    def test_ignores_a_log_older_than_the_configured_age(self):
        self.write_log("stale.jsonl", self.limits(9.0), mtime=self.now - 2 * 86400)
        self.assertIsNone(g.codex_limits(self.now, 86400))

    def test_drops_a_window_that_has_reset_since_the_log_was_written(self):
        limits = self.limits(40.0)
        limits["primary"]["resets_at"] = int(self.now - 60)
        self.write_log("reset.jsonl", limits, mtime=self.now - 10)
        self.assertIsNone(g.codex_limits(self.now, 86400))

    def test_returns_none_without_any_log_or_limits(self):
        self.assertIsNone(g.codex_limits(self.now, 86400))
        day = self.sessions / "2026" / "10" / "06"
        day.mkdir(parents=True)
        (day / "empty.jsonl").write_text(json.dumps({"type": "session_meta", "payload": {}}) + "\n")
        self.assertIsNone(g.codex_limits(self.now, 86400))

    def test_report_carries_codex_windows_and_a_brief_line(self):
        self.write_log("r.jsonl", self.limits(12.0), mtime=self.now - 5)
        rep = g.report(g.load_config(), self.now)
        self.assertEqual(rep["codex"]["windows"][0]["used_pct"], 12.0)
        self.assertIn("codex 7d 12%", rep["brief"])
        self.assertEqual(rep["claude"]["windows"], [])

    def test_brief_is_empty_when_nothing_is_known(self):
        self.assertEqual(g.report(g.load_config(), self.now)["brief"], "")

    def test_brief_flags_pacing_and_advises_fewer_steps(self):
        ug = Path(os.environ["UG_DIR"]); ug.mkdir()
        (ug / "usage.json").write_text(json.dumps(cache(self.now, five=(70, 3 * 3600))))
        rep = g.report(g.load_config(), self.now)
        self.assertTrue(rep["claude"]["windows"][0]["pacing"])
        self.assertIn("PACING", rep["brief"])
        self.assertIn("fewer, larger steps", rep["brief"])
        self.assertIn("never a reason to stop", rep["brief"])


if __name__ == "__main__":
    unittest.main()
