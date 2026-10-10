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
        self.assertIn("hold at 95", proc.stdout)

    def test_status_shows_current_usage_from_the_cache(self):
        self.write_cache(pct=42.0)
        self.assertIn("42", self.ug("status").stdout)

    def test_status_works_with_no_cache_present(self):
        proc = self.ug("status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no usage data", proc.stdout.lower())

    def write_session(self, sid, priority="normal", hold=None, last_call=None, cwd="/w"):
        now = time.time()
        (self.dir / "sessions").mkdir(exist_ok=True)
        (self.dir / "sessions" / f"{sid}.json").write_text(json.dumps({
            "id": sid, "priority": priority, "cwd": cwd, "started": now - 100, "last_call": last_call or now,
            "updated": now, "hold": hold, "pacing_seconds": 0, "lift": 0}))

    def test_status_reports_an_active_hold_of_a_session(self):
        self.write_cache(pct=99.0)
        self.write_session("s1", hold={"label": "5h", "until": int(time.time() + 600), "kind": "threshold"})
        self.assertIn("HOLD", self.ug("status").stdout)

    def test_status_ignores_an_expired_hold(self):
        self.write_cache(pct=99.0)
        self.write_session("s1", hold={"label": "5h", "until": int(time.time() - 5), "kind": "threshold"})
        self.assertNotIn("HOLD", self.ug("status").stdout)

    # --- sessions and priorities --------------------------------------------

    def test_sessions_lists_live_sessions_with_priority_and_borrowing(self):
        self.write_cache(pct=70.0, resets_in=3 * 3600)
        self.write_session("aaaaaaaa-1", "high")
        self.write_session("bbbbbbbb-2", "low", cwd=str(Path.home() / "x"))
        out = self.ug("sessions").stdout
        self.assertIn("2 live", out)
        self.assertIn("aaaaaaaa △ high", out)
        self.assertIn("bbbbbbbb ▽ low ⇡◇", out)
        self.assertIn("◔60s", out)
        self.assertIn("~/x", out)

    def test_priority_sets_the_only_live_session(self):
        self.write_session("abc-1")
        proc = self.ug("priority", "high")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads((self.dir / "sessions" / "abc-1.json").read_text())["priority"], "high")

    def test_priority_needs_a_prefix_when_several_sessions_are_live(self):
        self.write_session("abc-1")
        self.write_session("xyz-2")
        self.assertEqual(self.ug("priority", "low").returncode, 1)
        self.assertEqual(self.ug("priority", "low", "xyz").returncode, 0)
        self.assertEqual(json.loads((self.dir / "sessions" / "xyz-2.json").read_text())["priority"], "low")
        self.assertEqual(json.loads((self.dir / "sessions" / "abc-1.json").read_text())["priority"], "normal")
        self.assertEqual(self.ug("priority", "low", "nope").returncode, 1)

    def test_priority_rejects_an_unknown_level(self):
        self.write_session("abc-1")
        self.assertEqual(self.ug("priority", "urgent").returncode, 1)

    def test_priority_with_no_arguments_lists_sessions(self):
        self.assertIn("none live", self.ug("priority").stdout)

    def test_pace_set_accepts_the_priority_and_borrow_knobs(self):
        self.assertEqual(self.ug("pace", "set", "priority_delay_factor_low", "3").returncode, 0)
        self.assertEqual(self.ug("pace", "set", "borrow_after_seconds", "60").returncode, 0)
        self.assertEqual(self.config()["borrow_after_seconds"], 60.0)
        self.assertEqual(self.ug("pace", "set", "threshold_5h", "1").returncode, 1)

    # --- weekly profile ---------------------------------------------------------

    def test_profile_presets_days_and_hours(self):
        self.assertIn("preset uniform", self.ug("pace", "profile").stdout)
        self.assertEqual(self.ug("pace", "profile", "workweek").returncode, 0)
        self.assertEqual(self.config()["pace_profile_days"], [1.0] * 5 + [0.3, 0.3])
        self.assertIn("preset workweek", self.ug("pace", "profile").stdout)
        self.assertEqual(self.ug("pace", "profile", "weekend").returncode, 0)
        self.assertEqual(self.config()["pace_profile_days"][5:], [1.0, 1.0])
        self.assertEqual(self.ug("pace", "profile", "hours", "9-18,20-23").returncode, 0)
        hours = self.config()["pace_profile_hours"]
        self.assertEqual((hours[8], hours[9], hours[17], hours[18], hours[20], hours[23]), (0.1, 1.0, 1.0, 0.1, 1.0, 0.1))
        self.assertEqual(self.ug("pace", "profile", "days", "1,1,1,1,1,0,0").returncode, 0)
        self.assertIn("custom", self.ug("pace", "profile").stdout)
        self.assertEqual(self.ug("pace", "profile", "days", "1,2").returncode, 1)
        self.assertEqual(self.ug("pace", "profile", "hours", "25-30").returncode, 1)
        self.assertEqual(self.ug("pace", "profile", "sideways").returncode, 1)

    # --- install ------------------------------------------------------------

    def test_install_registers_the_plugin_and_strips_the_legacy_settings(self):
        bin_dir = self.dir / "bin"; bin_dir.mkdir()
        log = self.dir / "claude.log"
        fake = bin_dir / "claude"
        fake.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n"
                        f"if [ \"$1 $2 $3\" = \"plugin marketplace list\" ]; then printf '  > usage-guard\\n    Source: Folder (/elsewhere/usage-guard)\\n'; fi\n")
        fake.chmod(0o755)
        settings = self.dir / "settings.json"
        settings.write_text(json.dumps({
            "statusLine": {"type": "command", "command": "python3 ~/.claude/usage-guard/statusline.py"},
            "hooks": {"PreToolUse": [
                {"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/usage-guard/guard-hook.py"}]},
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 ~/mine.py"}]}],
                "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 ~/.claude/usage-guard/brief-hook.py"}]}]},
            "env": {"KEEP": "1"}}))
        env = {**self.env, "PATH": f"{bin_dir}:{self.env['PATH']}", "UG_CLAUDE_SETTINGS": str(settings),
               "HOME": str(self.dir)}
        proc = subprocess.run([sys.executable, str(UG), "install"], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = [c for c in log.read_text().splitlines() if c != "plugin marketplace list"]
        self.assertEqual(calls[0], "plugin marketplace remove usage-guard")  # it was registered from another checkout
        self.assertEqual(calls[1], f"plugin marketplace add {UG.parent}")
        self.assertEqual(calls[2], "plugin install usage-guard@usage-guard")
        doc = json.loads(settings.read_text())
        self.assertNotIn("statusLine", doc)
        self.assertNotIn("UserPromptSubmit", doc["hooks"])
        self.assertEqual(doc["hooks"]["PreToolUse"][0]["matcher"], "Bash")
        self.assertEqual(doc["env"], {"KEEP": "1"})
        self.assertTrue((self.dir / ".local" / "bin" / "ug").is_symlink())
        self.assertIn("removed legacy hooks.PreToolUse, hooks.UserPromptSubmit, statusLine", proc.stdout)

    def test_install_fails_without_claude_on_path(self):
        env = {**self.env, "PATH": str(self.dir)}
        proc = subprocess.run([sys.executable, str(UG), "install"], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("claude", proc.stderr)

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
