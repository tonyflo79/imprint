"""Per-node immutable spools. Compilation never rewrites foreign inputs."""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
import socket
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from imprint.errors import ConflictError, SafetyError
from imprint.capture.schema import validate_capture_envelope
from imprint.ontology.schema import canonical_bytes, payload_sha256
from imprint.permissions import secure_directory, secure_file, secure_tree
from imprint.store import ImprintStore

LOCK_STALE_SECONDS = 300


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _lock_owner_path(operator_root: Path) -> Path:
    return operator_root / "compiler.lock" / "owner.json"


def _write_lock_owner(path: Path, owner: dict[str, Any]) -> None:
    temporary = path.with_name(f".owner-{owner['nonce']}.tmp")
    data = json.dumps(owner, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    with temporary.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    secure_file(path)


def _local_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lease_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("lease timestamp must be UTC")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("lease timestamp must be timezone-aware UTC")
    return parsed


def compiler_lock_state(operator_root: Path) -> dict[str, Any]:
    lock = Path(operator_root) / "compiler.lock"
    owner_path = _lock_owner_path(Path(operator_root))
    if not lock.exists():
        return {"state": "absent", "stale": False}
    try:
        owner = json.loads(owner_path.read_text(encoding="ascii"))
        if not isinstance(owner, dict) or set(owner) != {
            "lock_schema_version", "nonce", "pid", "host", "created_at", "heartbeat_at",
        }:
            raise ValueError("lease fields are invalid")
        created = _lease_timestamp(owner["created_at"])
        heartbeat = _lease_timestamp(owner["heartbeat_at"])
        valid = (
            owner["lock_schema_version"] == "1.0.0"
            and isinstance(owner["nonce"], str) and len(owner["nonce"]) == 32
            and all(ch in "0123456789abcdef" for ch in owner["nonce"])
            and isinstance(owner["pid"], int) and not isinstance(owner["pid"], bool) and owner["pid"] > 0
            and isinstance(owner["host"], str) and bool(owner["host"].strip())
            and owner["host"] == owner["host"].strip() and "\x00" not in owner["host"]
            and heartbeat >= created
        )
        if not valid:
            raise ValueError("lease values are invalid")
        age = max(0, int((datetime.now(timezone.utc) - heartbeat).total_seconds()))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return {"state": "invalid", "stale": True, "age_seconds": -1}
    local_owner = owner["host"] == socket.gethostname()
    pid_alive = _local_pid_alive(owner["pid"]) if local_owner else None
    stale = age >= LOCK_STALE_SECONDS and (not local_owner or pid_alive is False)
    return {"state": "held", "stale": stale,
            "age_seconds": age, "nonce": owner.get("nonce"), "host": owner.get("host"),
            "pid": owner.get("pid"), "local_owner": local_owner, "pid_alive": pid_alive}


def recover_stale_compiler_lock(operator_root: Path, *, confirmation: str) -> dict[str, Any]:
    """Manually recover a stale lease using its exact opaque nonce."""
    root = Path(operator_root)
    state = compiler_lock_state(root)
    if state.get("state") == "absent":
        return {"status": "absent"}
    if not state.get("stale"):
        raise SafetyError("compiler lock is not stale; recovery refused")
    nonce = state.get("nonce")
    if not nonce or confirmation != nonce:
        raise SafetyError("stale lock recovery requires the exact owner nonce")
    owner_path = _lock_owner_path(root)
    owner_path.unlink()
    (root / "compiler.lock").rmdir()
    return {"status": "recovered", "nonce": nonce}


def _quarantine_receipt(operator_root: Path, path: Path, error: Exception) -> None:
    """Record a content-free failure receipt without moving foreign input."""
    relative = path.relative_to(operator_root).as_posix()
    receipt_id = hashlib.sha256(relative.encode("utf-8")).hexdigest()
    directory = operator_root / "quarantine"
    secure_directory(directory)
    final = directory / f"{receipt_id}.json"
    body = json.dumps({
        "quarantine_schema_version": "1.0.0",
        "receipt_id": receipt_id,
        "error_type": type(error).__name__,
        "content_included": False,
    }, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    if final.exists():
        return
    fd, temporary = tempfile.mkstemp(prefix=".quarantine-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, final)
        except FileExistsError:
            pass
    finally:
        Path(temporary).unlink(missing_ok=True)
    if final.exists():
        secure_file(final)


def _ack_path(operator_root: Path, envelope: dict[str, Any]) -> Path:
    node = envelope["node_id"]
    event = envelope["input_event_id"].rsplit(":", 1)[-1]
    return operator_root / "runtime" / "acknowledgements" / node / f"{event}.json"


def _write_acknowledgement(
    operator_root: Path, path: Path, envelope: dict[str, Any], result: str,
) -> None:
    """Persist content-free commit proof; never rewrite the source spool."""
    target = _ack_path(operator_root, envelope)
    secure_directory(target.parent)
    if target.parent.is_symlink():
        raise SafetyError("acknowledgement directory must not be a symlink")
    body = {
        "ack_schema_version": "1.0.0",
        "input_event_id": envelope["input_event_id"],
        "node_id": envelope["node_id"],
        "payload_sha256": payload_sha256(envelope),
        "source_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_path": path.relative_to(operator_root).as_posix(),
        "committed": True,
        "compiler_result": result,
        "acknowledged_at": _now(),
    }
    def matches_prior() -> bool:
        prior = json.loads(target.read_text(encoding="ascii"))
        stable = set(body) - {"acknowledged_at", "compiler_result"}
        return all(prior.get(key) == body[key] for key in stable)

    if target.exists():
        if not matches_prior():
            raise ConflictError("acknowledgement identity conflicts with committed input")
        return
    fd, temporary_name = tempfile.mkstemp(prefix=".ack-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if not matches_prior():
                raise ConflictError("acknowledgement identity conflicts with committed input")
    finally:
        temporary.unlink(missing_ok=True)
    if target.exists():
        secure_file(target)


def prune_acknowledged_spools(
    operator_root: Path, *, source_node_id: str, retention_days: int,
    now: datetime | None = None,
) -> dict[str, int]:
    """Delete only this producer's old spool inputs after exact commit proof."""
    if retention_days < 1:
        raise SafetyError("spool retention must be at least one day")
    if not source_node_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in source_node_id):
        raise SafetyError("source node identity is unsafe")
    root = Path(operator_root)
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise SafetyError("retention clock must be timezone-aware")
    threshold = clock.astimezone(timezone.utc) - timedelta(days=retention_days)
    ack_root = root / "runtime" / "acknowledgements" / source_node_id
    counts = {"deleted": 0, "retained": 0, "invalid": 0}
    for ack_path in sorted(ack_root.glob("*.json")) if ack_root.exists() else ():
        try:
            if ack_path.is_symlink() or not ack_path.is_file():
                raise SafetyError("acknowledgement is not a regular file")
            ack = json.loads(ack_path.read_text(encoding="ascii"))
            required = {
                "ack_schema_version", "input_event_id", "node_id", "payload_sha256",
                "source_file_sha256", "source_path", "committed", "compiler_result",
                "acknowledged_at",
            }
            if set(ack) != required or ack["ack_schema_version"] != "1.0.0" or ack["committed"] is not True:
                raise SafetyError("acknowledgement contract is invalid")
            if ack["node_id"] != source_node_id:
                raise SafetyError("acknowledgement belongs to another producer")
            acknowledged = datetime.fromisoformat(ack["acknowledged_at"].replace("Z", "+00:00"))
            source = (root / ack["source_path"]).resolve(strict=True)
            expected_parent = (root / "spool" / source_node_id).resolve(strict=True)
            if source.parent != expected_parent or source.is_symlink() or not source.is_file():
                raise SafetyError("acknowledgement source escapes its producer spool")
            raw = source.read_bytes()
            value = validate_capture_envelope(json.loads(raw))
            if value["input_event_id"] != ack["input_event_id"]:
                raise SafetyError("acknowledgement event does not match source")
            if payload_sha256(value) != ack["payload_sha256"] or hashlib.sha256(raw).hexdigest() != ack["source_file_sha256"]:
                raise SafetyError("acknowledgement hash does not match source")
            if acknowledged.astimezone(timezone.utc) > threshold:
                counts["retained"] += 1
                continue
            source.unlink()
            counts["deleted"] += 1
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, SafetyError):
            counts["invalid"] += 1
    return counts


def write_envelope(operator_root: Path, envelope: dict[str, Any]) -> Path:
    validate_capture_envelope(envelope)
    node_id = envelope["node_id"]
    event_id = envelope["input_event_id"].rsplit(":", 1)[-1]
    spool = operator_root / "spool" / node_id
    secure_directory(spool)
    final = spool / f"{event_id}.json"
    data = canonical_bytes(envelope) + b"\n"
    if final.exists():
        if final.read_bytes() == data:
            return final
        raise ConflictError("same spool event path contains different bytes")
    fd, temp_name = tempfile.mkstemp(prefix=".imprint-", suffix=".tmp", dir=spool)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, final)
    finally:
        temp.unlink(missing_ok=True)
    secure_file(final)
    return final


def compile_spools(operator_root: Path, store: ImprintStore, *, compiler_authorized: bool) -> dict[str, int]:
    if not compiler_authorized:
        raise SafetyError("canonical mutation requires explicit compiler authority")
    lock = operator_root / "compiler.lock"
    secure_directory(operator_root)
    nonce = uuid.uuid4().hex
    try:
        lock.mkdir()
    except FileExistsError as exc:
        state = compiler_lock_state(operator_root)
        raise SafetyError(f"compiler lock already held; refusing a second writer; state={state['state']} nonce={state.get('nonce','unknown')}") from exc
    try:
        owner = {
            "lock_schema_version": "1.0.0", "nonce": nonce, "pid": os.getpid(),
            "host": socket.gethostname(), "created_at": _now(), "heartbeat_at": _now(),
        }
        _write_lock_owner(_lock_owner_path(operator_root), owner)
        store.initialize()
        inputs = []
        counts = {"captured": 0, "duplicate": 0, "quarantined": 0}
        for path in sorted((operator_root / "spool").glob("*/*.json")):
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(envelope, dict):
                    raise ValueError("spool payload must be an object")
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                _quarantine_receipt(operator_root, path, exc)
                counts["quarantined"] += 1
                continue
            inputs.append((envelope.get("captured_at", ""), envelope.get("input_event_id", ""), path, envelope))
        for _, _, path, envelope in sorted(inputs):
            try:
                result = store.apply_capture(envelope, source_path=path.relative_to(operator_root).as_posix())
                _write_acknowledgement(operator_root, path, envelope, result)
                counts[result] += 1
            except Exception as exc:
                _quarantine_receipt(operator_root, path, exc)
                counts["quarantined"] += 1
            owner["heartbeat_at"] = _now()
            _write_lock_owner(_lock_owner_path(operator_root), owner)
        return counts
    finally:
        try:
            owner_path = _lock_owner_path(operator_root)
            current = json.loads(owner_path.read_text(encoding="ascii"))
            if current.get("nonce") == nonce:
                owner_path.unlink()
                lock.rmdir()
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        secure_tree(operator_root)
