"""Portable Claude Code hook bridge. Contains no operator paths or content logs."""

from __future__ import annotations

import json
import subprocess
import sys

HOOK_TIMEOUT_SECONDS = 10
_EVENT_NAMES = {
    "session-start": "SessionStart",
    "user-prompt-submit": "UserPromptSubmit",
    "health-check": "SessionStart",
}


def _failure(action: str, error: str) -> int:
    """Stop fails closed; read-only context and health hooks fail open visibly."""
    body = {
        "hook_schema_version": "1.0.0",
        "status": "degraded",
        "error": error,
        "failure_policy": "fail_closed" if action == "stop-capture" else "fail_open",
    }
    if action != "stop-capture":
        body["hookSpecificOutput"] = {
            "hookEventName": _EVENT_NAMES[action],
            "additionalContext": "",
        }
    print(json.dumps(body, sort_keys=True))
    return 2 if action == "stop-capture" else 0


def run(action: str) -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _failure(action, "hook_input_invalid")
    try:
        process = subprocess.run(
            [sys.executable, "-m", "imprint.cli", "hook", action],
            input=json.dumps(event, ensure_ascii=False, separators=(",", ":")),
            text=True,
            capture_output=True,
            check=False,
            timeout=HOOK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _failure(action, "hook_action_timeout")
    except OSError:
        return _failure(action, "hook_executable_unavailable")
    if process.returncode:
        return _failure(action, "hook_action_failed")
    if process.stdout:
        sys.stdout.write(process.stdout)
    return 0
