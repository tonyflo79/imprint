from __future__ import annotations

import json

from imprint.health import health_report
from imprint.store import ImprintStore


def _config(root):
    hooks = root / "hooks"
    hooks.mkdir(parents=True)
    for name in ("session_start.py", "user_prompt_submit.py", "stop_capture.py", "health_check.py"):
        (hooks / name).write_text("# test\n")
    return {
        "compiler": True, "context_budget_bytes": 32768,
        "experimental": {"digest": False, "profile_learning": False},
        "hooks_dir": str(hooks),
    }


def test_health_reports_bare_lock_as_degraded_without_content(tmp_path):
    root = tmp_path / "operator"
    root.mkdir()
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    (root / "compiler.lock").mkdir()
    report = health_report(root, store, _config(root))
    assert report["status"] == "degraded"
    assert report["metrics"]["stale_lock_count"] == 1
    encoded = json.dumps(report)
    assert str(root) not in encoded


def test_health_does_not_claim_projection_or_backup_that_do_not_exist(tmp_path):
    root = tmp_path / "operator"
    root.mkdir()
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    report = health_report(root, store, _config(root))
    assert report["metrics"]["projection_snapshot_present"] is False
    assert report["metrics"]["backup_verified"] is False
