"""Shared state and evaluation logic for the usage guard's Python side.

The Claude Code mod (hooks/register.tsx) is the guard for Claude: it reads the
engine's rate limits directly, holds and paces tool calls in-process, and writes
what it sees under the guard's directory. This module is what the `ug` CLI and
the Codex hooks share: the config table, the pace math (mirrored from
hooks/pace.ts and pinned to it by tests/test_parity.py), the Codex session-log
reader, the session registry reader and the report.

Files under the guard's directory (UG_DIR, else ~/.claude/usage-guard):

    config.json          settings (ug writes, the mod and the hooks read)
    usage.json           the account's Claude windows, as the mod last saw them
    codex-usage.json     Codex's windows, written by the Codex hook
    codex-blocked.json   Codex's live hold marker
    state.json           `release_at`, written by `ug release`
    sessions/<id>.json   one entry per live Claude session: priority, last call, hold
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent


def _table(name: str, marker: str) -> dict:
    """A JSON table kept in a TypeScript module as `export const X = <json>`."""
    text = (HERE / "hooks" / name).read_text()
    return json.loads(text.split(marker, 1)[1])


DEFAULTS = _table("defaults.ts", "export const DEFAULTS =")

PACE_MODES = ("delay", "hold")
PRIORITIES = ("low", "normal", "high")

# (key in the cache, config key holding its threshold, display label, config key of its margin, length)
WINDOWS = (
    ("five_hour", "threshold_5h", "5h", "pace_margin_5h", 5 * 3600),
    ("seven_day", "threshold_7d", "7d", "pace_margin_7d", 7 * 86400),
)
# Codex reports `window_minutes`; map them onto the same two windows.
CODEX_WINDOW_MINUTES = {300: "five_hour", 10080: "seven_day"}


@dataclass(frozen=True)
class Violation:
    label: str
    pct: float
    resets_at: int
    threshold: float


@dataclass(frozen=True)
class Terms:
    """What a session gets: a fraction of the window margin, a stretch on its delays,
    and how far it has risen above its own class by borrowing (0 = own terms)."""

    margin_factor: float
    delay_factor: float
    lift: float


ACCOUNT_TERMS = Terms(1.0, 1.0, 0.0)


@dataclass(frozen=True)
class Pace:
    """Where one window stands against its pace line."""

    key: str
    label: str
    pct: float
    resets_at: int
    threshold: float
    line: float        # usage the pace line allows right now
    ahead: float       # pct - line; positive means spending faster than the line
    margin: float      # how far ahead is tolerated under the terms in force
    active: bool       # pacing is engaged for this window
    delay_seconds: float   # per-tool-call delay in "delay" mode (0 when inactive)
    catchup_at: int    # when the line reaches pct - margin, i.e. when pacing would release
    line_rate: float = 0.0   # points per second the line climbs right now
    hold_at: float = 0.0     # the level the hold engages at right now


# --- paths -------------------------------------------------------------------

def base_dir() -> Path:
    return Path(os.environ.get("UG_DIR") or (Path.home() / ".claude" / "usage-guard"))


def config_path() -> Path:
    return base_dir() / "config.json"


def cache_path(vendor: str = "claude") -> Path:
    """Live usage cache: written by the mod (claude) or the Codex hook (codex)."""
    return base_dir() / ("usage.json" if vendor == "claude" else f"{vendor}-usage.json")


def state_path() -> Path:
    return base_dir() / "state.json"


def codex_blocked_path() -> Path:
    return base_dir() / "codex-blocked.json"


def sessions_dir() -> Path:
    return base_dir() / "sessions"


def codex_sessions_dir() -> Path:
    env = os.environ.get("UG_CODEX_SESSIONS")
    if env:
        return Path(env)
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "sessions"


def read_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def atomic_write(path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ug-tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- config --------------------------------------------------------------------

def parse_config(stored) -> dict:
    """`stored` over the defaults: a key the table lacks, or a value of another
    type than its default, is left at the default (the mod reads it the same way)."""
    cfg = dict(DEFAULTS)
    if isinstance(stored, dict):
        for key, value in stored.items():
            if key in cfg and _same_type(value, cfg[key]):
                cfg[key] = value
    if cfg["pace_mode"] not in PACE_MODES:
        cfg["pace_mode"] = DEFAULTS["pace_mode"]
    return cfg


def _same_type(value, default) -> bool:
    if isinstance(default, list):
        return (isinstance(value, list) and len(value) == len(default)
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 for v in value))
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, type(default))


def load_config() -> dict:
    return parse_config(read_json(config_path(), {}))


def save_config(updates: dict) -> dict:
    stored = read_json(config_path(), {})
    if not isinstance(stored, dict):
        stored = {}
    stored.update(updates)
    atomic_write(config_path(), stored)
    return load_config()


# --- pace math (mirrors hooks/pace.ts) --------------------------------------------

def is_stale(cache, cfg, now: float) -> bool:
    if not isinstance(cache, dict) or "ts" not in cache:
        return True
    try:
        return (now - float(cache["ts"])) > float(cfg["stale_after_seconds"])
    except (TypeError, ValueError):
        return True


def _reading(cache, key):
    window = cache.get(key) if isinstance(cache, dict) else None
    if not isinstance(window, dict):
        return None
    try:
        return float(window["used_percentage"]), int(window["resets_at"])
    except (KeyError, TypeError, ValueError):
        return None


def hold_level(cfg, key: str, resets_at: float, now: float) -> float:
    """The level the hold engages at right now. The weekly threshold keeps a
    reserve all week and releases it over the last `threshold_7d_release_hours`,
    climbing linearly to 100% at the reset. The pace line still aims at the base."""
    base = float(cfg["threshold_7d" if key == "seven_day" else "threshold_5h"])
    release = float(cfg["threshold_7d_release_hours"]) * 3600
    if key != "seven_day" or release <= 0:
        return base
    remaining = max(0.0, resets_at - now)
    if remaining >= release:
        return base
    return min(100.0, base + (100.0 - base) * (1.0 - remaining / release))


def violations(cache, cfg, now: float) -> list:
    """Windows at or over their hold level whose reset time is still in the future."""
    found = []
    for key, cfg_key, label, _margin_key, _length in WINDOWS:
        reading = _reading(cache, key)
        if reading is None:
            continue
        pct, resets_at = reading
        threshold = hold_level(cfg, key, resets_at, now)
        if pct >= threshold and resets_at > now:
            found.append(Violation(label, pct, resets_at, threshold))
    return found


@dataclass(frozen=True)
class Profile:
    """A weekly spending profile: a weight per day of the week (Monday first) and
    per hour of the day, in local time; `offset_minutes` is the local offset from
    UTC, east positive. Uniform weights give the even-spend line."""

    days: tuple
    hours: tuple
    offset_minutes: int = 0

    def is_uniform(self) -> bool:
        return len(set(self.days)) == 1 and len(set(self.hours)) == 1

    def weight_at(self, t: float) -> float:
        local = t + self.offset_minutes * 60
        day = (int(local // 86400) + 3) % 7   # 1970-01-01 was a Thursday; Monday is 0
        hour = int((local % 86400) // 3600)
        return self.days[day] * self.hours[hour]

    def weight_between(self, start: float, end: float) -> float:
        total, t = 0.0, start
        while t < end:
            local = t + self.offset_minutes * 60
            stop = min(t + (3600 - (local % 3600)), end)
            total += self.weight_at(t) * (stop - t)
            t = stop
        return total


UNIFORM = Profile((1.0,) * 7, (1.0,) * 24)


def profile_of(cfg, offset_minutes: Optional[int] = None) -> Profile:
    """The configured weekly profile; the machine's local offset unless given."""
    if offset_minutes is None:
        offset_minutes = int(time.localtime().tm_gmtoff // 60)
    return Profile(tuple(float(v) for v in cfg["pace_profile_days"]), tuple(float(v) for v in cfg["pace_profile_hours"]), offset_minutes)


def pace_line(threshold: float, resets_at: float, window_seconds: float, now: float, profile: Profile = UNIFORM) -> float:
    """Usage the pace line allows at `now`: threshold scaled by the share of the
    window's spending profile that has elapsed (the elapsed fraction when uniform)."""
    start = resets_at - window_seconds
    at = min(max(now, start), resets_at)
    if profile.is_uniform():
        return threshold * ((at - start) / window_seconds)
    whole = profile.weight_between(start, resets_at)
    if whole <= 0:
        return threshold * ((at - start) / window_seconds)
    return threshold * (profile.weight_between(start, at) / whole)


def line_rate_at(threshold: float, resets_at: float, window_seconds: float, now: float, profile: Profile = UNIFORM) -> float:
    """Points per second the line climbs at `now`."""
    if profile.is_uniform():
        return threshold / window_seconds
    whole = profile.weight_between(resets_at - window_seconds, resets_at)
    return threshold / window_seconds if whole <= 0 else threshold * profile.weight_at(now) / whole


def line_reaches(threshold: float, resets_at: float, window_seconds: float, now: float, level: float, profile: Profile = UNIFORM) -> int:
    """The first time at or after `now` when the line reaches `level`, at most `resets_at`."""
    if threshold <= 0 or level >= threshold:
        return int(resets_at)
    if profile.is_uniform():
        t = int(resets_at - window_seconds * (1.0 - level / threshold))
        return max(int(now), min(t, int(resets_at)))
    t = max(now, resets_at - window_seconds)
    while t < resets_at:
        local = t + profile.offset_minutes * 60
        end = min(t + (3600 - (local % 3600)), resets_at)
        line_end = pace_line(threshold, resets_at, window_seconds, end, profile)
        if line_end >= level:
            line_start = pace_line(threshold, resets_at, window_seconds, t, profile)
            f = (level - line_start) / (line_end - line_start) if line_end > line_start else 1.0
            return max(int(now), int(t + f * (end - t)))
        t = end
    return int(resets_at)


def ramp(idle_seconds: float, after: float, full: float) -> float:
    """0 before `after` seconds idle, 1 from `full`, linear between."""
    if full <= after:
        return 1.0 if idle_seconds >= after else 0.0
    return min(1.0, max(0.0, (idle_seconds - after) / (full - after)))


def _own_terms(priority: str, cfg) -> tuple:
    if priority == "high":
        return 1.0, 1.0
    if priority == "normal":
        return float(cfg["priority_margin_factor_normal"]), float(cfg["priority_delay_factor_normal"])
    return float(cfg["priority_margin_factor_low"]), float(cfg["priority_delay_factor_low"])


def terms(priority: str, idle_above: dict, cfg) -> Terms:
    """The terms a session of `priority` runs under. `idle_above[class]` is how
    long ago any other session of that class last made a tool call; a class with
    no session is absent and counts as idle forever. A session borrows the next
    class's terms progressively while that class is idle, and only once it has
    them whole does it start on the class above."""
    margin_factor, delay_factor = _own_terms(priority, cfg)
    lift = 0.0
    for above in PRIORITIES[PRIORITIES.index(priority) + 1:]:
        idle = idle_above.get(above, float("inf"))
        f = ramp(idle, float(cfg["borrow_after_seconds"]), float(cfg["borrow_full_seconds"]))
        target_margin, target_delay = _own_terms(above, cfg)
        margin_factor += f * (target_margin - margin_factor)
        delay_factor += f * (target_delay - delay_factor)
        lift += f
        if f < 1.0:
            break
    return Terms(margin_factor, delay_factor, lift)


def paces(cache, cfg, now: float, t: Terms = ACCOUNT_TERMS, profile: Profile = UNIFORM) -> list:
    """One Pace per window present in the cache, whether or not pacing is engaged.
    The weekly window follows `profile`; the 5h window is always even."""
    out = []
    enabled = bool(cfg["pace_enabled"])
    min_used = float(cfg["pace_min_used_pct"])
    per_pct = float(cfg["pace_seconds_per_pct"])
    max_delay = float(cfg["pace_max_delay_seconds"])
    for key, cfg_key, label, margin_key, length in WINDOWS:
        reading = _reading(cache, key)
        if reading is None:
            continue
        pct, resets_at = reading
        threshold = float(cfg[cfg_key])
        margin = float(cfg[margin_key]) * t.margin_factor
        shape = profile if key == "seven_day" else UNIFORM
        line = pace_line(threshold, resets_at, length, now, shape)
        ahead = pct - line
        over = ahead - margin
        active = enabled and resets_at > now and pct >= min_used and over > 0
        delay = min(max_delay * t.delay_factor, over * per_pct * t.delay_factor) if active else 0.0
        catchup = line_reaches(threshold, resets_at, length, now, pct - margin, shape)
        out.append(Pace(key, label, pct, resets_at, threshold, line, ahead, margin, active, delay, catchup,
                        line_rate_at(threshold, resets_at, length, now, shape), hold_level(cfg, key, resets_at, now)))
    return out


def pace_delay(pace_list) -> float:
    """The per-call delay pacing asks for: the worst window wins."""
    return max([p.delay_seconds for p in pace_list if p.active] or [0.0])


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "<1m"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours}h"


