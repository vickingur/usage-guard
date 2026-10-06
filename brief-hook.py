#!/usr/bin/env python3
"""UserPromptSubmit hook: one line of usage context at the start of every turn.

Agents cannot see the statusline. This hands them the same position, for both
Claude and Codex, as `additionalContext`, so a plan that would burn a fan-out or
a long loop can be weighed against what is left in the windows. Silent when
there is no usage data yet.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # keep the shared layer free of __pycache__
sys.path.insert(0, str(Path(__file__).resolve().parent))
import guardlib as g  # noqa: E402


def main():
    try:
        sys.stdin.read()
    except OSError:
        pass
    try:
        line = g.report(g.load_config())["brief"]
    except Exception:
        return 0
    if line:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": line,
        }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
