import json, os, re, subprocess, sys, tempfile, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "guard-hook.py"
BRIEF = ROOT / "brief-hook.py"
UG = ROOT / "ug"
STATUSLINE = ROOT / "statusline.py"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
TOOL_CALL = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                        "tool_input": {"command": "ls"}, "session_id": "s1"})
H5 = 5 * 3600


class PaceFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.sessions = self.dir / "codex-sessions"
        self.env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "UG_DIR": str(self.dir), "UG_CODEX_SESSIONS": str(self.sessions),
                    "NO_COLOR": "1"}
        self.write_config({})

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, cfg):
        base = {"poll_seconds": 0.1, "max_stall_seconds": 30, "pace_seconds_per_pct": 0.1,
                "pace_max_delay_seconds": 1.5}
        base.update(cfg)
        (self.dir / "config.json").write_text(json.dumps(base))

    def write_cache(self, pct=70.0, resets_in=3 * 3600, window="five_hour"):
        """2h into a 5h window at 70%: pace line 38, 32 ahead, 12 over the margin."""
        now = time.time()
        (self.dir / "usage.json").write_text(json.dumps({
            "ts": now, window: {"used_percentage": pct, "resets_at": int(now + resets_in)}}))

    def invoke(self, script, stdin=TOOL_CALL, timeout=30, **kw):
        start = time.time()
        proc = subprocess.run([sys.executable, str(script), *kw.get("args", [])], input=stdin,
                              capture_output=True, text=True, env=self.env, timeout=timeout)
        return proc, time.time() - start

    def context(self, proc):
        if not proc.stdout.strip():
            return None
        return json.loads(proc.stdout)["hookSpecificOutput"].get("additionalContext")