def _num(value, fallback):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


# --- the session registry (written by the mod) ----------------------------------

def sessions(cfg, now: float) -> list:
    """Every live Claude session's entry, as the mod wrote it; one not updated
    within `session_stale_seconds` is left out."""
    out = []
    try:
        names = sorted(p for p in sessions_dir().iterdir() if p.suffix == ".json")
    except OSError:
        return out
    for path in names:
        entry = read_json(path)
        if not isinstance(entry, dict) or entry.get("priority") not in PRIORITIES:
            continue
        try:
            if now - float(entry["updated"]) > float(cfg["session_stale_seconds"]):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        out.append(entry)
    return out


def set_session_priority(session_id: str, priority: str) -> bool:
    """Rewrites one session's priority in the registry; the mod picks it up on its next call."""
    path = sessions_dir() / f"{session_id}.json"
    entry = read_json(path)
    if not isinstance(entry, dict):
        return False
    entry["priority"] = priority
    atomic_write(path, entry)
    return True


def idle_above(entries, self_id: str, now: float) -> dict:
    """Seconds since another session of each class last made a tool call."""
    out = {}
    for e in entries:
        if e.get("id") == self_id:
            continue
        idle = max(0.0, now - _num(e.get("last_call"), 0))
        if e["priority"] not in out or idle < out[e["priority"]]:
            out[e["priority"]] = idle
    return out


