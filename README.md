# usage-guard

Statusline, hooks and a CLI that keep Claude Code inside its usage windows and
tell agents where they stand, for Claude and Codex alike. Pure Python 3.9+,
standard library only.

## Install

    git clone https://github.com/vickingur/usage-guard ~/.claude/usage-guard
    mkdir -p ~/.local/bin && ln -s ~/.claude/usage-guard/ug ~/.local/bin/ug

Then wire the statusline and the two hooks in `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 ~/.claude/usage-guard/statusline.py",
    "refreshInterval": 3
  },
  "hooks": {
    "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command",
      "command": "python3 ~/.claude/usage-guard/guard-hook.py", "timeout": 21700}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command",
      "command": "python3 ~/.claude/usage-guard/brief-hook.py", "timeout": 10}]}]
  }
}
```

The hook `timeout` is in seconds and must stay above `max_stall_seconds` (see
below). `ug status` confirms everything is wired once Claude Code has rendered
the statusline at least once.

## Parts

| File | Role |
|---|---|
| `statusline.py` | Renders the statusline **and** caches `rate_limits` to `usage.json` |
| `guard-hook.py` | PreToolUse hook: holds tool calls at a threshold, paces them before it (`--vendor codex` under Codex) |
| `brief-hook.py` | UserPromptSubmit hook: one line of usage context at every prompt (`--vendor codex` under Codex) |
| `ug` | Control CLI (`~/.local/bin/ug`), including `ug codex install` |
| `guardlib.py` | Shared paths, config, pace math, Codex reader, report |

## Why the statusline feeds the hooks

Claude Code passes `rate_limits` (the 5h and 7d percentages) **only** to the
statusline command. Hooks never receive it. So the statusline writes what it
sees to `usage.json`, and the hooks read that back. The statusline re-runs every
3 seconds (`refreshInterval`), so the cache stays fresh while you are active.

If the cache is missing or older than `stale_after_seconds`, the hooks **fail
open**: no data means no hold and no pacing.

The cache is **merged, not replaced**. `rate_limits` is rebuilt per render and a
window can drop out of it transiently; replacing wholesale would blind the guard
to a window that is still active. A carried-over window is kept only until its
own `resets_at` passes.

## Codex

Codex CLI (0.159 and later) runs the same two hooks, so Codex sessions get the
same threshold hold, pacing and `[usage]` line. Install with

    ug codex install

which adds the guard's `UserPromptSubmit` and `PreToolUse` entries to
`~/.codex/hooks.json` and records their trust hashes in `~/.codex/config.toml`.
Codex only runs hooks it has been told to trust, normally through a review in
its TUI; the hash is a SHA-256 over the normalized hook definition, so the
install step computes it and stands in for that review for these two hooks
only. Other hooks and trust entries are left untouched. `ug codex trusted`
exits 0 when everything is in place.

Codex has no statusline, but every turn's `token_count` event in its session
log carries the rate limits the API returned, and the hooks receive the live
transcript's path. Each hook refreshes `codex-usage.json` from it before
deciding, so Codex pacing works from Codex's own numbers; the `[usage]` line a
Codex agent receives covers both vendors. Without the hooks, the newest log
under `~/.codex/sessions` stands in for reporting, and `ug status` says which
source it is reading. A `window_minutes` of 300 maps to 5h and 10080 to 7d; a
window that has reset since the reading is dropped.

## Two mechanisms

### Threshold hold

A window at or over its threshold stops tool calls until it resets. The hook
*blocks its own process* rather than denying: a `deny` tells the model "no" and
it keeps going and keeps burning tokens; a blocked hook stalls the agentic loop
outright. Each tick (default 5s) it re-reads config and cache, so it releases as
soon as any of these becomes true:

- the window's `resets_at` passes
- usage drops below the threshold
- the threshold is raised above current usage
- `ug off`
- `ug release`

While held, the statusline shows a red `HOLD 5h until 14:00 (41m)` badge.

`max_stall_seconds` (default 21600 = 6h) caps a single hold. The settings.json
hook `timeout` is 21700, just above it, so the hook's own fallback fires before
Claude Code times the hook out and discards its decision. When the budget is
exhausted, realistic for a 7-day window that resets days away, the hook falls
back to a `deny` that names the window, its reset time and how to override.

### Pacing

The threshold only bites at the end. Pacing acts earlier so the budget lasts
until the reset.

Each window has a **pace line**: the usage you would have right now if spend
were spread evenly across the window and landed exactly on the threshold at
reset. Two hours into a 5h window with a 95% threshold the line is 38%. The
window is **ahead** by `used - line`. When ahead exceeds the window's **margin**
(default 20 points for 5h, 5 for 7d) and usage is at least `pace_min_used_pct`
(default 30%, so a burst right after a reset is left alone), pacing engages:

