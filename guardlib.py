"""Shared state and evaluation logic for the usage guard.

The statusline is the only component Claude Code hands `rate_limits` to, so it
writes what it sees into a cache file here; the hooks and the `ug` CLI read it
back. Codex usage comes from the newest Codex session log, which records the
rate limits the API returned with every turn.

Two mechanisms sit on top of that data:

- the **threshold** hold: a window at or over its threshold stops tool calls
  until it resets (see guard-hook.py);
- **pacing**: a window whose usage runs ahead of its *pace line* by more than a
  margin slows tool calls down so the budget lasts until the reset. The pace
  line is the usage you would have at this instant if spend were spread evenly
  across the window and landed exactly on the threshold at reset.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULTS = {
    "enabled": True,
    "threshold_5h": 95.0,
    "threshold_7d": 90.0,
    "max_stall_seconds": 21600,
    "poll_seconds": 5,
    "stale_after_seconds": 600,
    # Pacing. `pace_mode` is "delay" (sleep per tool call, proportional to how
    # far ahead of the line usage is) or "hold" (block until back on pace).
    "pace_enabled": True,
    "pace_mode": "delay",
    "pace_margin_5h": 20.0,
    "pace_margin_7d": 5.0,
    "pace_min_used_pct": 30.0,
    "pace_seconds_per_pct": 10.0,
    "pace_max_delay_seconds": 90.0,
    # A Codex session log older than this no longer says anything about now;
    # windows that have reset since the log was written are dropped anyway.
    "codex_log_max_age_seconds": 7 * 86400,
}

PACE_MODES = ("delay", "hold")

# (key in the statusline payload, config key holding its threshold, display label)
WINDOWS = (
    ("five_hour", "threshold_5h", "5h"),
    ("seven_day", "threshold_7d", "7d"),
)
WINDOW_SECONDS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
PACE_MARGIN_KEY = {"five_hour": "pace_margin_5h", "seven_day": "pace_margin_7d"}
# Codex reports `window_minutes`; map them onto the same two windows.
CODEX_WINDOW_MINUTES = {300: "five_hour", 10080: "seven_day"}


@dataclass(frozen=True)
class Violation:
    label: str
    pct: float
    resets_at: int
    threshold: float


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
    margin: float      # how far ahead is tolerated
    active: bool       # pacing is engaged for this window
    delay_seconds: float   # per-tool-call delay in "delay" mode (0 when inactive)
    catchup_at: int    # when the line reaches pct - margin, i.e. when pacing would release


def base_dir() -> Path:
    return Path(os.environ.get("UG_DIR") or (Path.home() / ".claude" / "usage-guard"))


def config_path() -> Path:
    return base_dir() / "config.json"


def cache_path() -> Path:
    return base_dir() / "usage.json"


def state_path() -> Path:
    return base_dir() / "state.json"


def blocked_path() -> Path:
    return base_dir() / "blocked.json"


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


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    stored = read_json(config_path(), {})
    if isinstance(stored, dict):
        cfg.update({k: v for k, v in stored.items() if k in DEFAULTS})
    if cfg["pace_mode"] not in PACE_MODES:
        cfg["pace_mode"] = DEFAULTS["pace_mode"]
    return cfg


def save_config(updates: dict) -> dict:
    stored = read_json(config_path(), {})
    if not isinstance(stored, dict):
        stored = {}
    stored.update(updates)
    atomic_write(config_path(), stored)
    return load_config()


def is_stale(cache, cfg, now: float) -> bool:
    if not isinstance(cache, dict) or "ts" not in cache:
        return True
    try:
        return (now - float(cache["ts"])) > float(cfg["stale_after_seconds"])
    except (TypeError, ValueError):
        return True


def violations(cache, cfg, now: float) -> list[Violation]:
    """Windows at or over their threshold whose reset time is still in the future."""
    found = []
    if not isinstance(cache, dict):
        return found
    for key, cfg_key, label in WINDOWS:
        window = cache.get(key)
        if not isinstance(window, dict):
            continue
        try:
            pct = float(window["used_percentage"])
            resets_at = int(window["resets_at"])
            threshold = float(cfg[cfg_key])
        except (KeyError, TypeError, ValueError):
            continue
        if pct >= threshold and resets_at > now:
            found.append(Violation(label, pct, resets_at, threshold))
    return found


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


def pace_line(threshold: float, resets_at: float, window_seconds: float, now: float) -> float:
    """Usage the pace line allows at `now`: threshold scaled by the elapsed fraction."""
    remaining = min(max(resets_at - now, 0.0), window_seconds)
    return threshold * (1.0 - remaining / window_seconds)


def paces(cache, cfg, now: float) -> list:
    """One Pace per window present in the cache, whether or not pacing is engaged."""
    out = []
    if not isinstance(cache, dict):
        return out
    enabled = bool(cfg.get("pace_enabled", True))
    min_used = _num(cfg.get("pace_min_used_pct"), DEFAULTS["pace_min_used_pct"])
    per_pct = _num(cfg.get("pace_seconds_per_pct"), DEFAULTS["pace_seconds_per_pct"])
    max_delay = _num(cfg.get("pace_max_delay_seconds"), DEFAULTS["pace_max_delay_seconds"])
    for key, cfg_key, label in WINDOWS:
        window = cache.get(key)
        if not isinstance(window, dict):
            continue
        try:
            pct = float(window["used_percentage"])
            resets_at = int(window["resets_at"])
        except (KeyError, TypeError, ValueError):
            continue
        threshold = _num(cfg.get(cfg_key), DEFAULTS[cfg_key])
        margin = _num(cfg.get(PACE_MARGIN_KEY[key]), DEFAULTS[PACE_MARGIN_KEY[key]])
        length = WINDOW_SECONDS[key]
        line = pace_line(threshold, resets_at, length, now)
        ahead = pct - line
        over = ahead - margin
        active = enabled and resets_at > now and pct >= min_used and over > 0
        delay = min(max_delay, over * per_pct) if active else 0.0
        # The line reaches (pct - margin) when remaining = W * (1 - (pct - margin) / threshold).
        if threshold <= 0 or pct - margin >= threshold:
            catchup = resets_at
        else:
            catchup = int(resets_at - length * (1.0 - (pct - margin) / threshold))
            catchup = max(int(now), min(catchup, resets_at))
        out.append(Pace(key, label, pct, resets_at, threshold, line, ahead, margin,
                        active, delay, catchup))
    return out


def pace_delay(pace_list) -> float:
    """The per-call delay pacing asks for: the worst window wins."""
    return max([p.delay_seconds for p in pace_list if p.active] or [0.0])


# --- Codex -----------------------------------------------------------------

def _newest_codex_log(root: Path) -> Optional[Path]:
    newest, newest_mtime = None, -1.0
    try:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                path = Path(dirpath) / name
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if mtime > newest_mtime:
                    newest, newest_mtime = path, mtime
    except OSError:
        return None
    return newest


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


def codex_limits(now: float, max_age_seconds: float) -> Optional[dict]:
    """Codex usage in the cache's own shape: {"ts", "five_hour"?, "seven_day"?}.

    None when there is no recent enough Codex session log or it carries no limits.
    """
    path = _newest_codex_log(codex_sessions_dir())
    if path is None:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if now - mtime > max_age_seconds:
        return None
    limits = _last_rate_limits(path)
    if not limits:
        return None
    entry = {"ts": mtime}
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


# --- reporting -------------------------------------------------------------

def report(cfg, now: Optional[float] = None) -> dict:
    """Everything an agent or a status command needs, as plain data."""
    now = time.time() if now is None else now
    cache = read_json(cache_path())
    stale = is_stale(cache, cfg, now)
    claude = {"stale": stale, "windows": [], "hold": None}
    if isinstance(cache, dict):
        claude["data_age_seconds"] = max(0, int(now - _num(cache.get("ts"), now)))
        for p in paces(cache, cfg, now):
            claude["windows"].append({
                "window": p.label, "used_pct": round(p.pct, 1), "threshold_pct": p.threshold,
                "resets_at": p.resets_at, "resets_in_seconds": max(0, p.resets_at - int(now)),
                "pace_line_pct": round(p.line, 1), "ahead_pct": round(p.ahead, 1),
                "margin_pct": p.margin, "pacing": p.active,
                "delay_seconds": round(p.delay_seconds, 1),
                "catchup_in_seconds": max(0, p.catchup_at - int(now)) if p.active else 0,
            })
    marker = read_json(blocked_path())
    if isinstance(marker, dict) and _num(marker.get("until"), 0) > now:
        claude["hold"] = {"window": marker.get("label"), "until": int(_num(marker["until"], 0))}
    codex = {"windows": []}
    codex_cache = codex_limits(now, _num(cfg.get("codex_log_max_age_seconds"), DEFAULTS["codex_log_max_age_seconds"]))
    if codex_cache:
        codex["data_age_seconds"] = max(0, int(now - codex_cache["ts"]))
        for p in paces(codex_cache, cfg, now):
            codex["windows"].append({
                "window": p.label, "used_pct": round(p.pct, 1),
                "resets_at": p.resets_at, "resets_in_seconds": max(0, p.resets_at - int(now)),
                "pace_line_pct": round(p.line, 1), "ahead_pct": round(p.ahead, 1),
                "margin_pct": p.margin, "ahead_of_pace": p.ahead > p.margin,
            })
    return {
        "guard": {"enabled": bool(cfg["enabled"]), "pace_enabled": bool(cfg["pace_enabled"]),
                  "pace_mode": cfg["pace_mode"]},
        "claude": claude,
        "codex": codex,
        "brief": brief_line(claude, codex, cfg),
    }


def _window_phrase(w: dict, for_claude: bool) -> str:
    text = f"{w['window']} {w['used_pct']:.0f}%"
    ahead = w["ahead_pct"]
    if ahead >= 0.5:
        text += f" (+{ahead:.0f} over pace line)"
    if for_claude and w.get("pacing"):
        text += f" PACING {w['delay_seconds']:.0f}s/call"
    elif not for_claude and w.get("ahead_of_pace"):
        text += " AHEAD OF PACE"
    return text


def brief_line(claude: dict, codex: dict, cfg) -> str:
    """One line telling an agent where both vendors stand."""
    parts = []
    if claude["windows"] and not claude["stale"]:
        parts.append("claude " + ", ".join(_window_phrase(w, True) for w in claude["windows"]))
    elif claude["windows"]:
        parts.append("claude: usage data stale")
    if claude.get("hold"):
        clock = time.strftime("%H:%M", time.localtime(claude["hold"]["until"]))
        parts.append(f"HOLD on {claude['hold']['window']} until {clock}")
    if codex["windows"]:
        parts.append("codex " + ", ".join(_window_phrase(w, False) for w in codex["windows"]))
    if not parts:
        return ""
    line = "[usage] " + " · ".join(parts)
    if not cfg["enabled"]:
        line += " · guard off"
    elif not cfg["pace_enabled"]:
        line += " · pacing off"
    if any(w.get("pacing") for w in claude["windows"]) or any(w.get("ahead_of_pace") for w in codex["windows"]):
        line += ". Spend is running ahead of the window: prefer fewer, larger steps; batch reads; avoid fan-outs and long loops until back on pace."
    return line