# --- Codex -----------------------------------------------------------------

def _recent_codex_logs(root: Path, limit: int = 8) -> list:
    """The most recently modified session logs, newest first, as (path, mtime)."""
    found = []
    try:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                path = Path(dirpath) / name
                try:
                    found.append((path, path.stat().st_mtime))
                except OSError:
                    continue
    except OSError:
        return []
    found.sort(key=lambda item: item[1], reverse=True)
    return found[:limit]


def _last_rate_limits(path: Path, tail_bytes: int = 512 * 1024) -> Optional[dict]:
    """The `rate_limits` object of the last token_count event in a Codex log."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            fh.seek(max(0, size - tail_bytes))
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(chunk.splitlines()):
        if '"token_count"' not in line or '"rate_limits"' not in line:
            continue
        try:
            payload = json.loads(line).get("payload") or {}
        except ValueError:
            continue
        limits = payload.get("rate_limits")
        if isinstance(limits, dict):
            return limits
    return None


def limits_from_codex_log(path: Path, now: float) -> Optional[dict]:
    """Codex usage read from one session log, in the cache's own shape."""
    limits = _last_rate_limits(path)
    if not limits:
        return None
    entry = {"ts": now}
    for slot in ("primary", "secondary"):
        window = limits.get(slot)
        if not isinstance(window, dict):
            continue
        key = CODEX_WINDOW_MINUTES.get(window.get("window_minutes"))
        if key is None or "used_percent" not in window:
            continue
        if _num(window.get("resets_at"), 0) <= now:
            continue  # that window has rolled over since the log was written
        entry[key] = {"used_percentage": window.get("used_percent"), "resets_at": window.get("resets_at")}
    return entry if len(entry) > 1 else None


