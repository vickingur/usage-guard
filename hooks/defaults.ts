// The guard's settings and their defaults. Pure JSON after the `=`: guardlib.py
// reads this file so the Python side carries the same table (tests/test_parity.py).
export const DEFAULTS = {
  "enabled": true,
  "threshold_5h": 95.0,
  "threshold_7d": 90.0,
  "max_stall_seconds": 21600,
  "poll_seconds": 5,
  "stale_after_seconds": 600,
  "pace_enabled": true,
  "pace_mode": "delay",
  "pace_margin_5h": 20.0,
  "pace_margin_7d": 15.0,
  "pace_min_used_pct": 30.0,
  "pace_seconds_per_pct": 5.0,
  "pace_max_delay_seconds": 30.0,
  "pace_profile_days": [1, 1, 1, 1, 1, 1, 1],
  "pace_profile_hours": [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
  "priority_margin_factor_normal": 0.5,
  "priority_margin_factor_low": 0.0,
  "priority_delay_factor_normal": 2.0,
  "priority_delay_factor_low": 4.0,
  "borrow_after_seconds": 120,
  "borrow_full_seconds": 600,
  "session_stale_seconds": 21600,
  "codex_log_max_age_seconds": 604800
}
