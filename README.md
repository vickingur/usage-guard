# usage-guard

Keeps Claude Code and Codex inside their usage windows and tells agents where
they stand. For Claude it is a **mod**: a plugin of function hooks that runs
inside Claude Code, in the terminal and in the desktop app, on macOS and Linux.
For Codex it is a pair of command hooks. A small Python CLI, `ug`, drives both.

```
/plugin install usage-guard --marketplace vickingur/usage-guard
```

## What it does

- **Threshold hold.** A window at or over its threshold (5h at 95%, 7d at 90%
  by default) stalls every tool call until it resets, or until `ug release`,
  `ug off`, or a raised threshold. The model burns nothing while stalled.
- **Pacing.** Before the threshold, a window running ahead of its *pace line*
  by more than a margin delays each tool call in proportion, and the model is
  told why and advised to take fewer, larger steps.
- **Priorities.** Each Claude session runs at `low`, `normal` or `high`.
  Lower priorities are paced earlier and harder, and borrow a higher class's
  terms progressively while that class sits idle on the machine.
- **Visibility.** The prompt footer's right corner shows both windows, Codex's,
  any pacing or hold, and the session's priority with buttons to step it. Every
  prompt carries a `[usage]` line as context; `ug status --json` has every
  number.

## Install

From a checkout, on each machine:

```
git clone https://github.com/vickingur/usage-guard ~/.claude/usage-guard
~/.claude/usage-guard/ug install
```

`ug install` registers the checkout as a plugin marketplace, installs the
plugin from it (so the folder is what runs: `git pull` then `/reload-plugins`
updates it), removes the command hooks and statusline an earlier version wired
into `~/.claude/settings.json`, and links `ug` into `~/.local/bin`. The plugin
loads in every new session, terminal or desktop. `ug codex install` wires Codex.

Python 3.9 or later, standard library only. The mod itself needs nothing.

## The footer

Everything sits at the bottom right, where the prompt footer keeps its mode
labels, on the terminal and in the desktop app:

```
● 5h 31% +2 ↻3h32m·29%  7d 67% +8 ↻2d9h·66%  cx 44%  ⧖5h@45%~1h20m ⊘@95%~4h  ‹ ◇ normal ⇡△ ›
```

| Mark | Meaning |
|---|---|
| `●` `⧖` `⊘` `○` `~` `·` | the guard's state: fine, pacing, held, off, figures older than ten minutes, no data yet |
| `5h 31% +2` | used, and how far ahead of the pace line; `+8▲` in amber while pacing |
| `↻3h32m·29%` | time until the window resets, and how much of the window has elapsed |
| `cx 3%/44%` | Codex's 5h and 7d windows (one figure when only one is known) |
| `⧖5h@45%~1h20m` | pacing would start at 45% on the 5h window, in about 1h20m at the current burn rate |
| `⊘@95%~4h` | the hold would start at 95%, in about 4h; the rate is read off the last hour of readings and left out until there is one |
| `⧖20s ↺1h12m` | while pacing: the per-call delay and when the window is back on pace |
| `⊘ 41m →14:00` | while held: how long until the hold lifts, and when |
| `~22m` | the figures are from 22 minutes ago (the session has been idle) |
| `▽ low`, `◇ normal`, `△ high` | the session's priority; `›` steps it up and `‹` down, wrapping (hotkeys `p` and `o` while the footer has focus) |
| `⇡60%△`, `⇡△` | what it borrows: 60% of the way to high's terms, or high's terms whole |

The `[usage]` line the model reads says the same in words, with the reset
times and the block and unblock points. `ug sessions` uses the same marks.
`/ug priority high` sets the priority by name, `/ug priority` cycles, `/ug`
prints the position in words.

A session starts at the plugin option `priority` (`/config`, default
`normal`), or at `UG_PRIORITY` from the environment when set:
`UG_PRIORITY=low claude -p "..."` for a batch job that should yield.

## Pace lines, terms and borrowing

Each window has a **pace line**: the usage you would have now if spend were
spread evenly across the window and landed exactly on the threshold at reset.
Two hours into a 5h window with a 95% threshold the line is 38%. The window is
**ahead** by `used - line`. Pacing engages when ahead exceeds the session's
**margin** and usage is at least `pace_min_used_pct` (30%, so a burst right
after a reset is left alone). Each call then sleeps `pace_seconds_per_pct` per
point over the margin, capped at `pace_max_delay_seconds`; both scaled by the
session's **delay factor**.

