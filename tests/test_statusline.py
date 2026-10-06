import json, os, re, subprocess, sys, tempfile, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def payload(**over):
    now = int(time.time())
    base = {
        "cwd": "/tmp",
        "workspace": {"current_dir": "/tmp", "project_dir": "/tmp"},
        "model": {"id": "claude-opus-5", "display_name": "Opus 5"},
        "context_window": {"used_percentage": 34.2, "context_window_size": 200000},
        "rate_limits": {
            "five_hour": {"used_percentage": 12.5, "resets_at": now + 3600 * 4 + 720 + 30},
            "seven_day": {"used_percentage": 63.0, "resets_at": now + 86400 * 5 + 3600 * 3 + 1800},
        },
    }
    base.update(over)
    return base


class StatuslineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "UG_DIR": self.tmp.name}

    def tearDown(self):
        self.tmp.cleanup()

    def run_statusline(self, data, cwd=None):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "statusline.py")],
            input=json.dumps(data), capture_output=True, text=True,
            env=self.env, cwd=cwd or "/tmp", timeout=15,
        )
        return proc, ANSI.sub("", proc.stdout).strip()

    def cache(self):
        p = Path(self.tmp.name) / "usage.json"
        return json.loads(p.read_text()) if p.exists() else None

    def test_renders_context_and_both_rate_limit_windows(self):
        proc, out = self.run_statusline(payload())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ctx 34%", out)
        self.assertIn("5h 12%", out)
        self.assertIn("7d 63%", out)

    def test_shows_the_current_model(self):
        _, out = self.run_statusline(payload())
        self.assertIn("Opus 5", out)

    def test_falls_back_to_the_model_id_when_there_is_no_display_name(self):
        _, out = self.run_statusline(payload(model={"id": "claude-haiku-4-5-20251001"}))
        self.assertIn("claude-haiku-4-5-20251001", out)

    def test_omits_the_model_segment_when_the_payload_has_no_model(self):
        data = payload()
        del data["model"]
        proc, out = self.run_statusline(data)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ctx 34%", out)

    def test_shows_time_until_each_window_resets(self):
        _, out = self.run_statusline(payload())
        self.assertIn("4h12m", out)
        self.assertIn("5d3h", out)

    def test_abbreviates_home_directory_as_tilde(self):
        home = os.path.expanduser("~")
        _, out = self.run_statusline(payload(workspace={"current_dir": home}))
        self.assertIn("~", out)
        self.assertNotIn(home, out)

    def test_writes_rate_limits_to_the_cache_for_the_hook(self):
        self.run_statusline(payload())
        cache = self.cache()
        self.assertEqual(cache["five_hour"]["used_percentage"], 12.5)
        self.assertEqual(cache["seven_day"]["used_percentage"], 63.0)
        self.assertAlmostEqual(cache["ts"], time.time(), delta=30)

    def test_preserves_existing_cache_when_payload_has_no_rate_limits(self):
        self.run_statusline(payload())
        before = self.cache()
        data = payload()
        del data["rate_limits"]
        _, out = self.run_statusline(data)
        self.assertEqual(self.cache(), before)

    def test_keeps_a_window_the_payload_transiently_omits(self):
        """rate_limits varies between renders; losing a window would blind the guard."""
        self.run_statusline(payload())
        data = payload()
        del data["rate_limits"]["five_hour"]
        self.run_statusline(data)
        cache = self.cache()
        self.assertEqual(cache["five_hour"]["used_percentage"], 12.5)
        self.assertEqual(cache["seven_day"]["used_percentage"], 63.0)

    def test_refreshes_a_window_the_payload_still_reports(self):
        self.run_statusline(payload())
        data = payload()
        data["rate_limits"]["five_hour"]["used_percentage"] = 88.0
        self.run_statusline(data)
        self.assertEqual(self.cache()["five_hour"]["used_percentage"], 88.0)

    def test_drops_a_carried_window_once_its_reset_time_has_passed(self):
        now = int(time.time())
        data = payload()
        data["rate_limits"]["five_hour"]["resets_at"] = now + 1
        self.run_statusline(data)
        self.assertIn("five_hour", self.cache())
        time.sleep(1.5)
        later = payload()
        del later["rate_limits"]["five_hour"]
        self.run_statusline(later)
        self.assertNotIn("five_hour", self.cache())

    def test_omits_rate_limit_fields_when_payload_has_none(self):
        data = payload()
        del data["rate_limits"]
        _, out = self.run_statusline(data)
        self.assertNotIn("5h", out)
        self.assertIn("ctx 34%", out)

    def test_shows_git_branch_when_cwd_is_a_repository(self):
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True)
        _, out = self.run_statusline(payload(workspace={"current_dir": str(repo)}), cwd=str(repo))
        self.assertIn("trunk", out)

    def test_marks_the_branch_dirty_when_there_are_uncommitted_changes(self):
        repo = Path(self.tmp.name) / "dirty"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
        (repo / "f.txt").write_text("hi")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
        (repo / "f.txt").write_text("changed")
        _, out = self.run_statusline(payload(workspace={"current_dir": str(repo)}), cwd=str(repo))
        self.assertIn("trunk*", out)

    def test_announces_an_active_hold_with_its_release_time(self):
        until = int(time.time()) + 3600 + 30
        (Path(self.tmp.name) / "blocked.json").write_text(json.dumps({"until": until, "label": "5h"}))
        _, out = self.run_statusline(payload())
        self.assertIn("HOLD", out)
        self.assertIn("5h", out)
        self.assertIn("1h00m", out)

    def test_ignores_a_stale_hold_marker_whose_release_time_has_passed(self):
        (Path(self.tmp.name) / "blocked.json").write_text(
            json.dumps({"until": int(time.time()) - 5, "label": "5h"}))
        _, out = self.run_statusline(payload())
        self.assertNotIn("HOLD", out)

    def test_survives_malformed_stdin_without_crashing(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "statusline.py")],
            input="{not json at all", capture_output=True, text=True,
            env=self.env, cwd="/tmp", timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_marks_guard_as_off_when_disabled(self):
        (Path(self.tmp.name) / "config.json").write_text(json.dumps({"enabled": False}))
        _, out = self.run_statusline(payload())
        self.assertIn("guard off", out)


if __name__ == "__main__":
    unittest.main()
