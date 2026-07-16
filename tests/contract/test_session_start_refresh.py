"""SessionStart must refresh memory context on resume/compact re-fires.

Regression coverage for finding I4: the once-per-(session, snapshot) delivery
latch dropped memory context exactly when the context window was wiped, because
the hook never read event.source. These tests exercise the in-process CLI hook
handler directly (no subprocess, no PYTHONPATH assumptions) so they hold whether
imprint is imported from source or an installed distribution.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import imprint.cli as cli


def _config(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "data"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "config_version": "3.0.0", "data_root": str(data),
        "operator_slug": "test", "node_id": "primary", "compiler": True,
        "context_budget_bytes": 32768,
    }))
    return config, data


def _hook(config: Path, action: str, event: dict, monkeypatch, capsys) -> dict:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(event)))
    code = cli.main(["--config", str(config), "hook", action])
    body = json.loads(capsys.readouterr().out)
    body["_returncode"] = code
    return body


def _seed(config: Path, monkeypatch, capsys) -> None:
    receipt = _hook(config, "stop-capture", {
        "session_id": "seed",
        "operator_text": "No, report the failed source because omissions change the decision.",
    }, monkeypatch, capsys)
    assert receipt["canonical_status"] == "compiled", receipt


def test_compact_refreshes_dropped_memory_context(tmp_path, monkeypatch, capsys):
    config, _ = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    session = "user-session"

    startup = _hook(config, "session-start",
                    {"session_id": session, "source": "startup"}, monkeypatch, capsys)
    assert startup["status"] == "delivered"
    assert "failed source" in startup["hookSpecificOutput"]["additionalContext"]

    # The window was compacted; SessionStart re-fires with the same session_id
    # and an unchanged store (same snapshot). Memory must be redelivered, not
    # suppressed by the latch.
    compact = _hook(config, "session-start",
                    {"session_id": session, "source": "compact"}, monkeypatch, capsys)
    assert compact["status"] == "delivered"
    assert "failed source" in compact["hookSpecificOutput"]["additionalContext"]
    assert compact["hookSpecificOutput"]["additionalContext"] == \
        startup["hookSpecificOutput"]["additionalContext"]


def test_resume_refreshes_dropped_memory_context(tmp_path, monkeypatch, capsys):
    config, _ = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    session = "user-session"

    startup = _hook(config, "session-start",
                    {"session_id": session, "source": "startup"}, monkeypatch, capsys)
    assert startup["status"] == "delivered"

    resume = _hook(config, "session-start",
                   {"session_id": session, "source": "resume"}, monkeypatch, capsys)
    assert resume["status"] == "delivered"
    assert "failed source" in resume["hookSpecificOutput"]["additionalContext"]


def test_repeated_compact_keeps_redelivering(tmp_path, monkeypatch, capsys):
    config, _ = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    session = "user-session"

    _hook(config, "session-start",
          {"session_id": session, "source": "startup"}, monkeypatch, capsys)
    for _ in range(3):
        again = _hook(config, "session-start",
                      {"session_id": session, "source": "compact"}, monkeypatch, capsys)
        assert again["status"] == "delivered"
        assert again["hookSpecificOutput"]["additionalContext"]


def test_startup_still_honors_once_delivery_latch(tmp_path, monkeypatch, capsys):
    # A non-wiping re-fire (fresh startup / unspecified source) must keep the
    # once-delivery invariant so unchanged snapshots are not re-injected.
    config, _ = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    session = "user-session"

    first = _hook(config, "session-start", {"session_id": session}, monkeypatch, capsys)
    assert first["status"] == "delivered"
    second = _hook(config, "session-start",
                   {"session_id": session, "source": "startup"}, monkeypatch, capsys)
    assert second["status"] == "already_delivered"
    assert second["hookSpecificOutput"]["additionalContext"] == ""
