"""Verified SQLite backups and guarded restore operations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .constants import ONTOLOGY_SCHEMA_VERSION, STORE_SCHEMA_VERSION
from .errors import SafetyError, ValidationError
from .paths import validate_data_root
from .permissions import secure_directory, secure_file
from .store import ImprintStore


_RECEIPT_FIELDS = {
    "backup_schema_version", "store_schema_version", "file", "sha256",
    "bytes", "integrity",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sidecars(path: Path) -> tuple[Path, Path, Path]:
    return (
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
        Path(str(path) + "-journal"),
    )


def _secure_sqlite_state(path: Path) -> None:
    """Tighten an SQLite file and any private-state sidecars that exist."""
    for candidate in (path, *_sidecars(path)):
        if candidate.exists():
            secure_file(candidate)


def _write_atomic_private(path: Path, payload: str) -> None:
    """Publish UTF-8 text from a pre-secured same-directory temporary file."""
    if path.exists() or path.is_symlink():
        raise SafetyError("refusing to overwrite an existing backup receipt")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        secure_file(temporary)
        temporary.write_text(payload, encoding="utf-8")
        secure_file(temporary)
        os.replace(temporary, path)
        secure_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _inspect_database(path: Path) -> dict[str, str]:
    """Validate a closed standalone database without creating sidecars."""
    if any(sidecar.exists() for sidecar in _sidecars(path)):
        raise ValidationError("database has WAL/SHM/journal sidecars and is not a closed backup")
    try:
        resolved = path.resolve(strict=True)
        if path.is_symlink() or not resolved.is_file():
            raise ValidationError("database must be a regular non-symlink file")
        connection = sqlite3.connect(
            f"{resolved.as_uri()}?mode=ro&immutable=1", uri=True, timeout=5,
        )
        try:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            store_row = connection.execute(
                "SELECT value FROM meta WHERE key='store_schema_version'"
            ).fetchone()
            ontology_row = connection.execute(
                "SELECT value FROM meta WHERE key='ontology_schema_version'"
            ).fetchone()
        finally:
            connection.close()
    except ValidationError:
        raise
    except (OSError, sqlite3.DatabaseError, TypeError) as exc:
        raise ValidationError("database is corrupt or missing schema metadata") from exc
    if integrity != "ok":
        raise ValidationError(f"backup integrity failed: {integrity}")
    if not store_row or store_row[0] != STORE_SCHEMA_VERSION:
        raise ValidationError("backup store schema is incompatible")
    if not ontology_row or ontology_row[0] != ONTOLOGY_SCHEMA_VERSION:
        raise ValidationError("backup ontology schema is incompatible")
    return {
        "integrity": integrity,
        "store_schema_version": str(store_row[0]),
        "ontology_schema_version": str(ontology_row[0]),
    }


def _validate_receipt(receipt: Any, target: Path) -> dict[str, Any]:
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise ValidationError("backup receipt has unknown or missing fields")
    if receipt["backup_schema_version"] != "1.0.0":
        raise ValidationError("unsupported backup receipt schema")
    if receipt["store_schema_version"] != STORE_SCHEMA_VERSION:
        raise ValidationError("backup schema does not match supported store schema")
    if receipt["file"] != target.name or receipt["integrity"] != "ok":
        raise ValidationError("backup receipt identity or integrity claim is invalid")
    if not isinstance(receipt["sha256"], str) or not _SHA256.fullmatch(receipt["sha256"]):
        raise ValidationError("backup receipt hash is invalid")
    if (
        not isinstance(receipt["bytes"], int) or isinstance(receipt["bytes"], bool)
        or receipt["bytes"] <= 0
    ):
        raise ValidationError("backup receipt byte count is invalid")
    return receipt


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
    secure_directory(target.parent)
    if target.exists():
        raise SafetyError("refusing to overwrite an existing backup")
    fd, temporary_name = tempfile.mkstemp(prefix=".backup-", suffix=".sqlite3", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        secure_file(temporary)
        source = sqlite3.connect(store.path)
        destination = sqlite3.connect(temporary)
        try:
            _secure_sqlite_state(store.path)
            _secure_sqlite_state(temporary)
            source.backup(destination)
        finally:
            destination.close()
            source.close()
            _secure_sqlite_state(store.path)
            _secure_sqlite_state(temporary)
        inspected = _inspect_database(temporary)
        secure_file(temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    secure_file(target)
    receipt = {
        "backup_schema_version": "1.0.0",
        "store_schema_version": STORE_SCHEMA_VERSION,
        "file": target.name,
        "sha256": _sha256(target),
        "bytes": target.stat().st_size,
        "integrity": inspected["integrity"],
    }
    receipt_path = target.with_suffix(target.suffix + ".receipt.json")
    _write_atomic_private(receipt_path, json.dumps(receipt, sort_keys=True) + "\n")
    return {**receipt, "path": str(target), "receipt_path": str(receipt_path)}


def verify_backup(path: Path) -> dict[str, Any]:
    supplied = path.expanduser()
    if supplied.is_symlink():
        raise ValidationError("backup must be a regular non-symlink file")
    target = supplied.resolve(strict=True)
    receipt_path = target.with_suffix(target.suffix + ".receipt.json")
    if not receipt_path.exists() or receipt_path.is_symlink():
        raise ValidationError("backup receipt is missing")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("backup receipt is corrupt") from exc
    receipt = _validate_receipt(receipt, target)
    actual_hash = _sha256(target)
    if actual_hash != receipt["sha256"]:
        raise ValidationError("backup hash does not match receipt")
    if receipt["bytes"] != target.stat().st_size:
        raise ValidationError("backup receipt byte count is invalid")
    inspected = _inspect_database(target)
    return {
        "status": "verified", "path": str(target), "sha256": actual_hash,
        "bytes": receipt["bytes"],
        **inspected,
    }


def _require_staged_identity(path: Path, verified: dict[str, Any]) -> None:
    """Bind a staged restore candidate to the exact previously verified bytes."""
    if path.stat().st_size != verified["bytes"] or _sha256(path) != verified["sha256"]:
        raise ValidationError("staged backup bytes do not match verified source")


def restore_backup(store: ImprintStore, root: Path, source: Path, *, confirmation: str) -> dict[str, Any]:
    source = source.expanduser()
    verified = verify_backup(source)
    source = source.resolve(strict=True)
    if confirmation != source.name:
        raise SafetyError("restore confirmation must exactly name the backup file")
    root = validate_data_root(root)
    secure_directory(store.path.parent)
    fd, temporary_name = tempfile.mkstemp(prefix=".restore-", suffix=".db", dir=store.path.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    rollback: Path | None = None
    live_existed = store.path.exists()
    safety = None
    try:
        secure_file(temporary)
        shutil.copyfile(source, temporary)
        secure_file(temporary)
        _inspect_database(temporary)
        _require_staged_identity(temporary, verified)
        if live_existed and any(sidecar.exists() for sidecar in _sidecars(store.path)):
            raise ValidationError("live database has WAL/SHM sidecars; close it before restore")
        if live_existed:
            safety = create_backup(store, root)
            rollback = store.path.with_name(f".restore-rollback-{os.getpid()}-{_stamp()}.db")
            try:
                os.link(store.path, rollback)
            except OSError:
                if rollback.exists() or rollback.is_symlink():
                    raise SafetyError("refusing an existing restore rollback path")
                rollback.touch(exist_ok=False)
                secure_file(rollback)
                shutil.copyfile(store.path, rollback)
            secure_file(rollback)
        # Recheck immediately before replacement so neither source substitution
        # nor staged-file replacement can cross the verified restore boundary.
        _require_staged_identity(temporary, verified)
        os.replace(temporary, store.path)
        try:
            secure_file(store.path)
            _inspect_database(store.path)
        except Exception:
            if rollback is not None:
                os.replace(rollback, store.path)
                secure_file(store.path)
            else:
                store.path.unlink(missing_ok=True)
            raise
    finally:
        temporary.unlink(missing_ok=True)
        if rollback is not None:
            rollback.unlink(missing_ok=True)
        store._compatibility_verified = False
    return {
        "status": "restored",
        "source": str(source),
        "safety_backup": safety["path"] if safety else None,
    }
