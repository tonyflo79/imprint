"""Verified SQLite backups and guarded restore operations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .constants import STORE_SCHEMA_VERSION
from .errors import SafetyError, ValidationError
from .paths import validate_data_root
from .store import ImprintStore


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_backup_path(root: Path, output: Path | None) -> Path:
    root = validate_data_root(root)
    backups = root / "backups"
    target = output or backups / f"imprint-{_stamp()}.sqlite3"
    target = target.expanduser().resolve(strict=False)
    if target == root or target == Path(target.anchor) or target == Path.home().resolve():
        raise SafetyError("backup target must be a file below a safe directory")
    if target.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        raise SafetyError("backup target must end in .db, .sqlite, or .sqlite3")
    validate_data_root(target.parent)
    return target


def create_backup(store: ImprintStore, root: Path, output: Path | None = None) -> dict[str, Any]:
    if not store.path.exists():
        raise ValidationError("canonical database does not exist")
    target = _safe_backup_path(root, output)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise SafetyError("refusing to overwrite an existing backup")
    fd, temporary_name = tempfile.mkstemp(prefix=".backup-", suffix=".sqlite3", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        source = sqlite3.connect(store.path)
        destination = sqlite3.connect(temporary)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        check = sqlite3.connect(temporary)
        try:
            integrity = str(check.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            check.close()
        if integrity != "ok":
            raise ValidationError(f"backup integrity failed: {integrity}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    receipt = {
        "backup_schema_version": "1.0.0",
        "store_schema_version": STORE_SCHEMA_VERSION,
        "file": target.name,
        "sha256": _sha256(target),
        "bytes": target.stat().st_size,
        "integrity": "ok",
    }
    receipt_path = target.with_suffix(target.suffix + ".receipt.json")
    receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    return {**receipt, "path": str(target), "receipt_path": str(receipt_path)}


def verify_backup(path: Path) -> dict[str, Any]:
    target = path.expanduser().resolve(strict=True)
    receipt_path = target.with_suffix(target.suffix + ".receipt.json")
    if not receipt_path.exists():
        raise ValidationError("backup receipt is missing")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    actual_hash = _sha256(target)
    if actual_hash != receipt.get("sha256"):
        raise ValidationError("backup hash does not match receipt")
    connection = sqlite3.connect(target)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        version_row = connection.execute(
            "SELECT value FROM meta WHERE key='store_schema_version'"
        ).fetchone()
    finally:
        connection.close()
    if integrity != "ok":
        raise ValidationError(f"backup integrity failed: {integrity}")
    if not version_row or version_row[0] != receipt.get("store_schema_version"):
        raise ValidationError("backup schema does not match receipt")
    return {
        "status": "verified", "path": str(target), "sha256": actual_hash,
        "integrity": integrity, "store_schema_version": str(version_row[0]),
    }


def restore_backup(store: ImprintStore, root: Path, source: Path, *, confirmation: str) -> dict[str, Any]:
    source = source.expanduser().resolve(strict=True)
    verify_backup(source)
    if confirmation != source.name:
        raise SafetyError("restore confirmation must exactly name the backup file")
    root = validate_data_root(root)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    safety = None
    if store.path.exists():
        safety = create_backup(store, root)
        current = sqlite3.connect(store.path)
        try:
            current.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            current.close()
    fd, temporary_name = tempfile.mkstemp(prefix=".restore-", suffix=".db", dir=store.path.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        origin = sqlite3.connect(source)
        destination = sqlite3.connect(temporary)
        try:
            origin.backup(destination)
        finally:
            destination.close()
            origin.close()
        os.replace(temporary, store.path)
        Path(str(store.path) + "-wal").unlink(missing_ok=True)
        Path(str(store.path) + "-shm").unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)
    restored = ImprintStore(store.path)
    if restored.integrity_check() != "ok":
        raise ValidationError("restored database failed integrity check")
    return {
        "status": "restored",
        "source": str(source),
        "safety_backup": safety["path"] if safety else None,
    }
