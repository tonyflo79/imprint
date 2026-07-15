from __future__ import annotations

import json
import os
import socket
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from imprint.compiler.spool import compiler_lock_state
from imprint.config import load_config
from imprint.errors import ValidationError
from imprint.store import ImprintStore


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("compiler", "false", "compiler must be a boolean"),
        ("allow_higher_budget", 1, "allow_higher_budget must be a boolean"),
        ("context_budget_bytes", True, "context_budget_bytes"),
        ("spool_retention_days", False, "spool_retention_days"),
        ("operator_slug", "Unsafe/Path", "operator_slug"),
        ("node_id", ["primary"], "node_id"),
        ("domains", {}, "domains must be an array"),
        ("domains", [{"domain_id": "sales", "public_label": 7}], "public_label"),
        ("domains", [{"domain_id": "sales", "public_label": "Sales", "frozen": "false"}], "frozen"),
        ("experimental", {"digest": "false", "profile_learning": False}, "booleans"),
        ("experimental", {"digest": False, "profile_learning": False, "future": False}, "exactly"),
        ("data_root", 7, "data_root"),
        ("hooks_dir", "", "hooks_dir"),
        ("hooks_dir", "relative/hooks", "absolute"),
    ],
)
def test_complete_config_rejects_type_confusion(tmp_path, field, value, message):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({field: value}))
    with pytest.raises(ValidationError, match=message):
        load_config(path)


def _owner(*, pid: int, host: str, created_at: str, heartbeat_at: str) -> dict:
    return {
        "lock_schema_version": "1.0.0",
        "nonce": "a" * 32,
        "pid": pid,
        "host": host,
        "created_at": created_at,
        "heartbeat_at": heartbeat_at,
    }


def test_fresh_heartbeat_protects_old_lease(tmp_path):
    lock = tmp_path / "compiler.lock"
    lock.mkdir()
    now = datetime.now(timezone.utc)
    (lock / "owner.json").write_text(json.dumps(_owner(
        pid=99999999,
        host=socket.gethostname(),
        created_at=(now - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        heartbeat_at=now.isoformat().replace("+00:00", "Z"),
    )))
    state = compiler_lock_state(tmp_path)
    assert state["state"] == "held" and state["stale"] is False
    assert state["pid_alive"] is False


def test_stale_heartbeat_requires_dead_local_pid(tmp_path):
    lock = tmp_path / "compiler.lock"
    lock.mkdir()
    old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    owner_path = lock / "owner.json"
    owner_path.write_text(json.dumps(_owner(
        pid=os.getpid(), host=socket.gethostname(), created_at=old, heartbeat_at=old,
    )))
    assert compiler_lock_state(tmp_path)["stale"] is False
    owner_path.write_text(json.dumps(_owner(
        pid=99999999, host=socket.gethostname(), created_at=old, heartbeat_at=old,
    )))
    state = compiler_lock_state(tmp_path)
    assert state["stale"] is True and state["pid_alive"] is False


def _make_store(path, version: str | None) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        if version is not None:
            conn.execute("INSERT INTO meta VALUES('store_schema_version', ?)", (version,))
        conn.execute("INSERT INTO meta VALUES('ontology_schema_version', '3.1.0')")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("version", ["2.0.0", None, "999.0.0"])
def test_incompatible_existing_store_is_byte_identical_after_refusal(tmp_path, version):
    path = tmp_path / "imprint.db"
    _make_store(path, version)
    before = path.read_bytes()
    with pytest.raises(ValidationError):
        ImprintStore(path).initialize()
    assert path.read_bytes() == before
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-shm").exists()


def test_future_store_refuses_ordinary_connection_before_write(tmp_path):
    path = tmp_path / "imprint.db"
    _make_store(path, "999.0.0")
    before = path.read_bytes()
    with pytest.raises(ValidationError, match="incompatible"):
        with ImprintStore(path).connect():
            pass
    assert path.read_bytes() == before


def test_absent_store_requires_initialize_and_is_not_created_by_connect(tmp_path):
    path = tmp_path / "imprint.db"
    with pytest.raises(ValidationError, match="initialized"):
        with ImprintStore(path).connect():
            pass
    assert not path.exists()


def test_corrupt_existing_store_is_byte_identical_after_refusal(tmp_path):
    path = tmp_path / "imprint.db"
    path.write_bytes(b"not a sqlite database")
    before = path.read_bytes()
    with pytest.raises(ValidationError, match="corrupt"):
        ImprintStore(path).initialize()
    assert path.read_bytes() == before