| Priority | Margin | Delay | Example: 2h into 5h at 70% (32 ahead) |
|---|---|---|---|
| high | the window's (20 / 15) | x1 | 12 over, 30s per call (the cap) |
| normal | half | x2 | 22 over, 60s per call |
| low | none | x4 | 32 over, 120s per call |

**Borrowing.** Sessions on one machine see each other through
`~/.claude/usage-guard/sessions/`. A session below `high` watches the class
above it: once no other session of that class has made a tool call for
`borrow_after_seconds` (120) it starts taking on that class's margin and delay
factor, linearly, and has them whole at `borrow_full_seconds` (600). Only then
does it start on the next class up. A class with no session at all lends at
once, so a lone low session on a quiet machine runs on high's terms, and drops
back to its own within one call when a high session makes one. The footer and
`ug sessions` say what each session is borrowing.

The threshold hold is the account's wall and ignores priority.

## Simulating the policy

`ug sim` runs sessions against the guard's own math (guardlib, the same
functions the mod mirrors) so a setting can be judged before it is changed.
Each session is a Markov chain over `think` (no calls), `work` (tool calls at
its intensity) and `done`; new sessions arrive as a Poisson process, with a
priority drawn from the scenario's mix. Every call costs a share of the 5h and
7d windows, which reset as Claude's do. At 100% the API refuses, so a session
hits the wall and waits for the reset whatever the policy.

```
ug sim --scenario burst --compare
ug sim --scenario batch --policy priority --days 3 --seed 7
ug sim --scenario mixed --compare --set pace_max_delay_seconds=120
ug sim --scenario solo --set pace_mode=hold --json
ug sim --scenario all --html sim.html
```

| Scenario | Sessions |
|---|---|
| `solo` | one normal session, steady work all week |
| `mixed` | interactive sessions arriving at 0.4/h, up to 6, 20/60/20 high/normal/low |
| `batch` | one interactive high session beside three low batch runners that rarely pause |
| `burst` | six heavy sessions for two days, then quiet |