def codex_limits(now: float, max_age_seconds: float) -> Optional[dict]:
    """Codex usage: the hook's live cache when fresh, else the newest session log.

    The Codex hook writes `codex-usage.json` from the running session's own
    transcript on every prompt and tool call; that is authoritative while Codex
    is active. Without it (hook not installed, or no session for a while) the
    newest log under ~/.codex/sessions stands in. None when neither says anything.
    """
    cached = read_json(cache_path("codex"))
    if isinstance(cached, dict) and now - _num(cached.get("ts"), 0) <= max_age_seconds:
        live = {k: v for k, v in cached.items()
                if k == "ts" or (isinstance(v, dict) and _num(v.get("resets_at"), 0) > now)}
        if len(live) > 1:
            live["source"] = "hook"
            return live
    # A session that just started has no token_count yet, so walk back through
    # the recent logs until one carries limits.
    for path, mtime in _recent_codex_logs(codex_sessions_dir()):
        if now - mtime > max_age_seconds:
            return None
        entry = limits_from_codex_log(path, now)
        if entry:
            entry["ts"] = mtime
            entry["source"] = "log"
            return entry
    return None


# --- reporting -------------------------------------------------------------

def _window_rows(cache, cfg, now: float, t: Terms = ACCOUNT_TERMS) -> list:
    rows = []
    for p in paces(cache, cfg, now, t, profile_of(cfg)):
        rows.append({
            "window": p.label, "used_pct": round(p.pct, 1), "threshold_pct": p.threshold, "hold_at_pct": round(p.hold_at, 1),
            "resets_at": p.resets_at, "resets_in_seconds": max(0, p.resets_at - int(now)),
            "pace_line_pct": round(p.line, 1), "ahead_pct": round(p.ahead, 1),
            "margin_pct": round(p.margin, 1), "pacing": p.active,
            "delay_seconds": round(p.delay_seconds, 1),
            "catchup_in_seconds": max(0, p.catchup_at - int(now)) if p.active else 0,
        })
    return rows


