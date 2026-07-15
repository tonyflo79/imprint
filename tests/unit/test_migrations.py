from __future__ import annotations

import pytest

from imprint.backup import create_backup
from imprint.errors import ConflictError, ValidationError
from imprint.portability import Migration, MigrationRunner
from imprint.store import ImprintStore


def migration(**changes):
    values = {
        "migration_id": "3.0.0-add-labels",
        "from_version": "3.0.0",
        "to_version": "3.0.1",
        "statements": ("CREATE TABLE IF NOT EXISTS labels (label_id TEXT PRIMARY KEY, value TEXT NOT NULL)",),
        "backup_receipt": "missing-backup.sqlite3",
    }
    values.update(changes)
    return Migration(**values)


def test_migration_is_additive_atomic_and_idempotent(tmp_path):
    root = tmp_path / "operator"
    store = ImprintStore(root / "imprint.db")
    runner = MigrationRunner(store)
    backup = create_backup(store, root)
    item = migration(backup_receipt=backup["path"])
    assert runner.apply(item) == "applied"
    assert runner.apply(item) == "already-applied"
    with store._migration_connection(store_versions=frozenset({"3.0.1"})) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='store_schema_version'").fetchone()[0] == "3.0.1"
        assert conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 1
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='labels'").fetchone()[0] == "labels"


def test_failed_migration_rolls_back_schema_and_version(tmp_path):
    root = tmp_path / "operator"
    store = ImprintStore(root / "imprint.db")
    runner = MigrationRunner(store)
    backup = create_backup(store, root)
    broken = migration(
        migration_id="broken",
        backup_receipt=backup["path"],
        statements=(
            "CREATE TABLE transient_table (id TEXT PRIMARY KEY)",
            "ALTER TABLE table_that_does_not_exist ADD COLUMN x TEXT",
        ),
    )
    with pytest.raises(Exception):
        runner.apply(broken)
    with store.connect() as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='store_schema_version'").fetchone()[0] == "3.0.0"
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='transient_table'").fetchone() is None
        assert conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 0


def test_migration_rejects_destructive_sql_missing_backup_and_code_reuse(tmp_path):
    root = tmp_path / "operator"
    store = ImprintStore(root / "imprint.db")
    runner = MigrationRunner(store)
    with pytest.raises(ValidationError):
        runner.apply(migration(statements=("DROP TABLE events",)))
    with pytest.raises(ValidationError):
        runner.apply(migration(backup_receipt=""))
    backup = create_backup(store, root)
    runner.apply(migration(backup_receipt=backup["receipt_path"]))
    with pytest.raises(ConflictError):
        runner.apply(migration(to_version="3.0.2", backup_receipt=backup["path"]))


def test_arbitrary_hash_and_unrelated_verified_backup_are_rejected(tmp_path):
    root = tmp_path / "operator"
    store = ImprintStore(root / "imprint.db")
    runner = MigrationRunner(store)
    with pytest.raises(ValidationError, match="does not exist"):
        runner.apply(migration(backup_receipt="sha256:" + "a" * 64))

    other_root = tmp_path / "other"
    other = ImprintStore(other_root / "imprint.db")
    other.initialize()
    with other.connect() as conn:
        conn.execute("INSERT INTO meta(key,value) VALUES('other','content')")
    unrelated = create_backup(other, other_root)
    with pytest.raises(ValidationError, match="exact logical snapshot"):
        runner.apply(migration(backup_receipt=unrelated["path"]))
