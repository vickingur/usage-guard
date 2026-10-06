#!/usr/bin/env python3
"""UserPromptSubmit hook: one line of usage context at the start of every turn.

Agents cannot see the statusline. This hands them the same position, for both
Claude and Codex, as `additionalContext`, so a plan that would burn a fan-out or
a long loop can be weighed against what is left in the windows. Silent when
there is no usage data yet. Claude Code runs it bare; Codex runs it with
`--vendor codex`, and the Codex cache is refreshed from the live transcript.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True  # keep the shared layer free of __pycache__
sys.path.insert(0, str(Path(__file__).resolve().parent))
import guardlib as g  # noqa: E402


def main():
    payload = None
    try:
        payload = json.loads(sys.stdin.read() or "null")
    except (OSError, ValueError):
        pass
    if "--vendor" in sys.argv and sys.argv[sys.argv.index("--vendor") + 1] == "codex":
        # Codex names its live transcript; refresh the Codex cache from it first.
        path = payload.get("transcript_path") if isinstance(payload, dict) else None
        if path:
            entry = g.limits_from_codex_log(Path(path), time.time())
            if entry:
                try:
                    g.atomic_write(g.cache_path("codex"), entry)
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
