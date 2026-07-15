"""Portable Claude Code hook bridge. Contains no operator paths or content logs."""

from __future__ import annotations

import json
import subprocess
import sys


def run(action: str) -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        print(json.dumps({"hook_schema_version": "1.0.0", "error": "hook_input_invalid"}))
        return 2
    process = subprocess.run(
        [sys.executable, "-m", "imprint.cli", "hook", action],
        input=json.dumps(event, ensure_ascii=False, separators=(",", ":")),
        text=True,
        capture_output=True,
        check=False,
    )
    if process.stdout:
        sys.stdout.write(process.stdout)
    if process.returncode and not process.stdout:
        print(json.dumps({"hook_schema_version": "1.0.0", "error": "hook_action_failed"}))
    return process.returncode