class HookPacingTest(PaceFixture):
    def test_below_the_margin_the_hook_stays_silent_and_fast(self):
        self.write_cache(pct=50.0)
        proc, took = self.invoke(HOOK)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertLess(took, 1.0)

    def test_delay_mode_sleeps_in_proportion_then_allows_with_context(self):
        self.write_cache(pct=70.0)  # 12 over margin * 0.1 s = 1.2 s
        proc, took = self.invoke(HOOK)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertGreaterEqual(took, 1.1)
        self.assertNotIn("deny", proc.stdout)
        ctx = self.context(proc)
        self.assertIn("pacing", ctx)
        self.assertIn("5h window at 70%", ctx)
        self.assertIn("fewer, larger steps", ctx)
        self.assertIn("never a reason to stop", ctx)

    def test_delay_is_capped_by_the_configured_maximum(self):
        self.write_config({"pace_max_delay_seconds": 0.3})
        self.write_cache(pct=90.0)  # 32 over margin would be 3.2 s uncapped
        proc, took = self.invoke(HOOK)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(took, 1.5)
        self.assertIn("pacing", self.context(proc))

    def test_pacing_off_lets_the_call_through_silently(self):
        self.write_config({"pace_enabled": False})
        self.write_cache(pct=70.0)
        proc, took = self.invoke(HOOK)
        self.assertEqual(proc.stdout, "")
        self.assertLess(took, 1.0)

    def test_switching_pacing_off_mid_delay_releases_at_once(self):
        self.write_config({"pace_max_delay_seconds": 20, "pace_seconds_per_pct": 2})
        self.write_cache(pct=70.0)
        # The hook drains stdin and ignores the payload, so an empty stdin lets
        # the test interact mid-run without a pipe to close (Python 3.9 rejects
        # communicate() after a manual close).
        proc = subprocess.Popen([sys.executable, str(HOOK)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=self.env)
        time.sleep(0.5)
        self.write_config({"pace_enabled": False})
        out, err = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(out, "")

    def test_hold_mode_releases_when_fresh_usage_is_back_on_pace(self):
        self.write_config({"pace_mode": "hold"})
        self.write_cache(pct=70.0)
        # The hook drains stdin and ignores the payload, so an empty stdin lets
        # the test interact mid-run without a pipe to close (Python 3.9 rejects
        # communicate() after a manual close).
        proc = subprocess.Popen([sys.executable, str(HOOK)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=self.env)
        time.sleep(0.6)
        self.assertIsNone(proc.poll(), "the hook should still be holding")
        self.write_cache(pct=50.0)  # the statusline saw usage fall back within the margin
        out, err = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 0, err)
        self.assertNotIn("deny", out)
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("this call was held", ctx)

    def test_hold_mode_writes_a_pace_marker_while_holding(self):
        self.write_config({"pace_mode": "hold", "max_stall_seconds": 3})
        self.write_cache(pct=70.0)
        # The hook drains stdin and ignores the payload, so an empty stdin lets
        # the test interact mid-run without a pipe to close (Python 3.9 rejects
        # communicate() after a manual close).
        proc = subprocess.Popen([sys.executable, str(HOOK)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=self.env)
        time.sleep(0.6)
        marker = json.loads((self.dir / "blocked.json").read_text())
        self.assertTrue(marker["label"].startswith("pace"))
        out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, err)
        self.assertNotIn("deny", out)  # the stall budget ends a pace hold with an allow, never a deny
        self.assertFalse((self.dir / "blocked.json").exists())

    def test_threshold_hold_still_wins_over_pacing(self):
        self.write_cache(pct=99.0, resets_in=2)
        proc, took = self.invoke(HOOK)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertGreaterEqual(took, 1.0)
        self.assertEqual(proc.stdout, "")


class BriefHookTest(PaceFixture):
    def test_silent_without_any_usage_data(self):
        proc, _ = self.invoke(BRIEF, stdin=json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "hi"}))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_emits_one_line_of_context_when_data_exists(self):
        self.write_cache(pct=50.0)
        proc, _ = self.invoke(BRIEF, stdin="{}")
        out = json.loads(proc.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "UserPromptSubmit")
        self.assertTrue(out["additionalContext"].startswith("[usage] claude 5h 50%"))
        self.assertNotIn("\n", out["additionalContext"])

    def test_survives_malformed_stdin(self):
        self.write_cache(pct=50.0)
        proc, _ = self.invoke(BRIEF, stdin="not json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("[usage]", proc.stdout)


class UgPaceTest(PaceFixture):
    def ug(self, *args):
        proc, _ = self.invoke(UG, stdin="", args=list(args))
        return proc

    def config(self):
        return json.loads((self.dir / "config.json").read_text())

    def test_pace_with_no_arguments_reports_settings(self):
        proc = self.ug("pace")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("pacing     on  mode delay", proc.stdout)
        self.assertIn("margin 5h  20%", proc.stdout)

    def test_pace_off_and_on(self):
        self.ug("pace", "off")
        self.assertFalse(self.config()["pace_enabled"])
        self.ug("pace", "on")
        self.assertTrue(self.config()["pace_enabled"])

    def test_pace_mode_accepts_only_delay_or_hold(self):
        self.assertEqual(self.ug("pace", "mode", "hold").returncode, 0)
        self.assertEqual(self.config()["pace_mode"], "hold")
        bad = self.ug("pace", "mode", "sideways")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("delay|hold", bad.stderr)

    def test_pace_margin_sets_one_window_and_validates(self):
        self.assertEqual(self.ug("pace", "margin", "7d", "8").returncode, 0)
        self.assertEqual(self.config()["pace_margin_7d"], 8.0)
        self.assertNotIn("pace_margin_5h", self.config())
        self.assertEqual(self.ug("pace", "margin", "7d", "150").returncode, 1)
        self.assertEqual(self.ug("pace", "margin", "1d", "5").returncode, 1)

    def test_pace_set_tunes_the_numeric_knobs(self):
        self.assertEqual(self.ug("pace", "set", "pace_max_delay_seconds", "45").returncode, 0)
        self.assertEqual(self.config()["pace_max_delay_seconds"], 45.0)
        self.assertEqual(self.ug("pace", "set", "threshold_5h", "1").returncode, 1)
        self.assertEqual(self.ug("pace", "set", "pace_max_delay_seconds", "-1").returncode, 1)

    def test_status_json_is_machine_readable_and_carries_pace_fields(self):
        self.write_cache(pct=70.0)
        proc = self.ug("status", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rep = json.loads(proc.stdout)
        [w] = rep["claude"]["windows"]
        self.assertEqual(w["window"], "5h")
        self.assertTrue(w["pacing"])
        self.assertAlmostEqual(w["pace_line_pct"], 38.0, delta=0.2)
        self.assertGreater(w["catchup_in_seconds"], 0)
        self.assertEqual(rep["codex"]["windows"], [])
        self.assertIn("PACING", rep["brief"])

    def test_status_text_shows_pace_line_and_pacing_state(self):
        self.write_cache(pct=70.0)
        proc = self.ug("status")
        self.assertIn("pace line 38%", proc.stdout)
        self.assertIn("PACING", proc.stdout)
        self.assertIn("codex", proc.stdout)

    def test_status_rejects_unknown_flags(self):
        self.assertEqual(self.ug("status", "--yaml").returncode, 1)

    def test_brief_prints_the_agent_line_or_a_placeholder(self):
        self.assertIn("no usage data yet", self.ug("brief").stdout)
        self.write_cache(pct=50.0)
        self.assertIn("[usage] claude 5h 50%", self.ug("brief").stdout)


class StatuslinePaceTest(PaceFixture):
    def payload(self, pct, resets_in):
        now = int(time.time())
        return {"cwd": "/tmp", "workspace": {"current_dir": "/tmp"},
                "rate_limits": {"five_hour": {"used_percentage": pct, "resets_at": now + resets_in}}}

    def render(self, data):
        proc = subprocess.run([sys.executable, str(STATUSLINE)], input=json.dumps(data), capture_output=True,
                              text=True, env=self.env, cwd="/tmp", timeout=15)
        return ANSI.sub("", proc.stdout).strip()

    def test_marks_how_far_ahead_of_the_line_a_window_runs(self):
        self.assertIn("5h 50% +12", self.render(self.payload(50.0, 3 * 3600)))

    def test_flags_an_actively_paced_window(self):
        self.assertIn("5h 70% +32▲", self.render(self.payload(70.0, 3 * 3600)))

    def test_no_mark_when_the_gap_rounds_to_zero(self):
        # 2h into 5h the line is 38; 38.3% used is 0.3 ahead, which would print as +0
        out = self.render(self.payload(38.3, 3 * 3600))
        self.assertIn("5h 38%", out)
        self.assertNotIn("+0", out)

    def test_no_mark_when_behind_the_line(self):
        out = self.render(self.payload(10.0, 3600))
        self.assertIn("5h 10%", out)
        self.assertNotIn("+", out.split("5h 10%")[1].split("·")[0])

    def test_shows_pace_off_when_pacing_is_disabled(self):
        self.write_config({"pace_enabled": False})
        self.assertIn("pace off", self.render(self.payload(10.0, 3600)))

    def test_shows_codex_usage_from_its_session_log(self):
        day = self.sessions / "2026" / "10" / "06"
        day.mkdir(parents=True)
        (day / "r.jsonl").write_text(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "rate_limits": {"primary": {
                "used_percent": 21.0, "window_minutes": 10080, "resets_at": int(time.time() + 86400)}}}}) + "\n")
        self.assertIn("cx 7d 21%", self.render(self.payload(10.0, 3600)))


if __name__ == "__main__":
    unittest.main()
