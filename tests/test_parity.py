"""The Python pace math answers exactly what hooks/pace.ts answers.

Both sides read hooks/pace-cases.ts and hooks/defaults.ts; hooks/pace.test.ts
runs the same cases under `claude plugin test`.
"""
import json, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import guardlib as g  # noqa: E402

CASES = json.loads((ROOT / "hooks" / "pace-cases.ts").read_text().split("export const CASES =", 1)[1])


def rnd(x):
    return round(x, 3)


class ParityTest(unittest.TestCase):
    def test_defaults_table_is_the_typescript_one(self):
        self.assertEqual(g.DEFAULTS, json.loads((ROOT / "hooks" / "defaults.ts").read_text().split("export const DEFAULTS =", 1)[1]))

    def test_every_case_answers_the_same_in_python(self):
        for case in CASES:
            with self.subTest(case["name"]):
                cfg = g.parse_config(case["config"])
                t = g.terms(case["priority"], case["idle"], cfg)
                want = case["expect"]["terms"]
                self.assertEqual((rnd(t.margin_factor), rnd(t.delay_factor), rnd(t.lift)),
                                 (want["marginFactor"], want["delayFactor"], want["lift"]))
                now = case["now"]
                cache = {"ts": now}
                for w in case["windows"]:
                    cache[w["key"]] = {"used_percentage": w["pct"], "resets_at": now + w["resets_in"]}
                got = g.paces(cache, cfg, now, t, g.profile_of(cfg, 0))
                self.assertEqual(len(got), len(case["expect"]["paces"]))
                for p, want in zip(got, case["expect"]["paces"]):
                    self.assertEqual(p.label, want["label"])
                    self.assertEqual(rnd(p.line), want["line"])
                    self.assertEqual(rnd(p.ahead), want["ahead"])
                    self.assertEqual(rnd(p.margin), want["margin"])
                    self.assertEqual(p.active, want["active"])
                    self.assertEqual(rnd(p.delay_seconds), want["delaySeconds"])
                    self.assertEqual(p.catchup_at - now if p.active else 0, want["catchup_in"])


if __name__ == "__main__":
    unittest.main()
