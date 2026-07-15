from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from imprint.store import ImprintStore


def _run(repo: Path, config: Path, action: str, event: dict) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["IMPRINT_CONFIG"] = str(config)
    return subprocess.run(
        [sys.executable, "-m", "imprint.cli", "hook", action],
        input=json.dumps(event), text=True, capture_output=True, cwd=repo, env=env, check=False,
    )


def test_hook_capture_compile_retrieve_and_once_delivery(tmp_path):
    repo = Path(__file__).parents[2]
    data = tmp_path / "data root with spaces"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "config_version": "3.0.0", "data_root": str(data),
        "operator_slug": "test-operator", "node_id": "test-node", "compiler": True,
        "context_budget_bytes": 32768,
    }))
    event = {
        "session_id": "hook-session-1",
        "operator_text": "No, report the failed source explicitly because missing evidence changes the conclusion.",
        "case_description": "Reviewing a multi-source synthesis",
    }
    captured = _run(repo, config, "stop-capture", event)
    assert captured.returncode == 0, captured.stderr
    receipt = json.loads(captured.stdout)
    assert receipt["status"] == "queued"
    assert set(receipt) == {"event_id", "hook_schema_version", "spool_file", "status"}

    env = dict(os.environ, IMPRINT_CONFIG=str(config))
    compiled = subprocess.run(
        [sys.executable, "-m", "imprint.cli", "compile", "--once"],
        text=True, capture_output=True, cwd=repo, env=env, check=False,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    assert json.loads(compiled.stdout)["captured"] == 1

    store = ImprintStore(data / "test-operator" / "imprint.db")
    evidence_id = store.current_nodes(["Evidence"])[0]["node_id"]
    rule_id = store.append_derived_node(
        node_type="Rule",
        payload={"statement": "Cite every failed source in the research domain.", "domain_id": "research"},
        provenance_status="inferred", authority_tier="inferred_candidate",
        evidence_ids=[evidence_id], operator_id=store.current_nodes(["Verdict"])[0]["operator_id"],
        valid_from="2026-07-14T12:00:00Z", proposed_by="integration-test",
    )
    store.ratify_node(rule_id, ratifier="synthetic-operator")
    config_value = json.loads(config.read_text())
    config_value["domains"] = [{
        "domain_id": "research", "public_label": "Research",
        "safe_paths": ["Projects/Research"], "keywords": ["sources"], "frozen": False,
    }]
    config.write_text(json.dumps(config_value))

    first = _run(repo, config, "session-start", {"session_id": "fresh-session"})
    assert first.returncode == 0, first.stderr
    first_body = json.loads(first.stdout)
    assert first_body["status"] == "delivered"
    assert "failed source" in first_body["hookSpecificOutput"]["additionalContext"]
    second = _run(repo, config, "session-start", {"session_id": "fresh-session"})
    assert json.loads(second.stdout)["status"] == "already_delivered"

    domain = _run(repo, config, "user-prompt-submit", {
        "session_id": "fresh-session", "cwd": "Projects/Research/Current", "prompt": "review",
    })
    assert domain.returncode == 0, domain.stderr
    domain_body = json.loads(domain.stdout)
    assert domain_body["domain_id"] == "research"
    assert domain_body["selection_method"] == "path"
    context = domain_body["hookSpecificOutput"]["additionalContext"]
    assert "research domain" in context
    assert "missing evidence changes" not in context
    domain_again = _run(repo, config, "user-prompt-submit", {
        "session_id": "fresh-session", "cwd": "Projects/Research/Current", "prompt": "review",
    })
    assert json.loads(domain_again.stdout)["status"] == "already_delivered"

    assert store.integrity_check() == "ok"
    verdict = store.current_nodes(["Verdict"])[0]
    assert verdict["payload"]["reason"] is None
    assert verdict["payload"]["reason_status"] == "absent"


def test_stop_hook_without_feedback_text_is_honest_noop(tmp_path):
    repo = Path(__file__).parents[2]
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_root": str(tmp_path / "data"), "operator_slug": "test"}))
    result = _run(repo, config, "stop-capture", {"session_id": "s"})
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "hook_schema_version": "1.0.0",
        "reason": "feedback_text_unavailable",
        "status": "skipped",
    }


def test_native_claude_stop_payload_mines_bounded_transcript(tmp_path):
    repo = Path(__file__).parents[2]
    data = tmp_path / "data"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "data_root": str(data), "operator_slug": "test", "node_id": "primary",
    }))
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "I omitted one failed source."}]}}),
        json.dumps({"type": "user", "message": {"role": "user", "content": "No, explicitly report every failed source because omission changes the decision."}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "Understood."}}),
    ]) + "\n")
    result = _run(repo, config, "stop-capture", {
        "hook_event_name": "Stop", "session_id": "native-session",
        "transcript_path": str(transcript), "cwd": str(tmp_path),
        "stop_hook_active": False,
    })
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "queued"
    env = dict(os.environ, IMPRINT_CONFIG=str(config))
    compiled = subprocess.run(
        [sys.executable, "-m", "imprint.cli", "compile", "--once"],
        text=True, capture_output=True, cwd=repo, env=env, check=False,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    store = ImprintStore(data / "test" / "imprint.db")
    verdict = store.current_nodes(["Verdict"])[0]
    assert verdict["payload"]["raw_operator_text"].startswith("No, explicitly report")
    assert len(verdict["evidence"]) == 2


def test_hook_rejects_wrong_event_contract(tmp_path):
    repo = Path(__file__).parents[2]
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_root": str(tmp_path / "data"), "operator_slug": "test"}))
    result = _run(repo, config, "stop-capture", {
        "hook_schema_version": "9.0.0", "hook_event_name": "SessionStart",
    })
    assert result.returncode == 2
    assert json.loads(result.stdout)["error_type"] == "ValidationError"
