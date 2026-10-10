#!/usr/bin/env python3
"""The usage guard under Codex: one hook for both of its events.

    codex-hook.py --event user_prompt_submit   one line of usage context per prompt
    codex-hook.py --event pre_tool_use         hold at the threshold, pace before it

Codex has no plugin system, so it runs the guard as command hooks (`ug codex
install` wires and trusts them). Each run refreshes `codex-usage.json` from the
live transcript the payload names, whose last token_count event carries the
rate limits the API returned, then decides from Codex's own windows. Codex
sessions have no priority: they run under the account terms.

The hold blocks this process rather than denying: a denial tells the model "no"
and it carries on burning tokens; a blocked hook stalls the loop until the
window rolls over. Every tick re-reads config and cache, so `ug off`, `ug
release` and a raised threshold take effect within one poll.
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


def emit(event: str, **fields) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, **fields}}))


def allow(context=None):
    """Exit 0 so the agent proceeds; with `context`, the model is told where usage stands."""
    clear_marker()
    if context:
        emit("PreToolUse", additionalContext=context)
    sys.exit(0)


def deny(reason):
    clear_marker()
    emit("PreToolUse", permissionDecision="deny", permissionDecisionReason=reason)
    sys.exit(0)


def clear_marker():
    try:
        g.codex_blocked_path().unlink()
    except OSError:
        pass


def write_marker(label, until, pct):
    try:
        g.atomic_write(g.codex_blocked_path(), {
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


def read_cache(cfg, now):
    return g.codex_limits(now, float(cfg["codex_log_max_age_seconds"]))


def refresh_cache(payload, now):
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


def brief():
    try:
        line = g.report(g.load_config())["brief"]
    except Exception:
        return 0
    if line:
        emit("UserPromptSubmit", additionalContext=line)
    return 0


def timing(cfg, start):
    try:
        return start + float(cfg["max_stall_seconds"]), max(0.05, float(cfg["poll_seconds"]))
    except (TypeError, ValueError):
        return start + g.DEFAULTS["max_stall_seconds"], g.DEFAULTS["poll_seconds"]


def guard():
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_: allow())
        except (OSError, ValueError):
            pass

    cfg = g.load_config()
    if not cfg["enabled"]:
        allow()
    now = time.time()
    cache = read_cache(cfg, now)
    if g.is_stale(cache, cfg, now):
        allow()  # fail open: no trustworthy data means no hold
    if not g.violations(cache, cfg, now):
        pace(cfg, cache, now)

    start = now
    deadline, poll = timing(cfg, start)
    while True:
        now = time.time()
        cfg = g.load_config()
        if not cfg["enabled"] or released_since(start):
            allow()
        cache = read_cache(cfg, now)
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
            deny(f"Usage guard: Codex {labels} (threshold {worst.threshold:.0f}%). "
                 f"Held for {g.fmt_duration(now - start)} and the window does not reset "
                 f"until {clock}, which exceeds the configured stall budget. "
                 f"Stop and wait, or run `ug off` to disable the guard.")
        time.sleep(min(poll, max(0.05, deadline - now), max(0.05, worst.resets_at - now)))


def pace_context(pace_list, cfg, waited):
    active = [p for p in pace_list if p.active]
    if not active:
        return None
    parts = [f"{p.label} window at {p.pct:.0f}% against a pace line of {p.line:.0f}% "
             f"(+{p.ahead:.0f}, margin {p.margin:.0f})" for p in active]
    worst = max(active, key=lambda p: p.ahead - p.margin)
    back = g.fmt_duration(worst.catchup_at - time.time())
    how = (f"each tool call is being delayed {g.pace_delay(active):.0f}s" if cfg["pace_mode"] == "delay"
           else f"this call was held {g.fmt_duration(waited)}")
    return (f"Usage guard pacing: {'; '.join(parts)}. Spend is running ahead of the window, so {how}; "
            f"back on pace in about {back} at the current rate. Keep working through the delays: prefer fewer, "
            f"larger steps, batch reads, and defer fan-outs and long loops until then. Pacing is never a reason to "
            f"stop, pause or ask the user to continue. `ug status --json` has the numbers.")


def pace(cfg, cache, now):
    """Delay or hold this call while usage runs ahead of a pace line, then allow it."""
    current = g.paces(cache, cfg, now)
    if not any(p.active for p in current):
        allow()
    entered = current  # what engaged pacing, for the context the model gets afterwards
    start = now
    deadline, poll = timing(cfg, start)

    if cfg["pace_mode"] == "delay":
        until = start + g.pace_delay(current)
        while time.time() < until:
            cfg = g.load_config()
            if not cfg["enabled"] or not cfg["pace_enabled"] or released_since(start):
                allow()
            time.sleep(min(poll, max(0.05, until - time.time())))
        allow(pace_context(current, cfg, time.time() - start))

    while True:  # hold mode: wait until no window is pacing, bounded by the stall budget
        now = time.time()
        cfg = g.load_config()
        if not cfg["enabled"] or not cfg["pace_enabled"] or released_since(start):
            allow()
        cache = read_cache(cfg, now)
        if g.is_stale(cache, cfg, now):
            allow()
        current = g.paces(cache, cfg, now)
        active = [p for p in current if p.active]
        if not active or now >= deadline:
            allow(pace_context(entered, cfg, now - start))
        worst = max(active, key=lambda p: p.catchup_at)
        write_marker("pace " + "+".join(p.label for p in active), worst.catchup_at, worst.pct)
        time.sleep(min(poll, max(0.05, deadline - now), max(0.05, worst.catchup_at - now)))


def main(argv):
    if "--event" not in argv or argv[argv.index("--event") + 1] not in ("user_prompt_submit", "pre_tool_use"):
        print("usage: codex-hook.py --event <user_prompt_submit|pre_tool_use>", file=sys.stderr)
        return 2
    event = argv[argv.index("--event") + 1]
    # Drain stdin so Codex never sees a broken pipe; the payload names the transcript.
    payload = None
    try:
        payload = json.loads(sys.stdin.read() or "null")
    except (OSError, ValueError):
        pass
    refresh_cache(payload, time.time())
    if event == "user_prompt_submit":
        return brief()
    guard()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
