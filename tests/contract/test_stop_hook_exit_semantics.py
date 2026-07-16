"""Stop-hook bridge exit semantics toward the Claude Code host.

Exit 2 from a Stop hook blocks the host's stop and feeds stderr (not stdout)
back to the model. The bridge must therefore:

  - exit 2 only when the capture subprocess definitely failed (the one error
    a blocked stop can heal), with the reason on stderr
  - never exit 2 when stop_hook_active is true -- the host is already
    continuing because of a previous block, and re-blocking loops forever
  - fail open (exit 0) on invalid input, a missing or hung executable, and
    malformed output: none of those are healed by retrying, so blocking on
    them wedges the host in a stop loop whenever the install is degraded
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).parents[2] / "hooks"


def _stop(stdin_text: str, *, importable: bool) -> subprocess.CompletedProcess[str]:
    # PYTHONPATH="" makes `-m imprint.cli` unimportable inside the bridge's
    # subprocess, which is the definite-capture-failure class (returncode != 0).
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": ""}
    if importable:
        env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    return subprocess.run(
        [sys.executable, str(HOOKS / "stop_capture.py")],
        input=stdin_text, text=True, capture_output=True, check=False, cwd=HOOKS, env=env,
    )


def test_definite_capture_failure_blocks_once_with_reason_on_stderr():
    result = _stop(json.dumps({"stop_hook_active": False}), importable=False)
    assert result.returncode == 2
    assert "judgment capture failed" in result.stderr
    body = json.loads(result.stdout)
    assert body["error"] == "hook_action_failed"
    assert body["failure_policy"] == "fail_closed"


def test_stop_hook_active_suppresses_reblocking():
    result = _stop(json.dumps({"stop_hook_active": True}), importable=False)
    assert result.returncode == 0
    assert json.loads(result.stdout)["failure_policy"] == "fail_open"


def test_invalid_input_fails_open_instead_of_blocking():
    result = _stop("not json", importable=False)
    assert result.returncode == 0
    body = json.loads(result.stdout)
    assert body["error"] == "hook_input_invalid"
    assert body["failure_policy"] == "fail_open"


def test_failure_stdout_is_a_single_valid_json_object():
    """The host parses stdout as JSON on exit 0; mixed text breaks it."""
    for stdin_text in (json.dumps({"stop_hook_active": True}), "not json"):
        result = _stop(stdin_text, importable=False)
        json.loads(result.stdout)
