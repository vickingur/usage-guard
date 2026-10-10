import json, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import guardlib as g  # noqa: E402
import simulate as sim  # noqa: E402

UG = ROOT / "ug"


def run(scenario, policy, days=1.0, dt=10.0, seed=1, **overrides):
    return sim.Simulation(sim.SCENARIOS[scenario], policy, days, dt, seed, g.parse_config(overrides)).run()


class SimulatorTest(unittest.TestCase):
    def test_a_seed_makes_the_run_deterministic(self):
        self.assertEqual(run("mixed", "priority"), run("mixed", "priority"))
        self.assertNotEqual(run("mixed", "priority", seed=2)["priorities"], run("mixed", "priority")["priorities"])

    def test_without_a_guard_a_burst_hits_the_wall_and_every_guarded_policy_does_not(self):
        bare = run("burst", "none", days=2)
        self.assertGreater(bare["windows"]["wall_h"], 0)
        self.assertGreaterEqual(bare["windows"]["five_hour"]["peak_pct"], 100.0)
        # A call released from a pace delay runs without re-checking the threshold,
        # as in the mod, so each concurrent session may land one call past it.
        sc = sim.SCENARIOS["burst"]
        slack = len(sc.initial)
        for policy in ("threshold", "pace", "priority"):
            rep = run("burst", policy, days=2)
            self.assertEqual(rep["windows"]["wall_h"], 0, policy)
            self.assertLessEqual(rep["windows"]["five_hour"]["peak_pct"], 95 + slack * sc.cost_5h, policy)
            self.assertLessEqual(rep["windows"]["seven_day"]["peak_pct"], 90 + slack * sc.cost_7d, policy)

    def test_priority_policy_serves_high_sessions_first_in_a_burst(self):
        rep = run("burst", "priority", days=2, pace_max_delay_seconds=120)
        p = rep["priorities"]
        self.assertGreater(p["high"]["got_per_hour"], p["normal"]["got_per_hour"])
        self.assertGreater(p["normal"]["got_per_hour"], p["low"]["got_per_hour"])
        self.assertLess(p["high"]["wait_p95_s"], p["low"]["wait_p95_s"])
        flat = run("burst", "pace", days=2, pace_max_delay_seconds=120)["priorities"]
        self.assertEqual(flat["high"]["wait_p95_s"], flat["low"]["wait_p95_s"])

    def test_a_lone_low_session_borrows_and_is_paced_like_a_high_one(self):
        lone = sim.Scenario("lone", ("low",), 0.0, 1, {"low": 1}, {p: sim.STEADY for p in g.PRIORITIES}, 0.1, 0.009)
        rep = sim.Simulation(lone, "priority", 1.0, 10.0, 1).run()
        self.assertGreaterEqual(rep["priorities"]["low"]["lift_mean"], 1.9)
        self.assertLessEqual(rep["priorities"]["low"]["wait_p95_s"], 30.0)

    def test_hold_mode_keeps_the_week_on_the_line_without_a_threshold_hold(self):
        rep = run("burst", "priority", days=2, pace_mode="hold")
        delay = run("burst", "priority", days=2)
        self.assertLess(rep["windows"]["seven_day"]["end_pct"], 90)
        self.assertLess(rep["windows"]["held_h"], delay["windows"]["held_h"])
        self.assertGreater(rep["priorities"]["low"]["paced_h"], delay["priorities"]["low"]["paced_h"])

    def test_the_report_carries_a_timeline_and_the_overrides(self):
        rep = run("solo", "pace", days=0.5, pace_max_delay_seconds=7)
        self.assertEqual(rep["config"], {"pace_max_delay_seconds": 7.0})
        self.assertEqual(len(rep["timeline"]), 13)
        self.assertEqual(rep["timeline"][0]["t_h"], 0.0)
        self.assertTrue(all(0 <= p["seven_day_pct"] <= 100 for p in rep["timeline"]))

    def test_render_and_compare_are_compact_text(self):
        reps = [run("mixed", policy, days=0.5) for policy in sim.POLICIES]
        text = sim.render(reps[3])
        self.assertIn("sim mixed · 0.5d · seed 1 · policy priority", text)
        self.assertIn("△ high", text)
        self.assertIn("⧖", text)
        table = sim.render_compare(reps)
        self.assertEqual(len([l for l in table.splitlines() if l.startswith(("none", "threshold", "pace", "priority"))]), 4)

    def test_strip_scales_to_the_cap(self):
        self.assertEqual(sim.strip([0, 50, 100]), "▁▅█")
        self.assertEqual(sim.strip([]), "")
        self.assertEqual(len(sim.strip(list(range(100)), width=10)), 10)

    def test_html_writes_a_self_contained_page_of_every_scenario(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "sim.html"
            proc = subprocess.run([sys.executable, str(UG), "sim", "--scenario", "all", "--days", "0.5", "--dt", "60", "--html", str(out)],
                                  capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            page = out.read_text()
            self.assertIn("<title>Usage Guard Simulator</title>", page)
            data = json.loads(page.split('<script id="data" type="application/json">', 1)[1].split("</script>", 1)[0])
            self.assertEqual(len(data), len(sim.SCENARIOS) * len(sim.POLICIES))
            self.assertNotIn("__DATA__", page)
            self.assertNotIn("https://", page.split("<script>", 1)[1])  # the page's own script loads nothing

    def test_scenario_all_needs_a_page_or_json(self):
        proc = subprocess.run([sys.executable, str(UG), "sim", "--scenario", "all"], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2)

    def test_ug_sim_runs_the_cli_and_rejects_a_bad_override(self):
        proc = subprocess.run([sys.executable, str(UG), "sim", "--scenario", "solo", "--days", "0.5", "--dt", "30", "--json"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["scenario"], "solo")
        proc = subprocess.run([sys.executable, str(UG), "sim", "--set", "nope=1"], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