- `delay` mode (default): every tool call sleeps `pace_seconds_per_pct` seconds
  per point over the margin, capped at `pace_max_delay_seconds`. 12 points over
  at the defaults is 90s per call.
- `hold` mode: the call waits until the line has caught up to `used - margin`,
  bounded by `max_stall_seconds`. A pace hold ends in an allow, never a deny.

Either way the call then proceeds and the model receives `additionalContext`
saying which window is ahead, by how much, what is being done about it and when
it will be back on pace, with the advice to prefer fewer, larger steps and avoid
fan-outs and long loops. The statusline marks each window with `+12` (dim) when
ahead within the margin and `+32▲` (amber) when pacing; a pace hold shows as an
amber `HOLD pace 5h until …` badge.

Switching pacing off, disabling the guard, or `ug release` ends a delay or a
pace hold within one poll tick.

## Telling agents where they are

- `brief-hook.py` runs on every prompt and adds one line, for example
  `[usage] claude 5h 62% (+17 over pace line) PACING 40s/call, 7d 71% (+11 over pace line) · codex 7d 2%`,
  with the advice appended whenever anything is ahead of pace. Silent without data.
- `ug status --json` is the machine-readable form, for scripts, skills and Codex
  agents: per vendor and window, used%, threshold, pace line, ahead, margin,
  pacing state, delay, catch-up and reset times, plus the same brief line.
- `ug brief` prints the one line on its own.

## Commands

    ug status [--json]     guard state, both vendors, pace lines, any hold
    ug brief               the one-line position agents receive at every prompt
    ug on | off            enable / disable the guard
    ug threshold           show both thresholds
    ug threshold 5h 95     set the 5-hour threshold
    ug threshold 7d 90     set the weekly threshold
    ug pace                show pacing settings
    ug pace on | off       enable / disable pacing (the threshold hold stays)
    ug pace mode delay     or hold
    ug pace margin 7d 8    how far ahead of the pace line the window may run
    ug pace set KEY VALUE  pace_min_used_pct, pace_seconds_per_pct, pace_max_delay_seconds
    ug codex install       add the guard's hooks to Codex and trust them
    ug codex trusted       exit 0 when Codex has the hooks and trusts them
    ug release             release an in-progress hold now
    ug config              effective settings as JSON

## Config: `config.json` (node-local, not synced)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master switch |
| `threshold_5h` | `95.0` | Hold when the 5h window reaches this percent |
| `threshold_7d` | `90.0` | Hold when the 7d window reaches this percent |
| `max_stall_seconds` | `21600` | Cap on a single hold before falling back to deny (threshold) or allow (pace) |
| `poll_seconds` | `5` | How often a hold or delay re-checks for release |
| `stale_after_seconds` | `600` | Cache older than this is ignored (fails open) |
| `pace_enabled` | `true` | Pacing on or off |
| `pace_mode` | `"delay"` | `delay` or `hold` |
| `pace_margin_5h` | `20.0` | Points ahead of the 5h pace line tolerated before pacing |
| `pace_margin_7d` | `5.0` | Same for the weekly window |
| `pace_min_used_pct` | `30.0` | Pacing never engages below this usage |
| `pace_seconds_per_pct` | `10.0` | Delay per point over the margin |
| `pace_max_delay_seconds` | `90.0` | Cap on the per-call delay |
| `codex_log_max_age_seconds` | `604800` | Oldest Codex session log that still counts |

The file only needs the keys you want to override; the rest fall back to
defaults. A corrupt file falls back to defaults entirely. Runtime state
(`usage.json`, `codex-usage.json`, `state.json`, `blocked.json`, `codex-blocked.json`)
is node-local too.

## Limitations

- The hold and the delay stop **tool calls**, not token spend generally. The
  model can still reply in prose during a held turn.
- `rate_limits` is absent until Claude Code has seen a limit from the API,
  typically after the first turn of a session. Until then the guard fails open.
- Without the Codex hooks, Codex numbers are as fresh as its newest session log;
  `ug status` shows the age and the source.
- Overhead is about 25ms per tool call when not pacing, and about 45ms per prompt
  for the brief.

## Tests

    python3 -B -m unittest discover -s ~/.claude/usage-guard/tests

Over 100 tests, no dependencies, Python 3.9 upwards. They use `UG_DIR` and
`UG_CODEX_SESSIONS` to redirect all state to a temp directory, so running them
never touches live config or triggers a real hold.

## License

MIT.
