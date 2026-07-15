from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

import imprint.backup as backup_module
from imprint.backup import create_backup, restore_backup, verify_backup
from imprint.constants import PRODUCT_VERSION
from imprint.errors import ValidationError
from imprint.store import ImprintStore


def _meta_store(path: Path, *, store_version: str = "3.0.0", ontology_version: str | None = "3.1.0") -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO meta VALUES('store_schema_version', ?)", (store_version,))
        if ontology_version is not None:
            connection.execute("INSERT INTO meta VALUES('ontology_schema_version', ?)", (ontology_version,))
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize("ontology_version", ["3.0.0", "999.0.0", None])
def test_store_refuses_noncurrent_or_missing_ontology_without_side_effects(tmp_path, ontology_version):
    path = tmp_path / "imprint.db"
    _meta_store(path, ontology_version=ontology_version)
    before = path.read_bytes()
    with pytest.raises(ValidationError, match="ontology"):
        ImprintStore(path).initialize()
    assert path.read_bytes() == before
    assert not Path(str(path) + "-wal").exists()
    assert not Path(str(path) + "-shm").exists()


def test_store_refuses_existing_wal_or_shm_without_touching_any_bytes(tmp_path):
    path = tmp_path / "imprint.db"
    _meta_store(path)
    wal = Path(str(path) + "-wal")
    shm = Path(str(path) + "-shm")
    wal.write_bytes(b"uncheckpointed-wal-sentinel")
    shm.write_bytes(b"shared-memory-sentinel")
    before = {item: item.read_bytes() for item in (path, wal, shm)}
    with pytest.raises(ValidationError, match="WAL/SHM"):
        ImprintStore(path).initialize()
    assert {item: item.read_bytes() for item in (path, wal, shm)} == before


def test_canonical_provenance_records_product_not_store_schema_version(tmp_path, capture_envelope):
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    with store.connect() as connection:
        versions = connection.execute("SELECT provenance_json FROM node_versions").fetchall()
    assert versions
    assert {json.loads(row[0])["software"]["version"] for row in versions} == {PRODUCT_VERSION}


def _rewrite_receipt_hash(backup: dict) -> None:
    path = Path(backup["path"])
    receipt_path = Path(backup["receipt_path"])
    receipt = json.loads(receipt_path.read_text())
    receipt["bytes"] = path.stat().st_size
    receipt["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n")


def test_backup_receipt_is_closed_and_database_ontology_must_be_current(tmp_path, capture_envelope):
    root = tmp_path / "operator"
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    backup = create_backup(store, root)
    receipt_path = Path(backup["receipt_path"])
    receipt = json.loads(receipt_path.read_text())
    receipt["future_field"] = True
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValidationError, match="unknown or missing"):
        verify_backup(Path(backup["path"]))

    receipt.pop("future_field")
    receipt_path.write_text(json.dumps(receipt))
    with sqlite3.connect(backup["path"]) as connection:
        connection.execute("UPDATE meta SET value='999.0.0' WHERE key='ontology_schema_version'")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    Path(str(backup["path"]) + "-wal").unlink(missing_ok=True)
    Path(str(backup["path"]) + "-shm").unlink(missing_ok=True)
    _rewrite_receipt_hash(backup)
    with pytest.raises(ValidationError, match="ontology"):
        verify_backup(Path(backup["path"]))


def test_restore_validates_staged_copy_before_touching_live_database(
    tmp_path, capture_envelope, monkeypatch,
):
    root = tmp_path / "operator"
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    backup = create_backup(store, root)
    live_before = store.path.read_bytes()
    real_copyfile = shutil.copyfile

    def corrupt_staged(source, destination, *args, **kwargs):
        result = real_copyfile(source, destination, *args, **kwargs)
        if Path(destination).name.startswith(".restore-"):
            Path(destination).write_bytes(b"not-a-database")
        return result

    monkeypatch.setattr(backup_module.shutil, "copyfile", corrupt_staged)
    with pytest.raises(ValidationError, match="corrupt"):
        restore_backup(store, root, Path(backup["path"]), confirmation=Path(backup["path"]).name)
    assert store.path.read_bytes() == live_before


def test_restore_rolls_back_exact_live_bytes_on_post_replace_failure(
    tmp_path, capture_envelope, monkeypatch,
):
    root = tmp_path / "operator"
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    backup = create_backup(store, root)
    store.tombstone_node(capture_envelope["verdict"]["verdict_id"], reason="live mutation")
    live_before = store.path.read_bytes()
    real_secure_file = backup_module.secure_file

    def fail_live_permissions(path):
        if Path(path) == store.path:
            raise OSError("forced post-replace failure")
        return real_secure_file(path)

    monkeypatch.setattr(backup_module, "secure_file", fail_live_permissions)
    with pytest.raises(OSError, match="forced post-replace"):
        restore_backup(store, root, Path(backup["path"]), confirmation=Path(backup["path"]).name)
    assert store.path.read_bytes() == live_before
    assert not list(store.path.parent.glob(".restore-rollback-*.db"))
