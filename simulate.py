#!/usr/bin/env python3
"""Simulate sessions against the guard's own policy.

Each session is a Markov chain over `think` (the person reads and types, no
calls), `work` (the model runs and makes tool calls at its intensity) and
`done`; new sessions arrive as a Poisson process. Every call costs a share of
the account's 5h and 7d windows, which reset the way Claude's do: 5h after the
first call of the window, 7d after the first call of the week. At 100% the API
refuses, so a session hits the wall and waits for the reset whatever the
policy; the guard's point is to never get there.

Policies, all through guardlib (the same math the mod runs):

    none        no guard: sessions run until they hit the wall
    threshold   hold at the threshold only
    pace        hold, plus pacing on the account terms (every session alike)
    priority    hold, plus pacing on each session's own terms with borrowing

`ug sim [--scenario S] [--policy P | --compare] [--days D] [--seed N] [--set KEY=VALUE] [--json]`.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guardlib as g  # noqa: E402

POLICIES = ("none", "threshold", "pace", "priority")
GLYPH = {"low": "▽", "normal": "◇", "high": "△"}
H, D = 3600.0, 86400.0


@dataclass(frozen=True)
class Profile:
    """How a session of one priority behaves."""

    calls_per_minute: float   # intensity while working
    work_mean_s: float        # mean length of a work burst before the person reads
    think_mean_s: float       # mean wait before the next burst
    life_mean_s: float        # mean session length (inf: lives the whole run)


@dataclass(frozen=True)
class Scenario:
    name: str
    initial: tuple            # priorities of the sessions present at t=0
    arrivals_per_hour: float  # Poisson arrivals of new sessions
    max_concurrent: int
    priority_mix: dict        # for arrivals: {priority: weight}
    profiles: dict            # {priority: Profile}
    cost_5h: float            # % of the 5h window one call costs
    cost_7d: float            # % of the 7d window one call costs
    activity: Optional[g.Profile] = None   # when set, arrivals and waking follow this weekly profile


# The run starts on a Monday at 00:00 in the simulated local time (offset 0),
# so a weekly profile lines up with its days.
MONDAY = 345600.0
WORKWEEK = g.Profile((1.0,) * 5 + (0.3, 0.3), (0.1,) * 8 + (1.0,) * 15 + (0.1,))


INTERACTIVE = Profile(calls_per_minute=1.5, work_mean_s=15 * 60, think_mean_s=15 * 60, life_mean_s=6 * H)
BATCH = Profile(calls_per_minute=3.0, work_mean_s=2 * H, think_mean_s=2 * 60, life_mean_s=math.inf)
STEADY = Profile(calls_per_minute=2.0, work_mean_s=20 * 60, think_mean_s=10 * 60, life_mean_s=math.inf)
HEAVY = Profile(calls_per_minute=3.0, work_mean_s=H, think_mean_s=5 * 60, life_mean_s=2 * D)

SCENARIOS = {
    "solo": Scenario("solo", ("normal",), 0.0, 1, {"normal": 1}, {p: STEADY for p in g.PRIORITIES}, 0.1, 0.009),
    "mixed": Scenario("mixed", ("high", "normal"), 0.4, 6, {"high": 0.2, "normal": 0.6, "low": 0.2},
                      {p: INTERACTIVE for p in g.PRIORITIES}, 0.1, 0.009),
    "batch": Scenario("batch", ("high", "low", "low", "low"), 0.0, 4, {"low": 1},
                      {"high": Profile(1.0, 10 * 60, 20 * 60, math.inf), "normal": INTERACTIVE, "low": BATCH}, 0.1, 0.009),
    "burst": Scenario("burst", ("high", "high", "normal", "normal", "normal", "low"), 0.0, 6, {"normal": 1},
                      {p: HEAVY for p in g.PRIORITIES}, 0.1, 0.009),
    # A working week: sessions come and go in office hours Monday to Friday, a little at the weekend.
    "workweek": Scenario("workweek", (), 1.2, 4, {"high": 0.3, "normal": 0.5, "low": 0.2},
                         {p: INTERACTIVE for p in g.PRIORITIES}, 0.1, 0.009, WORKWEEK),
}


class Windows:
    """The account's two windows, as Claude keeps them."""

    def __init__(self, cost_5h: float, cost_7d: float):
        self.cost = {"five_hour": cost_5h, "seven_day": cost_7d}
        self.length = {"five_hour": 5 * H, "seven_day": 7 * D}
        self.start = {"five_hour": None, "seven_day": None}
        self.used = {"five_hour": 0.0, "seven_day": 0.0}
        self.resets = {"five_hour": 0, "seven_day": 0}
        self.peak = {"five_hour": 0.0, "seven_day": 0.0}

    def reading(self, t: float):
        """The cache the guard would read at `t`; None before the first call."""
        out = {"ts": t}
        for key, start in self.start.items():
            if start is not None:
                out[key] = {"used_percentage": self.used[key], "resets_at": start + self.length[key]}
        return out if len(out) > 1 else None

    def wall_until(self, t: float):
        """When a window at 100% resets; None when the API would still answer."""
        for key, start in self.start.items():
            if start is not None and self.used[key] >= 100 and t < start + self.length[key]:
                return start + self.length[key]
        return None

    def charge(self, t: float) -> None:
        for key in self.start:
            start = self.start[key]
            if start is None or t >= start + self.length[key]:
                if start is not None:
                    self.resets[key] += 1
                self.start[key], self.used[key] = t, 0.0
            self.used[key] += self.cost[key]
            self.peak[key] = max(self.peak[key], self.used[key])


