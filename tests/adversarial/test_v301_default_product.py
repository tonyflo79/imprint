from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from imprint.store import ImprintStore


ROOT = Path(__file__).parents[2]


def _config(tmp_path: Path, *, compiler: bool) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    data = tmp_path / "data"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "config_version": "3.0.0", "data_root": str(data),
        "operator_slug": "test", "node_id": "primary", "compiler": compiler,
        "context_budget_bytes": 32768,
    }))
    return config, data


def _hook(config: Path, action: str, event: dict) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, IMPRINT_CONFIG=str(config))
    return subprocess.run(
        [sys.executable, "-m", "imprint.cli", "hook", action],
        input=json.dumps(event), text=True, capture_output=True,
        cwd=ROOT, env=env, check=False,
    )


def test_stop_capture_closes_canon_on_compiler_but_noncompiler_is_spool_only(tmp_path):
    config, data = _config(tmp_path / "compiler", compiler=True)
    result = _hook(config, "stop-capture", {
        "session_id": "native-compiler-session",
        "operator_text": "No, report every failed source because omissions change the decision.",
    })
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["canonical_status"] == "compiled"
    assert receipt["compile"] == {"captured": 1, "duplicate": 0, "quarantined": 0}
    store = ImprintStore(data / "test" / "imprint.db")
    assert store.current_nodes(["Verdict"])[0]["payload"]["raw_operator_text"].startswith("No, report")

    config, data = _config(tmp_path / "producer", compiler=False)
    result = _hook(config, "stop-capture", {
        "session_id": "native-producer-session",
        "operator_text": "No, preserve the exact correction because provenance matters.",
    })
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["canonical_status"] == "spool_only"
    assert len(list((data / "test" / "spool" / "primary").glob("*.json"))) == 1
    assert not (data / "test" / "imprint.db").exists()


def test_native_session_mapping_is_stable_local_opaque_and_never_stores_raw_id(tmp_path):
    config, data = _config(tmp_path, compiler=True)
    raw_same = "claude-native-sensitive-session-123"
    raw_other = "claude-native-sensitive-session-456"
    for raw, text in (
        (raw_same, "No, preserve the source because the evidence changes the recommendation."),
        (raw_same, "No, state the uncertainty because confidence was not established."),
        (raw_other, "No, retain the rejected option because the tradeoff matters."),
    ):
        result = _hook(config, "stop-capture", {"session_id": raw, "operator_text": text})
        assert result.returncode == 0, result.stdout + result.stderr
    spools = [json.loads(path.read_text()) for path in sorted(
        (data / "test" / "spool" / "primary").glob("*.json")
    )]
    session_counts = {}
    for item in spools:
        session_counts[item["session_id"]] = session_counts.get(item["session_id"], 0) + 1
    assert sorted(session_counts.values()) == [1, 2]
    assert all(item["session_id"].startswith("urn:imprint:session:") for item in spools)
    key = data / "test" / "session-map.key"
    assert len(key.read_text().strip()) == 64
    for path in (data / "test").rglob("*"):
        if path.is_file() and path.suffix in {".json", ".key"}:
            content = path.read_text(encoding="utf-8")
            assert raw_same not in content and raw_other not in content


def test_missing_native_session_ids_receive_distinct_event_local_opaque_sessions(tmp_path):
    config, data = _config(tmp_path, compiler=True)
    for text in (
        "No, preserve the source because it changes the decision.",
        "No, preserve the uncertainty because it changes the confidence.",
    ):
        result = _hook(config, "stop-capture", {"operator_text": text})
        assert result.returncode == 0, result.stdout + result.stderr
    spools = [json.loads(path.read_text()) for path in
              (data / "test" / "spool" / "primary").glob("*.json")]
    assert len({item["session_id"] for item in spools}) == 2


def test_huge_transcript_preserves_feedback_with_bounded_hashed_evidence(tmp_path):
    config, data = _config(tmp_path, compiler=True)
    transcript = tmp_path / "huge.jsonl"
    bounded_context = json.dumps({
        "type": "assistant", "message": {"role": "assistant", "content": "context " * 20000},
    })
    feedback = "No, keep the correction even when the transcript is huge because losing it corrupts the record."
    user = json.dumps({"type": "user", "message": {"role": "user", "content": feedback}})
    tail_payload = ("\n" + bounded_context + "\n" + user + "\n").encode()
    with transcript.open("wb") as handle:
        handle.seek((1024 * 1024 * 1024) - len(tail_payload))
        handle.write(tail_payload)
    started = time.monotonic()
    result = _hook(config, "stop-capture", {
        "hook_event_name": "Stop", "session_id": "huge-native-session",
        "transcript_path": str(transcript),
    })
    assert time.monotonic() - started < 5
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["canonical_status"] == "compiled"
    assert receipt["degradation"]["truncated"] is True
    assert receipt["degradation"]["transcript_bytes"] == transcript.stat().st_size
    assert len(receipt["degradation"]["evidence_sha256"]) == 64
    assert receipt["degradation"]["hash_scope"] == "bounded_tail"
    spool = json.loads(next((data / "test" / "spool" / "primary").glob("*.json")).read_text())
    assert spool["verdict"]["raw_operator_text"] == feedback
    assert len(spool["evidence"]) == 2
    assert len(spool["evidence"][1]["content"].encode("utf-8")) <= 64 * 1024
    extension = spool["extensions"]["org.imprint.transcript"]["payload"]
    assert extension["receipt"] == "huge_transcript_bounded_tail"
    assert extension["truncated"] is True


def _bridge_module():
    spec = importlib.util.spec_from_file_location("imprint_test_bridge", ROOT / "hooks" / "_bridge.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("script,expected", [
    ("stop_capture.py", 2),
    ("session_start.py", 0),
    ("user_prompt_submit.py", 0),
    ("health_check.py", 0),
])
def test_every_installed_hook_bridge_has_documented_invalid_input_policy(script, expected):
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / script)], input="not-json",
        text=True, capture_output=True, cwd=ROOT / "hooks", check=False,
    )
    assert result.returncode == expected
    body = json.loads(result.stdout)
    assert body["failure_policy"] == ("fail_closed" if expected else "fail_open")


@pytest.mark.parametrize("action,expected", [
    ("stop-capture", 2), ("session-start", 0),
    ("user-prompt-submit", 0), ("health-check", 0),
])
def test_hook_child_hang_is_killed_at_bound_with_declared_policy(tmp_path, monkeypatch, capsys, action, expected):
    fake = tmp_path / "fake"
    package = fake / "imprint"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text("import time; time.sleep(60)\n")
    bridge = _bridge_module()
    bridge.HOOK_TIMEOUT_SECONDS = 0.05
    monkeypatch.setenv("PYTHONPATH", str(fake))
    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO("{}"))
    assert bridge.run(action) == expected
    body = json.loads(capsys.readouterr().out)
    assert body["error"] == "hook_action_timeout"
    assert body["failure_policy"] == ("fail_closed" if expected else "fail_open")


@pytest.mark.parametrize("action,expected", [
    ("stop-capture", 2), ("session-start", 0),
    ("user-prompt-submit", 0), ("health-check", 0),
])
def test_missing_hook_executable_uses_declared_policy(monkeypatch, capsys, action, expected):
    bridge = _bridge_module()
    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(bridge.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("missing")))
    assert bridge.run(action) == expected
    assert json.loads(capsys.readouterr().out)["error"] == "hook_executable_unavailable"
