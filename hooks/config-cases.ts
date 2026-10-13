// Effective-config cases: pure JSON after the `=`, read by hooks/pace.test.ts and
// by tests/test_parity.py so parseConfig and guardlib.parse_config layer a stored
// config.json the same way. `expect` lists, per reader (`shared` is no vendor),
// the keys that differ from DEFAULTS; every other key must be at its default.
export const CONFIG_CASES = [
  {
    "name": "an old config with shared keys only reads the same for every vendor",
    "stored": { "enabled": false, "pace_mode": "delay", "threshold_5h": 80.0 },
    "expect": {
      "shared": { "enabled": false, "pace_mode": "delay", "threshold_5h": 80.0 },
      "claude": { "enabled": false, "pace_mode": "delay", "threshold_5h": 80.0 },
      "codex": { "enabled": false, "pace_mode": "delay", "threshold_5h": 80.0 }
    }
  },
  {
    "name": "a vendor's override beats the shared key for that vendor alone",
    "stored": { "pace_enabled": false, "claude": { "enabled": false }, "codex": { "pace_mode": "delay", "pace_enabled": true } },
    "expect": {
      "shared": { "pace_enabled": false },
      "claude": { "enabled": false, "pace_enabled": false },
      "codex": { "pace_mode": "delay" }
    }
  },
  {
    "name": "a key the override lacks comes from the shared settings, then the defaults",
    "stored": { "threshold_7d": 80.0, "codex": { "threshold_5h": 70.0, "pace_profile_days": [1, 1, 1, 1, 1, 0.3, 0.3] } },
    "expect": {
      "shared": { "threshold_7d": 80.0 },
      "claude": { "threshold_7d": 80.0 },
      "codex": { "threshold_5h": 70.0, "threshold_7d": 80.0, "pace_profile_days": [1, 1, 1, 1, 1, 0.3, 0.3] }
    }
  },
  {
    "name": "a bad value in an override is skipped and the shared one shows through",
    "stored": { "pace_mode": "delay", "pace_enabled": false,
                "codex": { "pace_mode": "sideways", "pace_enabled": "yes", "threshold_5h": true, "pace_profile_days": [1, 2], "nope": 1 } },
    "expect": {
      "shared": { "pace_mode": "delay", "pace_enabled": false },
      "claude": { "pace_mode": "delay", "pace_enabled": false },
      "codex": { "pace_mode": "delay", "pace_enabled": false }
    }
  },
  {
    "name": "an unknown shared pace mode falls to the default; a vendor's valid one still applies",
    "stored": { "pace_mode": "fast", "claude": { "pace_mode": "delay" } },
    "expect": {
      "shared": {},
      "claude": { "pace_mode": "delay" },
      "codex": {}
    }
  },
  {
    "name": "a vendor entry that is not an object is ignored",
    "stored": { "enabled": false, "claude": "off", "codex": [1] },
    "expect": {
      "shared": { "enabled": false },
      "claude": { "enabled": false },
      "codex": { "enabled": false }
    }
  },
  {
    "name": "a config that is not an object reads as the defaults",
    "stored": [1, 2],
    "expect": { "shared": {}, "claude": {}, "codex": {} }
  }
]
