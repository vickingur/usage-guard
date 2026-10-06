#!/usr/bin/env python3
"""Claude Code statusline: cwd, branch, context window, 5h/7d usage and pace.

Doubles as the data source for the usage guard: `rate_limits` is only ever
handed to the statusline, so whatever it sees is cached for the hooks. Each
window shows how far ahead of its pace line it runs (`+12`), highlighted when
pacing is engaged, and Codex usage appears from its newest session log.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True  # keep the shared layer free of __pycache__
sys.path.insert(0, str(Path(__file__).resolve().parent))
import guardlib as g  # noqa: E402

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
BLUE = "\033[34m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"
SEP = f"{DIM} · {RESET}"


def paint(text, color):
    return f"{color}{text}{RESET}"


def pct_color(pct, threshold):
    """Green below two-thirds of the threshold, amber approaching it, red at it."""
    if pct >= threshold:
        return BOLD + RED
    if pct >= threshold * 0.66:
        return YELLOW
    return GREEN


def short_path(raw):
    if not raw:
        return "?"
    path = Path(raw)
    try:
        path = Path("~") / path.relative_to(Path.home())
    except ValueError:
        pass
    parts = path.parts
    if len(parts) > 4:
        return str(Path(parts[0], "…", *parts[-2:]))
    return str(path)


def git_state(cwd):
    """Branch name plus a '*' when the worktree is dirty. Silent on any failure."""
    try:
        # --show-current (unlike rev-parse) still names an unborn branch in a
        # freshly-initialised repo; it returns empty only for a detached HEAD.
        branch = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"],
            capture_output=True, text=True, timeout=1.0,
        )
        if branch.returncode != 0:
            return None
        name = branch.stdout.strip()
        if not name:
            detached = subprocess.run(
                ["git", "-C", cwd, "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=1.0,
            )
            if detached.returncode != 0 or not detached.stdout.strip():
                return None
            name = f"@{detached.stdout.strip()}"
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        dirty = subprocess.run(
            ["git", "-C", cwd, "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, timeout=1.0,
        )
        if dirty.returncode == 0 and dirty.stdout.strip():
            name += "*"
    except (OSError, subprocess.SubprocessError):
        pass
    return name


def cache_rate_limits(data, now):
    """Persist rate limits for the hook.

    Merged, not replaced. `rate_limits` is rebuilt per render and a window can
    drop out of it transiently; replacing wholesale would blind the guard to a
    window that is still live. A carried-over window is kept only until its own
    reset time passes, after which it is no longer limiting anything.
    """
    limits = data.get("rate_limits")
    if not isinstance(limits, dict):
        return
    entry = {"ts": now}

    previous = g.read_json(g.cache_path())
    if isinstance(previous, dict):
        for key, _cfg_key, _label in g.WINDOWS:
            stale = previous.get(key)
            if not isinstance(stale, dict):
                continue
            try:
                if float(stale["resets_at"]) > now:
                    entry[key] = stale
            except (KeyError, TypeError, ValueError):
                continue

    for key, _cfg_key, _label in g.WINDOWS:
        window = limits.get(key)
        if isinstance(window, dict) and "used_percentage" in window:
            entry[key] = {
                "used_percentage": window.get("used_percentage"),
                "resets_at": window.get("resets_at"),
            }

    if len(entry) > 1:
        try:
            g.atomic_write(g.cache_path(), entry)
        except OSError:
            pass


def pace_mark(p):
    """` +12` after a window: dim when within the margin, bold amber when pacing."""
    if p is None or p.ahead < 0.5:
        return ""
    text = f"+{round(p.ahead)}"
    if p.active:
        return " " + paint(text + "▲", BOLD + YELLOW)
    return " " + paint(text, DIM)


def codex_segment(cfg, now):
    """`cx 7d 2%` from the newest Codex session log, or None without one."""
    try:
        cache = g.codex_limits(now, float(cfg["codex_log_max_age_seconds"]))
    except Exception:
        return None
    if not cache:
        return None
    chunks = []
    pace_by_key = {p.key: p for p in g.paces(cache, cfg, now)}
    for key, cfg_key, label in g.WINDOWS:
        window = cache.get(key)
        if not isinstance(window, dict) or not isinstance(window.get("used_percentage"), (int, float)):
            continue
        pct = window["used_percentage"]
        chunk = f"{DIM}{label}{RESET} " + paint(f"{round(pct)}%", pct_color(pct, float(cfg[cfg_key])))
        p = pace_by_key.get(key)
        if p is not None and p.ahead >= 0.5:
            chunk += " " + paint(f"+{round(p.ahead)}" + ("▲" if p.ahead > p.margin else ""),
                                 BOLD + YELLOW if p.ahead > p.margin else DIM)
        chunks.append(chunk)
    return f"{DIM}cx{RESET} " + " ".join(chunks) if chunks else None


def active_hold(now, vendor="claude"):
    """The live hold marker written by the guard hook, or None if absent/expired."""
    marker = g.read_json(g.blocked_path(vendor))
    if not isinstance(marker, dict):
        return None
    try:
        until = float(marker["until"])
    except (KeyError, TypeError, ValueError):
        return None
    if until <= now:
        return None
    return marker, until


def build(data, cfg, now):
    segments = []

    workspace = data.get("workspace") or {}
    cwd = workspace.get("current_dir") or data.get("cwd") or ""
    segments.append(paint(short_path(cwd), BOLD + CYAN))

    branch = git_state(cwd) if cwd else None
    if branch:
        segments.append(paint(branch, MAGENTA))

    model = data.get("model") or {}
    name = model.get("display_name") or model.get("id")
    if name:
        segments.append(paint(name, BLUE))

    ctx = data.get("context_window") or {}
    used = ctx.get("used_percentage")
    if isinstance(used, (int, float)):
        segments.append(f"{DIM}ctx{RESET} " + paint(f"{round(used)}%", pct_color(used, 90)))

    limits = data.get("rate_limits") or {}
    pace_by_key = {p.key: p for p in g.paces(limits, cfg, now)}
    for key, cfg_key, label in g.WINDOWS:
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        pct = window.get("used_percentage")
        if not isinstance(pct, (int, float)):
            continue
        chunk = f"{DIM}{label}{RESET} " + paint(f"{round(pct)}%", pct_color(pct, float(cfg[cfg_key])))
        chunk += pace_mark(pace_by_key.get(key))
        resets_at = window.get("resets_at")
        if isinstance(resets_at, (int, float)) and resets_at > now:
            chunk += f" {DIM}{g.fmt_duration(resets_at - now)}{RESET}"
        segments.append(chunk)

    codex = codex_segment(cfg, now)
    if codex:
        segments.append(codex)

    held = False
    for vendor in g.VENDORS:
        hold = active_hold(now, vendor)
        if not hold:
            continue
        held = True
        marker, until = hold
        label = marker.get("label") or "usage"
        if vendor != "claude":
            label = f"{vendor} {label}"
        clock = time.strftime("%H:%M", time.localtime(until))
        colour = BOLD + ("\033[43m\033[30m" if "pace" in str(label) else "\033[41m\033[97m")
        segments.append(paint(f" HOLD {label} until {clock} ({g.fmt_duration(until - now)}) ", colour))
    if held:
        pass
    elif not cfg["enabled"]:
        segments.append(paint("guard off", DIM + YELLOW))
    elif not cfg["pace_enabled"]:
        segments.append(paint("pace off", DIM + YELLOW))

    return SEP.join(segments)


def main():
    now = time.time()
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            data = {}
    except (ValueError, OSError):
        data = {}

    try:
        cfg = g.load_config()
    except Exception:
        cfg = dict(g.DEFAULTS)

    try:
        cache_rate_limits(data, now)
    except Exception:
        pass

    try:
        print(build(data, cfg, now))
    except Exception:
        print(paint("statusline error", DIM + RED))
    return 0


if __name__ == "__main__":
    sys.exit(main())