def report(cfg, now: Optional[float] = None) -> dict:
    """Everything an agent or a status command needs, as plain data."""
    now = time.time() if now is None else now
    cache = read_json(cache_path())
    stale = is_stale(cache, cfg, now)
    claude = {"stale": stale, "windows": [], "hold": None, "sessions": []}
    if isinstance(cache, dict):
        claude["data_age_seconds"] = max(0, int(now - _num(cache.get("ts"), now)))
        claude["windows"] = _window_rows(cache, cfg, now)
    live = sessions(cfg, now)
    for entry in live:
        t = terms(entry["priority"], idle_above(live, entry.get("id"), now), cfg)
        row = {
            "id": entry.get("id"), "priority": entry["priority"], "cwd": entry.get("cwd", ""),
            "last_call_seconds_ago": max(0, int(now - _num(entry.get("last_call"), now))),
            "lift": round(t.lift, 2), "margin_factor": round(t.margin_factor, 2),
            "delay_factor": round(t.delay_factor, 2), "hold": None,
            "delay_seconds": round(pace_delay(paces(cache, cfg, now, t, profile_of(cfg))), 1) if isinstance(cache, dict) and not stale else 0,
        }
        hold = entry.get("hold")
        if isinstance(hold, dict) and _num(hold.get("until"), 0) > now:
            row["hold"] = {"window": hold.get("label"), "until": int(_num(hold["until"], 0)), "kind": hold.get("kind")}
            if claude["hold"] is None or row["hold"]["until"] > claude["hold"]["until"]:
                claude["hold"] = dict(row["hold"])
        claude["sessions"].append(row)
    codex = {"windows": [], "hold": None}
    codex_cache = codex_limits(now, _num(cfg.get("codex_log_max_age_seconds"), DEFAULTS["codex_log_max_age_seconds"]))
    if codex_cache:
        codex["data_age_seconds"] = max(0, int(now - codex_cache["ts"]))
        codex["source"] = codex_cache.get("source", "log")
        codex["windows"] = _window_rows(codex_cache, cfg, now)
    marker = read_json(codex_blocked_path())
    if isinstance(marker, dict) and _num(marker.get("until"), 0) > now:
        codex["hold"] = {"window": marker.get("label"), "until": int(_num(marker["until"], 0))}
    return {
        "guard": {"enabled": bool(cfg["enabled"]), "pace_enabled": bool(cfg["pace_enabled"]),
                  "pace_mode": cfg["pace_mode"]},
        "claude": claude,
        "codex": codex,
        "brief": brief_line(claude, codex, cfg),
    }


def _window_phrase(w: dict) -> str:
    text = f"{w['window']} {w['used_pct']:.0f}%"
    ahead = w["ahead_pct"]
    if ahead >= 0.5:
        text += f" (+{ahead:.0f} over pace line)"
    if w.get("pacing"):
        text += f" PACING {w['delay_seconds']:.0f}s/call"
    return text


