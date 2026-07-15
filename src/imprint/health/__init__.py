"""Content-free invariant health reporting."""

from __future__ import annotations

import shutil
import json
from pathlib import Path

from .report import HealthInputs, HealthReport, evaluate_health


def health_report(root: Path, store, config: dict) -> dict[str, object]:
    """Inspect operational invariants without emitting captured content."""
    root = Path(root)
    try:
        database_ok = not store.path.exists() or store.integrity_check() == "ok"
    except Exception:
        database_ok = False
    spool_files = list((root / "spool").glob("*/*.json")) if (root / "spool").exists() else []
    configured_hooks = config.get("hooks_dir")
    hook_root = Path(configured_hooks) if isinstance(configured_hooks, str) else Path(__file__).resolve().parents[3] / "hooks"
    required_hooks = {"session_start.py", "user_prompt_submit.py", "stop_capture.py", "health_check.py"}
    hook_parity = hook_root.is_dir() and required_hooks.issubset({item.name for item in hook_root.glob("*.py")})
    disk_free = shutil.disk_usage(root if root.exists() else root.parent).free if (root.exists() or root.parent.exists()) else 0
    from imprint.compiler import compiler_lock_state
    lock_state = compiler_lock_state(root)
    migrations_ok = False
    try:
        with store.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='store_schema_version'").fetchone()
            migrations_ok = bool(row and row[0] == "3.0.0")
    except Exception:
        migrations_ok = False
    projection_present = (root / "projections" / "imprint.jsonld").is_file()
    backup_verified = False
    for receipt in (root / "backups").glob("*.receipt.json") if (root / "backups").exists() else ():
        try:
            value = json.loads(receipt.read_text(encoding="utf-8"))
            if value.get("backup_schema_version") == "1.0.0" and value.get("sha256"):
                backup_verified = True
                break
        except (OSError, json.JSONDecodeError):
            continue
    report = evaluate_health(HealthInputs(
        compiler_count=1 if config.get("compiler") else 0,
        database_ok=database_ok,
        migrations_ok=migrations_ok,
        config_ok=True,
        hook_parity_ok=hook_parity,
        spool_depth=len(spool_files),
        quarantine_count=len(list((root / "quarantine").glob("*.json"))) if (root / "quarantine").exists() else 0,
        retrieval_budget_bytes=int(config.get("context_budget_bytes", 32 * 1024)),
        higher_budget_explicit=bool(config.get("allow_higher_budget", False)),
        experimental_enabled=any(config.get("experimental", {}).values()),
        experimental_state="experimental" if any(config.get("experimental", {}).values()) else "disabled",
        projection_snapshot_present=projection_present,
        disk_free_bytes=disk_free,
        stale_lock_count=1 if lock_state.get("stale") else 0,
        backup_verified=backup_verified,
    ))
    result = report.as_dict()
    # CLI contract uses healthy/degraded while the invariant engine remains green/red.
    result["status"] = "healthy" if report.status == "green" else "degraded"
    return result

__all__ = ["HealthInputs", "HealthReport", "evaluate_health", "health_report"]