`--compare` runs `none` (no guard), `threshold`, `pace` (account terms) and
`priority` (each session's terms with borrowing) on the same seed, and reports
the week's end, the 5h peak, hours held (`⊘`), hours lost at the wall, and per
priority the calls per working hour and the p95 wait. A single policy prints
the hourly strips of both windows and a row per priority: calls, nominal and
achieved rate, mean and p95 wait, hours paced, held and at the wall, and the
mean lift borrowed. `--set KEY=VALUE` overrides any key from `ug config` for
the run. Runs are deterministic per seed; a week at the default 5s step takes
about a second. `--html FILE` writes one self-contained page of the compared
policies (every scenario with `--scenario all`): the window curves against the
pace line and the threshold, a policy table to click through, and what each
priority got.

## Where the numbers come from

Claude Code hands its rate limits to the mod directly (`$.session.usage()` and
the `session.measure` event), so nothing is scraped and no statusline is
needed. The mod writes what it sees to `usage.json` for `ug`, and its session
entry to `sessions/<id>.json` for the other sessions. Without a reading (a
session's first turn, or data older than `stale_after_seconds`) the guard
**fails open**.

Codex has no plugin system: `ug codex install` adds one command hook for its
`UserPromptSubmit` and `PreToolUse` events to `~/.codex/hooks.json` and records
their trust hashes in `~/.codex/config.toml`, standing in for the review Codex
would ask for in its TUI. Every turn's `token_count` event in a Codex session
log carries the rate limits the API returned; the hook refreshes
`codex-usage.json` from the live transcript before deciding. Codex sessions
run on the account terms.

## How the hold works inside a mod

A hook has ten seconds of its own time per dispatch, but time spent inside an
engine call is free. The guard therefore waits on the host (`sleep`, present on
every macOS and Linux) in chunks of `poll_seconds`, re-reading config, usage and
`state.json` between chunks, so every escape hatch takes effect within one
poll. `max_stall_seconds` (6h) caps a hold; past it the call is denied with the
reset time. Should the hook itself be lost mid-hold, its fallback denies the
call rather than letting it through; a fault while merely pacing lets the call
run.

Overhead when nothing is ahead: a few file reads per tool call, in-process. The
registry is re-read at most every five seconds.

## Commands

    ug status [--json]     guard state, both vendors, pace lines, sessions, any hold
    ug brief               the one-line position agents receive at every prompt
    ug sessions            live Claude sessions: priority, borrowing, last call, holds
    ug priority P [ID]     set a session's priority; ID is an id prefix, optional with one session
    ug on | off            enable / disable the guard
    ug threshold [W PCT]   show, or set, the threshold for 5h or 7d
    ug pace                show pacing settings
    ug pace on | off       enable / disable pacing (the threshold hold stays)
    ug pace mode M         delay or hold
    ug pace margin W PCT   how far ahead of the pace line window W may run
    ug pace set KEY VALUE  any numeric pacing, priority or borrow setting
    ug sim [...]           simulate sessions against the policy (below)
    ug install             install the plugin from this checkout, drop the legacy hooks
    ug codex install       add the guard's hook to Codex and trust it
    ug codex trusted       exit 0 when Codex has the hook and trusts it
    ug release             release an in-progress hold now
    ug config              effective settings as JSON

Inside a session: `/ug`, `/ug priority`, `/ug priority low|normal|high`.

## Config: `config.json` (node-local)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master switch |
| `threshold_5h` | `95.0` | Hold when the 5h window reaches this percent |
| `threshold_7d` | `90.0` | Hold when the 7d window reaches this percent |
| `max_stall_seconds` | `21600` | Cap on a single hold before a deny (threshold) or an allow (pace) |
| `poll_seconds` | `5` | How often a hold or delay re-checks for release |
| `stale_after_seconds` | `600` | Usage older than this is ignored (fails open) |
| `pace_enabled` | `true` | Pacing on or off |
| `pace_mode` | `"delay"` | `delay` or `hold` |
| `pace_margin_5h` | `20.0` | Points ahead of the 5h pace line a high session may run |
| `pace_margin_7d` | `15.0` | Same for the weekly window |
| `pace_min_used_pct` | `30.0` | Pacing never engages below this usage |
| `pace_seconds_per_pct` | `5.0` | Delay per point over the margin |
| `pace_max_delay_seconds` | `30.0` | Cap on the per-call delay at delay factor 1 |
| `priority_margin_factor_normal` | `0.5` | A normal session's share of the margin |
| `priority_margin_factor_low` | `0.0` | A low session's share |
| `priority_delay_factor_normal` | `2.0` | Stretch on a normal session's delays |
| `priority_delay_factor_low` | `4.0` | Stretch on a low session's delays |
| `borrow_after_seconds` | `120` | Idle time of the class above before borrowing starts |
| `borrow_full_seconds` | `600` | Idle time at which the class above is borrowed whole |
| `session_stale_seconds` | `21600` | A session entry older than this no longer counts |
| `codex_log_max_age_seconds` | `604800` | Oldest Codex session log that still counts |

Only the keys you want to override need to be there; a key of the wrong type
is left at its default. The table lives once, in `hooks/defaults.ts`, and the
Python side reads it from there.

## Layout

| Path | Role |
|---|---|
| `hooks/register.tsx` | The mod: guard, footer, `/ug`, session registry |
| `hooks/pace.ts` | Pace math and the shapes on disk, pure |
| `hooks/defaults.ts`, `hooks/pace-cases.ts` | The config table and parity cases both languages read |
| `types/index.d.ts` | The mod's state contract |
| `guardlib.py` | The same math for the CLI and Codex, the Codex reader, the report |
| `codex-hook.py` | Codex's command hook |
| `ug` | The CLI |
| `simulate.py`, `sim-page.html` | `ug sim`: the Markov-chain simulator and its page |

## Tests

    claude plugin test ~/.claude/usage-guard
    python3 -B -m unittest discover -s ~/.claude/usage-guard/tests

The mod's tests run against Claude Code's own engine with the file system,
clock and host sleeps answered in memory. The Python tests redirect every path
with `UG_DIR`, `UG_CODEX_SESSIONS`, `UG_CODEX_HOME` and `UG_CLAUDE_SETTINGS`,
so they never touch live state. `tests/test_parity.py` and `hooks/pace.test.ts`
run the same cases, so the two implementations cannot drift.

## Limitations

- The hold and the delay stop **tool calls**, not token spend generally. The
  model can still reply in prose during a held turn.
- Rate limits arrive with the first API response of a session; until then the
  guard fails open.
- Borrowing is per machine: sessions on other machines sharing the account are
  not seen.

## License

MIT.