def brief_line(claude: dict, codex: dict, cfg) -> str:
    """One line telling an agent where both vendors stand."""
    parts = []
    if claude["windows"] and not claude["stale"]:
        parts.append("claude " + ", ".join(_window_phrase(w) for w in claude["windows"]))
    elif claude["windows"]:
        parts.append("claude: usage data stale")
    if codex["windows"]:
        parts.append("codex " + ", ".join(_window_phrase(w) for w in codex["windows"]))
    for vendor in (claude, codex):
        if vendor.get("hold"):
            clock = time.strftime("%H:%M", time.localtime(vendor["hold"]["until"]))
            parts.append(f"HOLD on {vendor['hold']['window']} until {clock}")
    if not parts:
        return ""
    line = "[usage] " + " · ".join(parts)
    if not cfg["enabled"]:
        line += " · guard off"
    elif not cfg["pace_enabled"]:
        line += " · pacing off"
    if any(w.get("pacing") for w in claude["windows"] + codex["windows"]):
        line += (". Spend is running ahead of the window: the guard is pacing each tool call for you, so keep working "
                 "through the delays. Prefer fewer, larger steps; batch reads; defer fan-outs and long loops until back "
                 "on pace. Pacing is never a reason to stop, pause or ask the user to continue.")
    return line


# --- Codex hooks: install and trust ------------------------------------------
#
# Codex runs hooks.json hooks only after the user has reviewed them in its TUI,
# which records a trust hash in config.toml. The hash is a SHA-256 over the
# normalized hook definition (event, matcher, command, timeout, async,
# statusMessage), so it can be computed here and written alongside the hooks:
# an explicit install step that stands in for the review.

CODEX_EVENT_LABEL = {
    "PreToolUse": "pre_tool_use", "PermissionRequest": "permission_request",
    "PostToolUse": "post_tool_use", "PreCompact": "pre_compact", "PostCompact": "post_compact",
    "SessionStart": "session_start", "SessionEnd": "session_end",
    "UserPromptSubmit": "user_prompt_submit", "SubagentStart": "subagent_start",
    "SubagentStop": "subagent_stop", "Stop": "stop", "Interrupt": "interrupt",
}
CODEX_DEFAULT_TIMEOUT = 600


def codex_home() -> Path:
    env = os.environ.get("UG_CODEX_HOME") or os.environ.get("CODEX_HOME")
    return Path(env) if env else Path.home() / ".codex"


def codex_hooks_path() -> Path:
    return codex_home() / "hooks.json"


def codex_config_path() -> Path:
    return codex_home() / "config.toml"


def codex_hook_hash(event: str, matcher, handler: dict) -> str:
    """Codex's trust hash for one command hook (see codex-rs hooks/engine/discovery.rs)."""
    import hashlib
    timeout = handler.get("timeout")
    if event in ("SessionEnd", "Interrupt"):
        timeout = min(max(int(timeout or 1), 1), 3)
    else:
        timeout = max(int(timeout or CODEX_DEFAULT_TIMEOUT), 1)
    config = {"type": "command", "command": handler["command"], "timeout": timeout,
              "async": bool(handler.get("async", False))}
    if handler.get("statusMessage"):
        config["statusMessage"] = handler["statusMessage"]
    identity = {"event_name": CODEX_EVENT_LABEL[event], "hooks": [config]}
    if matcher is not None:
        identity["matcher"] = matcher
    blob = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def codex_guard_hooks(script_dir: Path) -> dict:
    """The hooks.json entries the guard needs under Codex."""
    base = str(script_dir).replace(str(Path.home()), "~", 1)
    return {
        "UserPromptSubmit": [{"hooks": [{"type": "command",
            "command": f"python3 {base}/codex-hook.py --event user_prompt_submit", "timeout": 10}]}],
        "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command",
            "command": f"python3 {base}/codex-hook.py --event pre_tool_use", "timeout": 21700,
            "statusMessage": "usage guard"}]}],
    }


def _is_guard_hook(handler: dict) -> bool:
    return "usage-guard/" in handler.get("command", "")


