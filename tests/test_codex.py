import json, os, re, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import guardlib as g  # noqa: E402

HOOK = ROOT / "codex-hook.py"
UG = ROOT / "ug"
PRE = ("--event", "pre_tool_use")
BRIEF = ("--event", "user_prompt_submit")


def transcript(path: Path, seven_pct, five_pct=None, now=None):
    """A Codex rollout log whose last token_count carries the given usage."""
    now = now or time.time()
    limits = {"limit_id": "codex", "primary": {"used_percent": seven_pct, "window_minutes": 10080,
                                                "resets_at": int(now + 5 * 86400)}, "secondary": None}
    if five_pct is not None:
        limits["secondary"] = {"used_percent": five_pct, "window_minutes": 300, "resets_at": int(now + 3 * 3600)}
    lines = [json.dumps({"type": "session_meta", "payload": {"id": "s"}})]
    lines += [json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                             "content": [{"type": "output_text", "text": "x" * 300}]}})
              for _ in range(50)]
    lines.append(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {}, "rate_limits": limits}}))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


class CodexFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "ug"
        self.dir.mkdir()
        self.codex = Path(self.tmp.name) / "codex"
        self.env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "UG_DIR": str(self.dir),
                    "UG_CODEX_SESSIONS": str(self.codex / "sessions"), "UG_CODEX_HOME": str(self.codex),
                    "NO_COLOR": "1"}
        (self.dir / "config.json").write_text(json.dumps({
            "poll_seconds": 0.1, "pace_mode": "delay", "pace_seconds_per_pct": 0.1, "pace_max_delay_seconds": 1.5}))
        self.transcript = self.codex / "sessions" / "2026" / "10" / "06" / "rollout.jsonl"
        # In-process guardlib calls must see the same redirected paths as the
        # subprocesses, or they would read and WRITE the real ~/.codex.
        self.patched = mock.patch.dict(os.environ, {k: self.env[k] for k in ("UG_DIR", "UG_CODEX_SESSIONS", "UG_CODEX_HOME")})
        self.patched.start()

    def tearDown(self):
        self.patched.stop()
        self.tmp.cleanup()

    def payload(self, event, **extra):
        base = {"session_id": "s", "turn_id": "t", "transcript_path": str(self.transcript), "cwd": "/tmp",
                "hook_event_name": event, "model": "m", "permission_mode": "default"}
        base.update(extra)
        return json.dumps(base)

    def invoke(self, script, stdin, *args, timeout=30):
        start = time.time()
        proc = subprocess.run([sys.executable, str(script), *args], input=stdin, capture_output=True,
                              text=True, env=self.env, timeout=timeout)
        return proc, time.time() - start

    def context(self, proc):
        return json.loads(proc.stdout)["hookSpecificOutput"].get("additionalContext") if proc.stdout.strip() else None


