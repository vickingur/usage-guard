// Parity cases: pure JSON after the `=`, read by hooks/pace.test.ts and by
// tests/test_parity.py so the TypeScript and Python pace math agree. Times are
// epoch seconds; `now` is 1_000_000 and windows are given by their reset
// offsets from it. Expected values are rounded to 3 decimals.
export const CASES = [
  {
    "name": "2h into a 5h window at 70% under account terms: line 38, 12 over the margin",
    "config": {},
    "priority": "high",
    "idle": {},
    "now": 1000000,
    "windows": [
      {
        "key": "five_hour",
        "pct": 70.0,
        "resets_in": 10800
      }
    ],
    "expect": {
      "terms": {
        "marginFactor": 1,
        "delayFactor": 1,
        "lift": 0
      },
      "paces": [
        {
          "label": "5h",
          "line": 38.0,
          "ahead": 32.0,
          "margin": 20.0,
          "active": true,
          "delaySeconds": 30.0,
          "catchup_in": 2273,
          "holdAt": 95.0
        }
      ]
    }
  },
  {
    "name": "the same window for a normal session with a busy high session: half the margin, double the delay",
    "config": {},
    "priority": "normal",
    "idle": {
      "high": 10
    },
    "now": 1000000,
    "windows": [
      {
        "key": "five_hour",
        "pct": 70.0,
        "resets_in": 10800
      }
    ],
    "expect": {
      "terms": {
        "marginFactor": 0.5,
        "delayFactor": 2,
        "lift": 0
      },
      "paces": [
        {
          "label": "5h",
          "line": 38.0,
          "ahead": 32.0,
          "margin": 10.0,
          "active": true,
          "delaySeconds": 60.0,
          "catchup_in": 4168,
          "holdAt": 95.0
        }
      ]
    }
  },
  {
    "name": "a normal session halfway through the borrow ramp has terms halfway to high's",
    "config": {},
    "priority": "normal",
    "idle": {
      "high": 360
    },
    "now": 1000000,
    "windows": [
      {
        "key": "five_hour",
        "pct": 70.0,
        "resets_in": 10800
      }
    ],
    "expect": {
      "terms": {
        "marginFactor": 0.75,
        "delayFactor": 1.5,
        "lift": 0.5
      },
      "paces": [
        {
          "label": "5h",
          "line": 38.0,
          "ahead": 32.0,
          "margin": 15.0,
          "active": true,
          "delaySeconds": 45.0,
          "catchup_in": 3221,
          "holdAt": 95.0
        }
      ]
    }
  },
  {
    "name": "a low session alone on the machine borrows all the way up to high's terms",
    "config": {},
    "priority": "low",
    "idle": {},
    "now": 1000000,
    "windows": [
      {
        "key": "five_hour",
        "pct": 70.0,
        "resets_in": 10800
      }
    ],
    "expect": {
      "terms": {
        "marginFactor": 1,
        "delayFactor": 1,
        "lift": 2
      },
      "paces": [
        {
          "label": "5h",
          "line": 38.0,
          "ahead": 32.0,
          "margin": 20.0,
          "active": true,
          "delaySeconds": 30.0,
          "catchup_in": 2273,
          "holdAt": 95.0
        }
      ]
    }
  },
  {
    "name": "a low session stops at normal's terms while a high session is busy",
    "config": {},
    "priority": "low",
    "idle": {
      "high": 0
    },
    "now": 1000000,
    "windows": [
      {
        "key": "five_hour",
        "pct": 70.0,
        "resets_in": 10800
      }
    ],
    "expect": {
      "terms": {
        "marginFactor": 0.5,
        "delayFactor": 2,
        "lift": 1
      },
      "paces": [
        {
          "label": "5h",
          "line": 38.0,
          "ahead": 32.0,
          "margin": 10.0,
          "active": true,
          "delaySeconds": 60.0,
          "catchup_in": 4168,
          "holdAt": 95.0
        }
      ]
    }
  },
  {
    "name": "a low session with a busy normal session above it keeps its own terms: no margin, 4x delay",
    "config": {},
    "priority": "low",
    "idle": {
      "normal": 30
    },
    "now": 1000000,
    "windows": [
      {
        "key": "five_hour",
        "pct": 70.0,
        "resets_in": 10800
      }
    ],
    "expect": {
      "terms": {
        "marginFactor": 0,
        "delayFactor": 4,
        "lift": 0
      },
      "paces": [
        {
          "label": "5h",
          "line": 38.0,
          "ahead": 32.0,
          "margin": 0.0,
          "active": true,
          "delaySeconds": 120.0,
          "catchup_in": 6063,
          "holdAt": 95.0
        }
      ]
    }
  },
  {
    "name": "below pace_min_used_pct nothing paces however far ahead",
    "config": {},
    "priority": "low",
    "idle": {
      "normal": 0,
      "high": 0
    },
    "now": 1000000,
    "windows": [
      {
        "key": "five_hour",
        "pct": 25.0,
        "resets_in": 17000
      }
    ],
    "expect": {
      "terms": {
        "marginFactor": 0,
        "delayFactor": 4,
        "lift": 0
      },
      "paces": [
        {
          "label": "5h",
          "line": 5.278,
          "ahead": 19.722,
          "margin": 0.0,
          "active": false,
          "delaySeconds": 0.0,
          "catchup_in": 0,
          "holdAt": 95.0
        }
      ]
    }
  },
  {
    "name": "a weekly window within its margin is not pacing, the delay is small when just over",
    "config": {
      "pace_seconds_per_pct": 5.0
    },
    "priority": "high",
    "idle": {},
    "now": 1000000,
    "windows": [
      {
        "key": "seven_day",
        "pct": 60.0,
        "resets_in": 302400
      }
    ],
    "expect": {
      "terms": {
        "marginFactor": 1,
        "delayFactor": 1,
        "lift": 0
      },
      "paces": [
        {
          "label": "7d",
          "line": 45.0,
          "ahead": 15.0,
          "margin": 15.0,
          "active": false,
          "delaySeconds": 0.0,
          "catchup_in": 0,
          "holdAt": 90.0
        }
      ]
    }
  },
  {
    "name": "pacing off disengages every window",
    "config": {
      "pace_enabled": false
    },
    "priority": "low",
    "idle": {},
    "now": 1000000,
    "windows": [
      {
        "key": "five_hour",
        "pct": 70.0,
        "resets_in": 10800
      }
    ],
    "expect": {
      "terms": {
        "marginFactor": 1,
        "delayFactor": 1,
        "lift": 2
      },
      "paces": [
        {
          "label": "5h",
          "line": 38.0,
          "ahead": 32.0,
          "margin": 20.0,
          "active": false,
          "delaySeconds": 0.0,
          "catchup_in": 0,
          "holdAt": 95.0
        }
      ]
    }
  },
  {
    "name": "a step ramp (full <= after) lends nothing before `after` and everything from it",
    "config": {
      "borrow_after_seconds": 120,
      "borrow_full_seconds": 120
    },
    "priority": "normal",
    "idle": {
      "high": 119
    },
    "now": 1000000,
    "windows": [
      {
        "key": "five_hour",
        "pct": 70.0,
        "resets_in": 10800
      }
    ],
    "expect": {
      "terms": {
        "marginFactor": 0.5,
        "delayFactor": 2,
        "lift": 0
      },
      "paces": [
        {
          "label": "5h",
          "line": 38.0,
          "ahead": 32.0,
          "margin": 10.0,
          "active": true,
          "delaySeconds": 60.0,
          "catchup_in": 4168,
          "holdAt": 95.0
        }
      ]
    }
  },
  {
    "name": "a work-week profile: Monday 13:46, the line has climbed only the share of the week's weight so far",
    "config": {
      "pace_profile_days": [
        1,
        1,
        1,
        1,
        1,
        0.3,
        0.3
      ],
      "pace_profile_hours": [
        0.1,
        0.1,
        0.1,
        0.1,
        0.1,
        0.1,
        0.1,
        0.1,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        0.1
      ]
    },
    "priority": "high",
    "idle": {},
    "now": 1000000,
    "windows": [
      {
        "key": "seven_day",
        "pct": 20.0,
        "resets_in": 555200
      }
    ],
    "expect": {
      "terms": {
        "marginFactor": 1,
        "delayFactor": 1,
        "lift": 0
      },
      "paces": [
        {
          "label": "7d",
          "line": 6.649,
          "ahead": 13.351,
          "margin": 15.0,
          "active": false,
          "delaySeconds": 0.0,
          "catchup_in": 0,
          "holdAt": 90.0
        }
      ]
    }
  },
  {
    "name": "the same profile on Saturday night: most of the week's weight has elapsed, so 80% used is below the line",
    "config": {
      "pace_profile_days": [
        1,
        1,
        1,
        1,
        1,
        0.3,
        0.3
      ],
      "pace_profile_hours": [
        0.1,
        0.1,
        0.1,
        0.1,
        0.1,
        0.1,
        0.1,
        0.1,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        0.1
      ]
    },
    "priority": "high",
    "idle": {},
    "now": 1465200,
    "windows": [
      {
        "key": "seven_day",
        "pct": 80.0,
        "resets_in": 90000
      }
    ],
    "expect": {
      "terms": {
        "marginFactor": 1,
        "delayFactor": 1,
        "lift": 0
      },
      "paces": [
        {
          "label": "7d",
          "line": 85.148,
          "ahead": -5.148,
          "margin": 15.0,
          "active": false,
          "delaySeconds": 0.0,
          "catchup_in": 0,
          "holdAt": 90.0
        }
      ]
    }
  },
  {
    "name": "half a day before the weekly reset the hold level has climbed halfway from 90 to 100",
    "config": {},
    "priority": "high",
    "idle": {},
    "now": 1000000,
    "windows": [
      {
        "key": "seven_day",
        "pct": 94.0,
        "resets_in": 43200
      }
    ],
    "expect": {
      "terms": {
        "marginFactor": 1,
        "delayFactor": 1,
        "lift": 0
      },
      "paces": [
        {
          "label": "7d",
          "line": 83.571,
          "ahead": 10.429,
          "margin": 15.0,
          "active": false,
          "delaySeconds": 0.0,
          "catchup_in": 0,
          "holdAt": 95.0
        }
      ]
    }
  }
]
