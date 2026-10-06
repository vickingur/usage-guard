import json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "guard-hook.py"
TOOL_CALL = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                        "tool_input": {"command": "ls"}, "session_id": "s1"})


class HookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "UG_DIR": str(self.dir)}
        self.write_config({"poll_seconds": 0.1, "max_stall_seconds": 30})

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, cfg):
        base = {"enabled": True, "threshold_5h": 95.0, "threshold_7d": 90.0,
                "poll_seconds": 0.1, "max_stall_seconds": 30, "stale_after_seconds": 600}
        base.update(cfg)
        (self.dir / "config.json").write_text(json.dumps(base))

    def write_cache(self, pct=99.0, resets_in=3600, ts_age=0, window="five_hour"):
        """Write a usage cache and return the reset instant the hook must wait for."""
        now = time.time()
        resets_at = int(now + resets_in)
        (self.dir / "usage.json").write_text(json.dumps({
            "ts": now - ts_age,
            window: {"used_percentage": pct, "resets_at": resets_at},
        }))
        return resets_at

    def run_hook(self, timeout=30):
        start = time.time()
        proc = subprocess.run([sys.executable, str(HOOK)], input=TOOL_CALL,
                              capture_output=True, text=True, env=self.env, timeout=timeout)
        return proc, time.time() - start

    def start_hook(self):
        return subprocess.Popen([sys.executable, str(HOOK)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=self.env)

    def assertAllowed(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("deny", proc.stdout)

    # --- pass-through cases -------------------------------------------------

    def test_allows_immediately_when_guard_is_disabled(self):
        self.write_config({"enabled": False})
        self.write_cache(pct=99.0)
        proc, elapsed = self.run_hook()
        self.assertAllowed(proc)
        self.assertLess(elapsed, 3)

    def test_allows_immediately_when_usage_is_below_threshold(self):
        self.write_cache(pct=40.0)
        proc, elapsed = self.run_hook()
        self.assertAllowed(proc)
        self.assertLess(elapsed, 3)

    def test_allows_when_no_cache_exists_at_all(self):
        proc, elapsed = self.run_hook()
        self.assertAllowed(proc)
        self.assertLess(elapsed, 3)

    def test_allows_when_cache_is_stale_beyond_the_configured_age(self):
        self.write_cache(pct=99.0, ts_age=1200)
        proc, elapsed = self.run_hook()
        self.assertAllowed(proc)
        self.assertLess(elapsed, 3)

    def test_allows_when_the_window_already_reset_despite_high_usage(self):
        self.write_cache(pct=99.0, resets_in=-10)
        proc, elapsed = self.run_hook()
        self.assertAllowed(proc)
        self.assertLess(elapsed, 3)

    def test_survives_malformed_stdin(self):
        proc = subprocess.run([sys.executable, str(HOOK)], input="{bad",
                              capture_output=True, text=True, env=self.env, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    # --- holding ------------------------------------------------------------

    def test_holds_until_the_window_resets_then_allows(self):
        resets_at = self.write_cache(pct=99.0, resets_in=2)
        proc, _ = self.run_hook()
        self.assertAllowed(proc)
        self.assertGreaterEqual(time.time(), resets_at)

    def test_writes_a_hold_marker_while_holding_and_clears_it_after(self):
        self.write_cache(pct=99.0, resets_in=3)
        proc = self.start_hook()
        proc.stdin.write(TOOL_CALL)
        proc.stdin.close()
        time.sleep(1.0)
        marker = json.loads((self.dir / "blocked.json").read_text())
        self.assertEqual(marker["label"], "5h")
        self.assertGreater(marker["until"], time.time())
        proc.wait(timeout=15)
        self.assertFalse((self.dir / "blocked.json").exists())

    def test_holds_on_the_weekly_window_too(self):
        resets_at = self.write_cache(pct=99.0, resets_in=2, window="seven_day")
        proc, _ = self.run_hook()
        self.assertAllowed(proc)
        self.assertGreaterEqual(time.time(), resets_at)

    def test_holds_until_the_later_reset_when_both_windows_are_over(self):
        now = time.time()
        early, late = int(now + 1), int(now + 3)
        (self.dir / "usage.json").write_text(json.dumps({
            "ts": now,
            "five_hour": {"used_percentage": 99.0, "resets_at": early},
            "seven_day": {"used_percentage": 99.0, "resets_at": late},
        }))
        proc, _ = self.run_hook()
        self.assertAllowed(proc)
        # Must outlast the earlier window and wait for the later one.
        self.assertGreaterEqual(time.time(), late)

    # --- escape hatches -----------------------------------------------------

    def test_releases_mid_hold_when_the_guard_is_switched_off(self):
        self.write_cache(pct=99.0, resets_in=600)
        proc = self.start_hook()
        proc.stdin.write(TOOL_CALL)
        proc.stdin.close()
        time.sleep(0.6)
        self.write_config({"enabled": False})
        proc.wait(timeout=10)
        self.assertEqual(proc.returncode, 0)

    def test_releases_mid_hold_when_a_release_is_requested(self):
        self.write_cache(pct=99.0, resets_in=600)
        proc = self.start_hook()
        proc.stdin.write(TOOL_CALL)
        proc.stdin.close()
        time.sleep(0.6)
        (self.dir / "state.json").write_text(json.dumps({"release_at": time.time()}))
        proc.wait(timeout=10)
        self.assertEqual(proc.returncode, 0)

    def test_releases_mid_hold_when_the_threshold_is_raised_above_usage(self):
        self.write_cache(pct=96.0, resets_in=600)
        proc = self.start_hook()
        proc.stdin.write(TOOL_CALL)
        proc.stdin.close()
        time.sleep(0.6)
        self.write_config({"threshold_5h": 99.0})
        proc.wait(timeout=10)
        self.assertEqual(proc.returncode, 0)

    def test_a_release_predating_the_hold_does_not_release_it(self):
        (self.dir / "state.json").write_text(json.dumps({"release_at": time.time() - 60}))
        resets_at = self.write_cache(pct=99.0, resets_in=2)
        proc, _ = self.run_hook()
        self.assertAllowed(proc)
        self.assertGreaterEqual(time.time(), resets_at)

    # --- stall budget -------------------------------------------------------

    def test_denies_once_the_stall_budget_is_exhausted(self):
        self.write_config({"max_stall_seconds": 1})
        self.write_cache(pct=99.0, resets_in=86400 * 3)
        proc, elapsed = self.run_hook()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        specific = out["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PreToolUse")
        self.assertEqual(specific["permissionDecision"], "deny")
        self.assertIn("5h", specific["permissionDecisionReason"])

    def test_clears_the_hold_marker_after_denying(self):
        self.write_config({"max_stall_seconds": 1})
        self.write_cache(pct=99.0, resets_in=86400 * 3)
        self.run_hook()
        self.assertFalse((self.dir / "blocked.json").exists())


if __name__ == "__main__":
    unittest.main()