class CodexHookTest(CodexFixture):
    def test_brief_hook_reads_the_live_transcript_and_caches_it(self):
        transcript(self.transcript, seven_pct=12.0)
        proc, _ = self.invoke(HOOK, self.payload("UserPromptSubmit", prompt="hi"), *BRIEF)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("codex 7d 12%", self.context(proc))
        cache = json.loads((self.dir / "codex-usage.json").read_text())
        self.assertEqual(cache["seven_day"]["used_percentage"], 12.0)

    def test_guard_hook_is_silent_and_fast_when_codex_usage_is_on_pace(self):
        transcript(self.transcript, seven_pct=12.0)
        proc, took = self.invoke(HOOK, self.payload("PreToolUse", tool_name="Bash", tool_input={}), *PRE)
        self.assertEqual(proc.stdout, "")
        self.assertLess(took, 1.0)

    def test_guard_hook_paces_codex_from_its_own_transcript(self):
        # 2h into the 5h window at 70%: pace line 38, 32 ahead, 12 over -> 1.2s delay
        transcript(self.transcript, seven_pct=12.0, five_pct=70.0)
        proc, took = self.invoke(HOOK, self.payload("PreToolUse", tool_name="Bash", tool_input={}), *PRE)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertGreaterEqual(took, 1.1)
        ctx = self.context(proc)
        self.assertIn("5h window at 70%", ctx)
        self.assertNotIn("deny", proc.stdout)

    def test_guard_hook_holds_codex_at_the_threshold_until_the_reset(self):
        (self.dir / "config.json").write_text(json.dumps({"poll_seconds": 0.1}))
        now = time.time()
        limits = {"limit_id": "codex", "primary": {"used_percent": 99.0, "window_minutes": 10080, "resets_at": int(now + 5 * 86400)},
                  "secondary": {"used_percent": 99.0, "window_minutes": 300, "resets_at": int(now + 2)}}
        lines = [json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {}, "rate_limits": limits}})]
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.write_text("\n".join(lines) + "\n")
        (self.dir / "config.json").write_text(json.dumps({"poll_seconds": 0.1, "threshold_7d": 100}))
        proc, took = self.invoke(HOOK, self.payload("PreToolUse", tool_name="Bash", tool_input={}), *PRE)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertGreaterEqual(took, 1.5)
        self.assertNotIn("deny", proc.stdout)
        self.assertFalse((self.dir / "codex-blocked.json").exists())

    def test_codex_hold_marker_is_separate_from_claudes(self):
        (self.dir / "config.json").write_text(json.dumps({"poll_seconds": 0.1}))
        transcript(self.transcript, seven_pct=99.0)
        proc = subprocess.Popen([sys.executable, str(HOOK), *PRE], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env)
        try:
            proc.communicate(input=self.payload("PreToolUse", tool_name="Bash", tool_input={}), timeout=0.8)
            self.fail("the hook should still be holding")
        except subprocess.TimeoutExpired:
            pass
        self.assertTrue((self.dir / "codex-blocked.json").exists())
        self.assertFalse((self.dir / "sessions").exists())  # Claude's registry is the mod's alone
        proc.kill()
        proc.communicate()

    def test_hook_without_an_event_fails_loudly(self):
        proc, _ = self.invoke(HOOK, "{}")
        self.assertEqual(proc.returncode, 2)
        proc, _ = self.invoke(HOOK, "{}", "--event", "stop")
        self.assertEqual(proc.returncode, 2)

    def test_brief_is_silent_without_any_usage_data(self):
        proc, _ = self.invoke(HOOK, "{}", *BRIEF)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))

    def test_hooks_survive_malformed_stdin(self):
        transcript(self.transcript, seven_pct=12.0)
        for event in (PRE, BRIEF):
            proc, _ = self.invoke(HOOK, "{bad", *event)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def start_hold(self, seven_pct=99.0, **cfg):
        (self.dir / "config.json").write_text(json.dumps({"poll_seconds": 0.1, **cfg}))
        transcript(self.transcript, seven_pct=seven_pct)
        proc = subprocess.Popen([sys.executable, str(HOOK), *PRE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=self.env)
        proc.stdin.write(self.payload("PreToolUse", tool_name="Bash", tool_input={}))
        proc.stdin.close()
        time.sleep(0.6)
        self.assertIsNone(proc.poll(), "the hook should still be holding")
        return proc

    def finish(self, proc):
        out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, err)
        return out

    def test_ug_release_ends_a_codex_hold(self):
        proc = self.start_hold()
        self.assertEqual(self.invoke(UG, "", "release")[0].returncode, 0)
        self.assertNotIn("deny", self.finish(proc))
        self.assertFalse((self.dir / "codex-blocked.json").exists())

    def test_ug_off_ends_a_codex_hold(self):
        proc = self.start_hold()
        self.invoke(UG, "", "off")
        self.assertNotIn("deny", self.finish(proc))

    def test_raising_the_threshold_above_usage_ends_a_codex_hold(self):
        proc = self.start_hold(seven_pct=92.0)
        self.invoke(UG, "", "threshold", "7d", "99")
        self.assertNotIn("deny", self.finish(proc))

    def test_ug_status_shows_the_codex_hold(self):
        proc = self.start_hold()
        proc_status, _ = self.invoke(UG, "", "status")
        self.assertIn("HOLD", proc_status.stdout)
        self.assertIn("codex: active on 7d", proc_status.stdout)
        proc.kill(); proc.communicate()

    def test_switching_pacing_off_mid_delay_releases_at_once(self):
        (self.dir / "config.json").write_text(json.dumps({"poll_seconds": 0.1, "pace_max_delay_seconds": 20, "pace_seconds_per_pct": 2}))
        transcript(self.transcript, seven_pct=12.0, five_pct=70.0)
        proc = subprocess.Popen([sys.executable, str(HOOK), *PRE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=self.env)
        proc.stdin.write(self.payload("PreToolUse", tool_name="Bash", tool_input={})); proc.stdin.close()
        time.sleep(0.5)
        self.assertIsNone(proc.poll())
        self.invoke(UG, "", "pace", "off")
        out = self.finish(proc)
        self.assertEqual(out, "")

    def test_hold_mode_releases_when_fresh_usage_is_back_on_pace(self):
        (self.dir / "config.json").write_text(json.dumps({"poll_seconds": 0.1, "pace_mode": "hold"}))
        transcript(self.transcript, seven_pct=12.0, five_pct=70.0)
        proc = subprocess.Popen([sys.executable, str(HOOK), *PRE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=self.env)
        proc.stdin.write(self.payload("PreToolUse", tool_name="Bash", tool_input={})); proc.stdin.close()
        time.sleep(0.6)
        self.assertIsNone(proc.poll(), "the hook should still be holding")
        marker = json.loads((self.dir / "codex-blocked.json").read_text())
        self.assertTrue(marker["label"].startswith("pace"))
        now = time.time()
        (self.dir / "codex-usage.json").write_text(json.dumps({
            "ts": now, "seven_day": {"used_percentage": 12.0, "resets_at": int(now + 5 * 86400)},
            "five_hour": {"used_percentage": 50.0, "resets_at": int(now + 3 * 3600)}}))
        out = self.finish(proc)
        self.assertIn("this call was held", json.loads(out)["hookSpecificOutput"]["additionalContext"])
        self.assertFalse((self.dir / "codex-blocked.json").exists())

    def test_hook_cache_beats_the_session_log_scan_while_fresh(self):
        transcript(self.transcript, seven_pct=12.0)
        now = time.time()
        (self.dir / "codex-usage.json").write_text(json.dumps({
            "ts": now, "seven_day": {"used_percentage": 55.0, "resets_at": int(now + 86400)}}))
        got = g.codex_limits(now, 86400)
        self.assertEqual(got["source"], "hook")
        self.assertEqual(got["seven_day"]["used_percentage"], 55.0)
        got = g.codex_limits(now + 2 * 86400, 86400)  # cache aged out -> log scan, whose window also reset
        self.assertIsNone(got)

    def test_a_reset_window_in_the_hook_cache_is_dropped(self):
        now = time.time()
        (self.dir / "codex-usage.json").write_text(json.dumps({
            "ts": now, "seven_day": {"used_percentage": 55.0, "resets_at": int(now - 5)}}))
        self.assertIsNone(g.codex_limits(now, 86400))


class FreshSessionTest(CodexFixture):
    def older_log(self, seven_pct, five_pct=None, age=120):
        path = self.codex / "sessions" / "2026" / "10" / "06" / "previous.jsonl"
        transcript(path, seven_pct=seven_pct, five_pct=five_pct)
        os.utime(path, (time.time() - age, time.time() - age))
        return path

    def fresh_transcript(self):
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": "fresh"}}) + "\n")

    def test_report_walks_back_to_a_log_that_carries_limits(self):
        self.older_log(seven_pct=33.0)
        self.fresh_transcript()  # newest, but no token_count yet
        got = g.codex_limits(time.time(), 86400)
        self.assertEqual(got["seven_day"]["used_percentage"], 33.0)
        self.assertEqual(got["source"], "log")

    def test_brief_on_the_first_prompt_still_shows_codex_from_the_previous_session(self):
        self.older_log(seven_pct=33.0)
        self.fresh_transcript()
        proc, _ = self.invoke(HOOK, self.payload("UserPromptSubmit", prompt="hi"), *BRIEF)
        self.assertIn("codex 7d 33%", self.context(proc))

    def test_guard_paces_a_fresh_session_from_the_previous_sessions_numbers(self):
        self.older_log(seven_pct=12.0, five_pct=70.0, age=30)
        self.fresh_transcript()
        proc, took = self.invoke(HOOK, self.payload("PreToolUse", tool_name="Bash", tool_input={}), *PRE)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertGreaterEqual(took, 1.1)
        self.assertIn("5h window at 70%", self.context(proc))

    def test_guard_fails_open_when_the_only_numbers_are_stale(self):
        self.older_log(seven_pct=99.0, age=3600)  # older than stale_after_seconds (600)
        self.fresh_transcript()
        proc, took = self.invoke(HOOK, self.payload("PreToolUse", tool_name="Bash", tool_input={}), *PRE)
        self.assertEqual(proc.stdout, "")
        self.assertLess(took, 1.0)


class CodexTrustTest(CodexFixture):
    def test_hash_matches_what_codex_computed_for_a_real_hook(self):
        # Golden values observed from codex-cli 0.159.0 after trusting these exact hooks.
        base = "/private/tmp/claude-501/-Users-viktorpoputnikov-workspace-fleet/0ef52947-d642-44d1-950c-df341a0c7c3b/scratchpad/hooktest"
        h = {"type": "command", "command": f"python3 {base}/probe.py", "timeout": 10}
        self.assertEqual(g.codex_hook_hash("UserPromptSubmit", None, h),
                         "sha256:a197954b3cf1cd88db188d36225f937397fe9c0b2faabce9e5b90203ccc3dca4")
        self.assertEqual(g.codex_hook_hash("PreToolUse", "*", h),
                         "sha256:d108538eeb1005a06cf5c8d088eb8be4a02167ab1561ffe44bea5180c4ca40e0")

    def test_install_writes_hooks_and_trust_and_is_idempotent(self):
        self.codex.mkdir()
        (self.codex / "config.toml").write_text('model = "x"\n\n[features]\nmulti_agent = true\n')
        result = g.codex_install(ROOT)
        self.assertEqual(result["hooks"], 2)
        self.assertEqual(len(result["trusted_now"]), 2)
        doc = json.loads((self.codex / "hooks.json").read_text())
        self.assertEqual(len(doc["hooks"]["PreToolUse"]), 1)
        text = (self.codex / "config.toml").read_text()
        self.assertTrue(text.startswith('model = "x"'))
        self.assertEqual(text.count("[hooks.state."), 2)
        self.assertTrue(g.codex_trusted(ROOT))
        again = g.codex_install(ROOT)
        self.assertEqual(again["trusted_now"], [])
        self.assertEqual((self.codex / "config.toml").read_text(), text)

    def test_install_keeps_foreign_hooks_and_trust_entries(self):
        self.codex.mkdir()
        (self.codex / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "^Bash$", "hooks": [{"type": "command", "command": "python3 ~/policy.py", "timeout": 5}]}]}}))
        (self.codex / "config.toml").write_text('[hooks.state."/x/hooks.json:stop:0:0"]\ntrusted_hash = "sha256:abc"\n')
        g.codex_install(ROOT)
        doc = json.loads((self.codex / "hooks.json").read_text())
        self.assertEqual(doc["hooks"]["PreToolUse"][0]["matcher"], "^Bash$")
        self.assertEqual(len(doc["hooks"]["PreToolUse"]), 2)
        self.assertIn('"/x/hooks.json:stop:0:0"', (self.codex / "config.toml").read_text())

    def test_install_replaces_a_stale_trust_entry_for_the_same_key(self):
        self.codex.mkdir()
        g.codex_install(ROOT)
        text = (self.codex / "config.toml").read_text()
        stale = re.sub(r'trusted_hash = "sha256:[0-9a-f]+"', 'trusted_hash = "sha256:stale"', text, count=1)
        (self.codex / "config.toml").write_text(stale)
        self.assertFalse(g.codex_trusted(ROOT))
        result = g.codex_install(ROOT)
        self.assertEqual(len(result["trusted_now"]), 1)
        self.assertTrue(g.codex_trusted(ROOT))
        self.assertEqual((self.codex / "config.toml").read_text().count("[hooks.state."), 2)

    def test_trusted_is_false_without_hooks_or_without_config(self):
        self.assertFalse(g.codex_trusted(ROOT))
        self.codex.mkdir()
        (self.codex / "hooks.json").write_text(json.dumps(g.codex_guard_hooks(ROOT) and {"hooks": g.codex_guard_hooks(ROOT)}))
        self.assertFalse(g.codex_trusted(ROOT))

    def test_ug_codex_commands(self):
        self.codex.mkdir()
        proc, _ = self.invoke(UG, "", "codex", "trusted")
        self.assertEqual(proc.returncode, 1)
        proc, _ = self.invoke(UG, "", "codex", "install")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("2 guard hooks", proc.stdout)
        proc, _ = self.invoke(UG, "", "codex", "trusted")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        proc, _ = self.invoke(UG, "", "codex", "frobnicate")
        self.assertEqual(proc.returncode, 1)


if __name__ == "__main__":
    unittest.main()