def codex_trust_entries(doc: dict, hooks_path: Path) -> dict:
    """{state key: trust hash} for every command hook in a hooks.json document."""
    out = {}
    for event, groups in (doc.get("hooks") or {}).items():
        if event not in CODEX_EVENT_LABEL:
            continue
        for gi, group in enumerate(groups or []):
            for hi, handler in enumerate(group.get("hooks") or []):
                if handler.get("type", "command") != "command":
                    continue
                key = f"{hooks_path}:{CODEX_EVENT_LABEL[event]}:{gi}:{hi}"
                out[key] = codex_hook_hash(event, group.get("matcher"), handler)
    return out


def codex_trust_state(config_text: str) -> dict:
    """{state key: trust hash} already recorded in a config.toml text."""
    import re
    found = {}
    for m in re.finditer(r'^\[hooks\.state\."((?:[^"\\]|\\.)*)"\]\s*\n((?:(?!^\[).*\n?)*)', config_text, re.M):
        key = m.group(1).replace('\\"', '"').replace("\\\\", "\\")
        h = re.search(r'^trusted_hash\s*=\s*"([^"]+)"', m.group(2), re.M)
        if h:
            found[key] = h.group(1)
    return found


def codex_install(script_dir: Path) -> dict:
    """Add the guard's hooks to Codex's hooks.json and trust them in config.toml.

    Idempotent: existing guard entries are replaced, other hooks and trust
    entries are left alone. Returns what changed.
    """
    hooks_path = codex_hooks_path()
    doc = read_json(hooks_path, {})
    if not isinstance(doc, dict):
        doc = {}
    hooks = doc.setdefault("hooks", {})
    wanted = codex_guard_hooks(script_dir)
    for event, groups in wanted.items():
        kept = [grp for grp in (hooks.get(event) or [])
                if not any(_is_guard_hook(h) for h in grp.get("hooks") or [])]
        hooks[event] = kept + groups
    atomic_write(hooks_path, doc)

    needed = codex_trust_entries(doc, hooks_path)
    cfg_path = codex_config_path()
    try:
        text = cfg_path.read_text()
    except OSError:
        text = ""
    have = codex_trust_state(text)
    missing = {k: v for k, v in needed.items() if have.get(k) != v}
    if missing:
        import re
        for key in missing:
            # drop a stale block for the same key before appending the fresh one
            text = re.sub(r'^\[hooks\.state\."' + re.escape(key) + r'"\]\s*\n(?:(?!^\[).*\n?)*', "", text, flags=re.M)
        if text and not text.endswith("\n"):
            text += "\n"
        for key, h in missing.items():
            text += f'\n[hooks.state."{key}"]\ntrusted_hash = "{h}"\n'
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg_path.with_suffix(".toml.ug-tmp")
        tmp.write_text(text)
        os.replace(tmp, cfg_path)
    return {"hooks_path": str(hooks_path), "config_path": str(cfg_path),
            "hooks": sum(len(v) for v in wanted.values()), "trusted_now": sorted(missing)}


def codex_trusted(script_dir: Path) -> bool:
    """True when hooks.json carries the guard's hooks and config.toml trusts them."""
    hooks_path = codex_hooks_path()
    doc = read_json(hooks_path)
    if not isinstance(doc, dict):
        return False
    present = {h.get("command") for groups in (doc.get("hooks") or {}).values()
               for grp in groups or [] for h in grp.get("hooks") or []}
    wanted = {h["command"] for groups in codex_guard_hooks(script_dir).values()
              for grp in groups for h in grp["hooks"]}
    if not wanted <= present:
        return False
    try:
        have = codex_trust_state(codex_config_path().read_text())
    except OSError:
        return False
    needed = codex_trust_entries(doc, hooks_path)
    guard_keys = {key for key, _ in _guard_entries(doc, hooks_path)}
    return all(have.get(key) == needed[key] for key in guard_keys)


def _guard_entries(doc: dict, hooks_path: Path):
    for event, groups in (doc.get("hooks") or {}).items():
        if event not in CODEX_EVENT_LABEL:
            continue
        for gi, grp in enumerate(groups or []):
            for hi, h in enumerate(grp.get("hooks") or []):
                if _is_guard_hook(h):
                    yield f"{hooks_path}:{CODEX_EVENT_LABEL[event]}:{gi}:{hi}", h
