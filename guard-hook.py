#!/usr/bin/env python3
"""PreToolUse hook: hold tool calls at a usage threshold, pace them before it.

Threshold: this holds rather than denies. A denial just tells the model "no"
and it carries on burning tokens; blocking the hook's process stalls the agentic
loop outright until the window rolls over. The hold re-reads config and cache
every tick, so `ug off`, `ug release` and a raised threshold all take effect
within one poll.

Vendors: Claude Code runs this with no arguments and the usage comes from the
statusline's cache. Codex runs it with `--vendor codex`; the payload names the
live transcript, whose last token_count event carries the rate limits, so the
hook refreshes the Codex cache itself before deciding.

Pacing: when usage runs ahead of a window's pace line by more than its margin,
each tool call is delayed in proportion ("delay" mode) or blocked until usage is
back on pace ("hold" mode). Either way the call then proceeds, and the hook
tells the model where it stands through `additionalContext`, so it can choose
fewer, larger steps instead of being surprised by a slow loop.
"""
from __future__ import annotations

import json
import signal
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True  # keep the shared layer free of __pycache__
sys.path.insert(0, str(Path(__file__).resolve().parent))
import guardlib as g  # noqa: E402


VENDOR = "claude"


def allow(context=None):
    """Exit 0 so the agent proceeds with its normal permission flow.

    With `context`, the model is told where usage stands; without it the hook
    says nothing at all.
    """
    clear_marker()
    if context:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": context,
        }}))
    sys.exit(0)


def deny(reason):
    clear_marker()
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def clear_marker():
    try:
        g.blocked_path(VENDOR).unlink()
    except OSError:
        pass


def write_marker(label, until, pct):
    try:
        g.atomic_write(g.blocked_path(VENDOR), {
            "until": int(until), "label": label, "pct": pct, "since": int(time.time()),
        })
    except OSError:
        pass


def released_since(start):
    """True when `ug release` was invoked after this hold began."""
    state = g.read_json(g.state_path())
    if not isinstance(state, dict):
        return False
    try:
        return float(state.get("release_at", 0)) > start
    except (TypeError, ValueError):
        return False


def refresh_codex_cache(payload, now):
    """Write the Codex cache from the running session's transcript, if named."""
    path = payload.get("transcript_path") if isinstance(payload, dict) else None
    if not path:
        return
    entry = g.limits_from_codex_log(Path(path), now)
    if entry:
        try:
            g.atomic_write(g.cache_path("codex"), entry)
        except OSError:
            pass


def main():
    global VENDOR
    if "--vendor" in sys.argv:
        VENDOR = sys.argv[sys.argv.index("--vendor") + 1]
    # Drain stdin so the agent never sees a broken pipe. Claude's payload is
    # unused; Codex's names the transcript the cache is refreshed from.
    payload = None
    try:
        payload = json.loads(sys.stdin.read() or "null")
    except (OSError, ValueError):
        pass
    if VENDOR == "codex":
        refresh_codex_cache(payload, time.time())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_: allow())
        except (OSError, ValueError):
            pass

    cfg = g.load_config()
    if not cfg["enabled"]:
        allow()

    now = time.time()
    cache = g.read_json(g.cache_path(VENDOR))
    if g.is_stale(cache, cfg, now):
        allow()  # fail open: no trustworthy data means no hold

    current = g.violations(cache, cfg, now)
    if not current:
        pace(cfg, cache, now)

    start = now
    try:
        deadline = start + float(cfg["max_stall_seconds"])
        poll = max(0.05, float(cfg["poll_seconds"]))
    except (TypeError, ValueError):
        deadline = start + g.DEFAULTS["max_stall_seconds"]
        poll = g.DEFAULTS["poll_seconds"]

    while True:
        now = time.time()
        cfg = g.load_config()
        if not cfg["enabled"] or released_since(start):
            allow()

        cache = g.read_json(g.cache_path(VENDOR))
        if g.is_stale(cache, cfg, now):
            allow()

        current = g.violations(cache, cfg, now)
        if not current:
            allow()  # threshold raised, usage dropped, or every window has reset

        worst = max(current, key=lambda v: v.resets_at)
        write_marker("+".join(v.label for v in current), worst.resets_at, worst.pct)

        if now >= deadline:
            labels = ", ".join(f"{v.label} at {v.pct:.0f}%" for v in current)
            clock = time.strftime("%Y-%m-%d %H:%M", time.localtime(worst.resets_at))
            deny(
                f"Usage guard: {labels} (threshold {worst.threshold:.0f}%). "
                f"Held for {g.fmt_duration(now - start)} and the window does not reset "
                f"until {clock}, which exceeds the configured stall budget. "
                f"Stop and wait, or run `ug off` to disable the guard."
                if VENDOR == "claude" else
                f"Usage guard: Codex {labels} (threshold {worst.threshold:.0f}%). "
                f"Held for {g.fmt_duration(now - start)} and the window does not reset "
                f"until {clock}, which exceeds the configured stall budget. "
                f"Stop and wait, or run `ug off` to disable the guard."
            )

        time.sleep(min(poll, max(0.05, deadline - now), max(0.05, worst.resets_at - now)))