@dataclass
class Session:
    id: str
    priority: str
    profile: Profile
    rng: random.Random
    born: float
    state: str = "work"
    blocked_until: float = 0.0
    blocked_kind: str = ""
    last_call: float = -math.inf
    calls: int = 0
    waits: list = field(default_factory=list)   # per-call pace delays
    paced_s: float = 0.0
    held_s: float = 0.0
    wall_s: float = 0.0
    work_s: float = 0.0
    lift_sum: float = 0.0
    track: dict = field(default_factory=dict)   # hour bucket -> {state: seconds}


BLOCKED_TIME = {"pace": "paced_s", "hold": "held_s", "wall": "wall_s"}
TRACK_STATES = ("work", "think", "pace", "hold", "wall")
TRACK_BUCKET_S = 3600.0


def poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def weighted(rng: random.Random, mix: dict) -> str:
    total = sum(mix.values())
    x = rng.random() * total
    for key, w in mix.items():
        x -= w
        if x <= 0:
            return key
    return next(iter(mix))


class Simulation:
    def __init__(self, scenario: Scenario, policy: str, days: float, dt: float, seed: int, cfg=None):
        self.sc, self.policy, self.days, self.dt, self.seed = scenario, policy, days, dt, seed
        self.cfg = cfg or g.parse_config({})
        self.profile = g.profile_of(self.cfg, 0)
        self.rng = random.Random(seed)
        self.windows = Windows(scenario.cost_5h, scenario.cost_7d)
        self.sessions: list = []
        self.gone: list = []
        self.samples: list = []   # (t, used_5h, used_7d, line_7d)
        self.held_steps = 0
        self.wall_steps = 0

    # --- the chain --------------------------------------------------------------

    def spawn(self, priority: str, t: float) -> Session:
        idx = len(self.sessions) + len(self.gone)
        s = Session(f"s{idx}", priority, self.sc.profiles[priority], random.Random(self.seed * 1000 + idx), t)
        self.sessions.append(s)
        return s

    def decide(self, s: Session, t: float):
        """What the guard does with a call now: (seconds to wait, 'hold' | 'pace' | '')."""
        cache = self.windows.reading(t)
        if cache is None or self.policy == "none":
            return 0.0, ""
        # The guard's math runs on wall-clock seconds so the weekly profile lines
        # up with its days; the registry and the run keep the simulation's own clock.
        clock = t + MONDAY
        cache = {k: ({"used_percentage": v["used_percentage"], "resets_at": v["resets_at"] + MONDAY} if isinstance(v, dict) else v)
                 for k, v in cache.items()}
        vio = g.violations(cache, self.cfg, clock)
        if vio:
            return max(v.resets_at for v in vio) - clock, "hold"
        if self.policy == "threshold":
            return 0.0, ""
        if self.policy == "pace":
            t_ = g.ACCOUNT_TERMS
        else:
            entries = [{"id": o.id, "priority": o.priority, "last_call": o.last_call} for o in self.sessions]
            t_ = g.terms(s.priority, g.idle_above(entries, s.id, t), self.cfg)
            s.lift_sum += t_.lift
        pace = g.paces(cache, self.cfg, clock, t_, self.profile)
        if self.cfg["pace_mode"] == "hold":
            active = [x for x in pace if x.active]
            if not active:
                return 0.0, ""
            wait = min(max(x.catchup_at for x in active) - clock, float(self.cfg["max_stall_seconds"]))
            return wait, "pace"
        delay = g.pace_delay(pace)
        return delay, "pace" if delay > 0 else ""

    def attempt(self, s: Session, t: float) -> None:
        s.last_call = t
        wall = self.windows.wall_until(t)
        if wall is not None:
            s.blocked_until, s.blocked_kind = wall, "wall"
            return
        wait, kind = self.decide(s, t)
        if kind == "hold":
            s.blocked_until, s.blocked_kind = t + wait, "hold"
            return
        s.waits.append(wait)
        if wait > 0:
            s.blocked_until, s.blocked_kind = t + wait, "pace"
            return
        self.execute(s, t)

    def execute(self, s: Session, t: float) -> None:
        self.windows.charge(t)
        s.calls += 1

    def step(self, s: Session, t: float) -> None:
        dt, p = self.dt, s.profile
        if s.state == "work":
            s.work_s += dt
        blocked = t < s.blocked_until
        bucket = s.track.setdefault(int(t // TRACK_BUCKET_S), dict.fromkeys(TRACK_STATES, 0.0))
        bucket[s.blocked_kind if blocked else s.state] += dt
        if blocked:
            attr = BLOCKED_TIME[s.blocked_kind]
            setattr(s, attr, getattr(s, attr) + dt)
            return
        if s.blocked_kind:
            kind, s.blocked_kind = s.blocked_kind, ""
            if kind == "pace":
                self.execute(s, t)
            else:
                self.attempt(s, t)   # the window reset: the held call goes now
                if s.blocked_kind:
                    return
        r = s.rng
        if p.life_mean_s != math.inf and r.random() < dt / p.life_mean_s:
            s.state = "done"
            return
        activity = 1.0 if self.sc.activity is None else self.sc.activity.weight_at(MONDAY + t)
        if s.state == "think":
            if r.random() < activity * dt / p.think_mean_s:
                s.state = "work"
            return
        if r.random() < dt / p.work_mean_s:
            s.state = "think"
            return
        for _ in range(poisson(r, p.calls_per_minute * dt / 60)):
            self.attempt(s, t)
            if s.blocked_kind:
                break

    # --- the run ----------------------------------------------------------------------

    def run(self) -> dict:
        t, end, dt = 0.0, self.days * D, self.dt
        for priority in self.sc.initial:
            self.spawn(priority, 0.0)
        next_sample = 0.0
        while t < end:
            activity = 1.0 if self.sc.activity is None else self.sc.activity.weight_at(MONDAY + t)
            for _ in range(poisson(self.rng, activity * self.sc.arrivals_per_hour * dt / H)):
                if len(self.sessions) < self.sc.max_concurrent:
                    self.spawn(weighted(self.rng, self.sc.priority_mix), t)
            for s in self.sessions:
                self.step(s, t)
            held = any(s.blocked_kind == "hold" and t < s.blocked_until for s in self.sessions)
            walled = any(s.blocked_kind == "wall" and t < s.blocked_until for s in self.sessions)
            self.held_steps += held
            self.wall_steps += walled
            if t >= next_sample:
                self.sample(t)
                next_sample += H
            done = [s for s in self.sessions if s.state == "done"]
            if done:
                self.gone.extend(done)
                self.sessions = [s for s in self.sessions if s.state != "done"]
            t += dt
        self.sample(t)
        return self.report()

    def sample(self, t: float) -> None:
        w = self.windows
        line = None
        if w.start["seven_day"] is not None:
            line = g.pace_line(float(self.cfg["threshold_7d"]), MONDAY + w.start["seven_day"] + w.length["seven_day"],
                               w.length["seven_day"], MONDAY + t, self.profile)
        # A window whose reset has passed with no call since holds nothing any more.
        live = {key: w.start[key] is not None and t < w.start[key] + w.length[key] for key in w.start}
        self.samples.append((t, w.used["five_hour"] if live["five_hour"] else 0.0,
                             w.used["seven_day"] if live["seven_day"] else 0.0, line))

    def report(self) -> dict:
        everyone = self.sessions + self.gone
        by_priority = {}
        for priority in g.PRIORITIES:
            group = [s for s in everyone if s.priority == priority]
            if not group:
                continue
            waits = sorted(w for s in group for w in s.waits)
            work_h = sum(s.work_s for s in group) / H
            calls = sum(s.calls for s in group)
            by_priority[priority] = {
                "sessions": len(group), "calls": calls,
                "nominal_per_hour": round(self.sc.profiles[priority].calls_per_minute * 60, 1),
                "got_per_hour": round(calls / work_h, 1) if work_h else 0.0,
                "wait_mean_s": round(sum(waits) / len(waits), 1) if waits else 0.0,
                "wait_p95_s": round(waits[int(0.95 * (len(waits) - 1))], 1) if waits else 0.0,
                "paced_h": round(sum(s.paced_s for s in group) / H, 2),
                "held_h": round(sum(s.held_s for s in group) / H, 2),
                "wall_h": round(sum(s.wall_s for s in group) / H, 2),
                "lift_mean": round(sum(s.lift_sum for s in group) / max(1, len(waits)), 2),
            }
        w = self.windows
        ahead = [1 for _, _, used7, line in self.samples if line is not None and used7 > line]
        return {
            "scenario": self.sc.name, "policy": self.policy, "days": self.days, "dt": self.dt, "seed": self.seed,
            "config": {k: v for k, v in self.cfg.items() if v != g.DEFAULTS[k] and not k.startswith("pace_profile_")},
            "profile": "uniform" if self.profile.is_uniform() else "workweek" if (list(self.profile.days), list(self.profile.hours)) == (list(WORKWEEK.days), list(WORKWEEK.hours)) else "custom",
            "activity": "uniform" if self.sc.activity is None else "workweek",
            "windows": {
                "five_hour": {"peak_pct": round(w.peak["five_hour"], 1), "resets": w.resets["five_hour"]},
                "seven_day": {"end_pct": round(w.used["seven_day"], 1), "peak_pct": round(w.peak["seven_day"], 1),
                              "threshold_pct": self.cfg["threshold_7d"],
                              "ahead_of_line_share": round(len(ahead) / max(1, len(self.samples)), 2)},
                "held_h": round(self.held_steps * self.dt / H, 2),
                "wall_h": round(self.wall_steps * self.dt / H, 2),
            },
            "priorities": by_priority,
            "sessions": [{
                "id": s.id, "priority": s.priority, "born_h": round(s.born / H, 1),
                "track": [[int(b), *[round(100 * v / TRACK_BUCKET_S) for v in bucket.values()]]
                          for b, bucket in sorted(s.track.items())],
            } for s in everyone],
            "timeline": [{"t_h": round(t / H, 1), "five_hour_pct": round(a, 1), "seven_day_pct": round(b, 1),
                          "seven_day_line_pct": None if line is None else round(line, 1)}
                         for t, a, b, line in self.samples],
        }


# --- output ---------------------------------------------------------------------------

BARS = "▁▂▃▄▅▆▇█"


def strip(values, cap=100.0, width=28) -> str:
    """One glyph per bucket of the series, 0..cap."""
    if not values:
        return ""
    per = max(1, math.ceil(len(values) / width))
    out = []
    for i in range(0, len(values), per):
        v = max(values[i:i + per])
        out.append(BARS[min(7, int(8 * max(0.0, min(v, cap)) / cap))] if v < cap else "█")
    return "".join(out)


def hours(x: float) -> str:
    return f"{x:.1f}h"


def render(rep: dict) -> str:
    w = rep["windows"]
    lines = [f"sim {rep['scenario']} · {rep['days']:g}d · seed {rep['seed']} · policy {rep['policy']} · line {rep['profile']}"]
    seven = [p["seven_day_pct"] for p in rep["timeline"]]
    five = [p["five_hour_pct"] for p in rep["timeline"]]
    lines.append(f"7d  {strip(seven)}  end {w['seven_day']['end_pct']:.0f}% of {w['seven_day']['threshold_pct']:g}"
                 f" · peak {w['seven_day']['peak_pct']:.0f}% · ahead of line {w['seven_day']['ahead_of_line_share'] * 100:.0f}% of the time")
    lines.append(f"5h  {strip(five)}  peak {w['five_hour']['peak_pct']:.0f}% · {w['five_hour']['resets']} resets"
                 f" · held {hours(w['held_h'])} · wall {hours(w['wall_h'])}")
    lines.append(f"{'':<10}{'sess':>5}{'calls':>7}{'nom/h':>7}{'got/h':>7}{'wait µ':>8}{'p95':>7}{'◔':>7}{'⊘':>7}{'wall':>7}{'⇡':>6}")
    for priority, row in rep["priorities"].items():
        lines.append(f"{GLYPH[priority]} {priority:<8}{row['sessions']:>5}{row['calls']:>7}{row['nominal_per_hour']:>7.0f}"
                     f"{row['got_per_hour']:>7.0f}{row['wait_mean_s']:>7.0f}s{row['wait_p95_s']:>6.0f}s"
                     f"{hours(row['paced_h']):>7}{hours(row['held_h']):>7}{hours(row['wall_h']):>7}{row['lift_mean']:>6.1f}")
    return "\n".join(lines)


def render_compare(reps: list) -> str:
    first = reps[0]
    lines = [f"sim {first['scenario']} · {first['days']:g}d · seed {first['seed']} · policies compared · line {first['profile']}"]
    prios = [p for p in g.PRIORITIES if p in first["priorities"]]
    head = f"{'policy':<10}{'7d end':>7}{'5h peak':>8}{'⊘':>6}{'wall':>6}"
    for p in prios:
        head += f"{GLYPH[p] + ' got/h':>9}{GLYPH[p] + ' p95':>8}"
    lines.append(head)
    for rep in reps:
        w = rep["windows"]
        row = f"{rep['policy']:<10}{w['seven_day']['end_pct']:>6.0f}%{w['five_hour']['peak_pct']:>7.0f}%{hours(w['held_h']):>6}{hours(w['wall_h']):>6}"
        for p in prios:
            r = rep["priorities"][p]
            row += f"{r['got_per_hour']:>9.0f}{r['wait_p95_s']:>7.0f}s"
        lines.append(row)
    lines.append("7d  " + "  ".join(f"{rep['policy']} {strip([p['seven_day_pct'] for p in rep['timeline']], width=14)}" for rep in reps))
    return "\n".join(lines)


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="ug sim", description=__doc__.split("\n\n")[0])
    ap.add_argument("--scenario", choices=list(SCENARIOS) + ["all"], default="mixed")
    ap.add_argument("--policy", choices=POLICIES, default="priority")
    ap.add_argument("--compare", action="store_true", help="run every policy on the same seed")
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--dt", type=float, default=5.0, help="step in seconds")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="override a guard setting for the run, e.g. pace_max_delay_seconds=120 or pace_mode=hold")
    ap.add_argument("--profile", choices=("uniform", "workweek", "both"), default="uniform",
                    help="the weekly profile the guard's 7d pace line follows; `both` runs each (with --html or --json)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--html", metavar="FILE", help="write a self-contained page of the runs (implies --compare)")
    args = ap.parse_args(argv)
    if args.html:
        args.compare = True
    overrides = {}
    for item in args.set:
        key, _, raw = item.partition("=")
        if key not in g.DEFAULTS or not raw:
            ap.error(f"--set needs KEY=VALUE with a key from `ug config`, got {item!r}")
        default = g.DEFAULTS[key]
        if isinstance(default, list):
            overrides[key] = [float(v) for v in raw.split(",")]
        else:
            overrides[key] = raw if isinstance(default, str) else raw.lower() in ("1", "true", "on") if isinstance(default, bool) else float(raw)
    if (args.scenario == "all" or args.profile == "both") and not args.html and not args.json:
        ap.error("--scenario all and --profile both need --html or --json")
    cfgs = []
    for profile in (("uniform", "workweek") if args.profile == "both" else (args.profile,)):
        shaped = dict(overrides)
        if profile == "workweek":
            shaped["pace_profile_days"], shaped["pace_profile_hours"] = list(WORKWEEK.days), list(WORKWEEK.hours)
        cfgs.append(g.parse_config(shaped))
    policies = POLICIES if args.compare else (args.policy,)
    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    reps = [Simulation(SCENARIOS[name], policy, args.days, args.dt, args.seed, cfg).run()
            for cfg in cfgs for name in names for policy in policies]
    if args.html:
        page = (Path(__file__).resolve().parent / "sim-page.html").read_text()
        Path(args.html).write_text(page.replace("__DATA__", json.dumps(reps).replace("</", "<\\/")))
        print(f"wrote {args.html}: {len(reps)} runs")
        return 0
    if args.json:
        print(json.dumps(reps if args.compare else reps[0], indent=2))
    elif args.compare:
        print(render_compare(reps))
    else:
        print(render(reps[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