def pace_context(pace_list, cfg, waited):
    active = [p for p in pace_list if p.active]
    if not active:
        return None
    parts = []
    for p in active:
        parts.append(f"{p.label} window at {p.pct:.0f}% against a pace line of {p.line:.0f}% "
                     f"(+{p.ahead:.0f}, margin {p.margin:.0f})")
    worst = max(active, key=lambda p: p.ahead - p.margin)
    back = g.fmt_duration(worst.catchup_at - time.time())
    how = (f"each tool call is being delayed {g.pace_delay(active):.0f}s" if cfg["pace_mode"] == "delay"
           else f"this call was held {g.fmt_duration(waited)}")
    return (f"Usage guard pacing: {'; '.join(parts)}. Spend is running ahead of the window, so {how}; "
            f"back on pace in about {back} at the current rate. Prefer fewer, larger steps, batch reads, "
            f"and avoid fan-outs and long loops until then. `ug status --json` has the numbers.")


def pace(cfg, cache, now):
    """Delay or hold this call while usage runs ahead of a pace line, then allow it."""
    current = g.paces(cache, cfg, now)
    if not any(p.active for p in current):
        allow()
    entered = current  # what engaged pacing, for the context the model gets afterwards
    start = now
    try:
        poll = max(0.05, float(cfg["poll_seconds"]))
        deadline = start + float(cfg["max_stall_seconds"])
    except (TypeError, ValueError):
        poll = g.DEFAULTS["poll_seconds"]
        deadline = start + g.DEFAULTS["max_stall_seconds"]

    if cfg["pace_mode"] == "delay":
        until = start + g.pace_delay(current)
        while time.time() < until:
            cfg = g.load_config()
            if not cfg["enabled"] or not cfg["pace_enabled"] or released_since(start):
                allow()
            time.sleep(min(poll, max(0.05, until - time.time())))
        allow(pace_context(current, cfg, time.time() - start))

    # hold mode: wait until no window is pacing, bounded by the stall budget
    while True:
        now = time.time()
        cfg = g.load_config()
        if not cfg["enabled"] or not cfg["pace_enabled"] or released_since(start):
            allow()
        cache = g.read_json(g.cache_path(VENDOR))
        if g.is_stale(cache, cfg, now):
            allow()
        current = g.paces(cache, cfg, now)
        active = [p for p in current if p.active]
        if not active or now >= deadline:
            allow(pace_context(entered, cfg, now - start))
        worst = max(active, key=lambda p: p.catchup_at)
        write_marker("pace " + "+".join(p.label for p in active), worst.catchup_at, worst.pct)
        time.sleep(min(poll, max(0.05, deadline - now), max(0.05, worst.catchup_at - now)))


if __name__ == "__main__":
    main()
