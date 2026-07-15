"""Additive, idempotent SQLite migration runner."""

from __future__ import annotations

import hashlib
import re
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from imprint.backup import verify_backup
from imprint.constants import ONTOLOGY_SCHEMA_VERSION
from imprint.errors import ConflictError, ValidationError
from imprint.ontology.schema import canonical_bytes
from imprint.store import ImprintStore
from imprint.store.service import utc_now


LEGACY_BUSINESS_NODE_TYPES = frozenset({
    "Customer", "Segment", "Problem", "Desire", "Situation", "Claim",
    "Promise", "Expectation", "Mechanism", "RequiredBehavior", "Offer",
    "Price", "Channel", "Objection", "Proof", "Intervention",
    "SupportAction", "Purchase", "Usage", "Result", "Refund", "Retention",
    "Referral",
})


@dataclass(frozen=True)
class OntologyMigration:
    """A semantic compatibility step which never rewrites preserved prose."""

    migration_id: str
    from_version: str
    to_version: str
    legacy_classification: str = "legacy_untyped"
    auto_converts_legacy: bool = False
    storage_table_changes: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "migration_id": self.migration_id,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "legacy_classification": self.legacy_classification,
            "auto_converts_legacy": self.auto_converts_legacy,
            "storage_table_changes": self.storage_table_changes,
        }


ONTOLOGY_MIGRATION_CATALOG = (
    OntologyMigration(
        migration_id="ontology-3.0.0-to-3.1.0",
        from_version="3.0.0",
        to_version="3.1.0",
    ),
)


def ontology_migration_catalog() -> list[dict[str, Any]]:
    """Return the frozen, built-in semantic compatibility catalog."""
    return [item.as_dict() for item in ONTOLOGY_MIGRATION_CATALOG]


def _ontology_path(from_version: str | None) -> list[OntologyMigration]:
    if from_version is None or from_version == ONTOLOGY_SCHEMA_VERSION:
        return []
    current = from_version
    path: list[OntologyMigration] = []
    visited: set[str] = set()
    while current != ONTOLOGY_SCHEMA_VERSION and current not in visited:
        visited.add(current)
        step = next(
            (item for item in ONTOLOGY_MIGRATION_CATALOG if item.from_version == current),
            None,
        )
        if step is None:
            return []
        path.append(step)
        current = step.to_version
    return path if current == ONTOLOGY_SCHEMA_VERSION else []


def _read_ontology_version(store: ImprintStore) -> str | None:
    """Read without backfilling meta, so a legacy missing value stays visible."""
    if not store.path.exists():
        store.initialize()
    with store.connect() as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if table is None:
            return None
        row = conn.execute(
            "SELECT value FROM meta WHERE key='ontology_schema_version'"
        ).fetchone()
    return str(row[0]) if row else None


def verify_ontology_schema(store: ImprintStore) -> dict[str, Any]:
    """Verify the store's semantic version separately from its SQLite schema."""
    version = _read_ontology_version(store)
    path = _ontology_path(version)
    if version == ONTOLOGY_SCHEMA_VERSION:
        status = "current"
    elif version is None:
        status = "missing"
    elif path:
        status = "migration_available"
    else:
        status = "unsupported"
    return {
        "status": status,
        "compatible": status == "current",
        "store_ontology_schema_version": version,
        "expected_ontology_schema_version": ONTOLOGY_SCHEMA_VERSION,
        "migration_path": [item.as_dict() for item in path],
    }


def _legacy_semantic_records(store: ImprintStore) -> list[dict[str, Any]]:
    """Classify opaque legacy records without interpreting or rewriting them."""
    if not store.path.exists():
        return []
    with store.connect() as conn:
        tables = {
            str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {"nodes", "node_versions", "events"}
        if not required.issubset(tables):
            return []
        rows = conn.execute(
            """
            SELECT n.node_id,n.node_type,n.created_event_id,e.event_type,
                   nv.version_id
              FROM nodes n
              JOIN events e ON e.event_id=n.created_event_id
              LEFT JOIN node_versions nv
                ON nv.node_id=n.node_id AND nv.system_to IS NULL
             ORDER BY n.node_type,n.node_id
            """
        ).fetchall()

    classified = []
    for row in rows:
        node_type = str(row["node_type"])
        event_type = str(row["event_type"])
        if node_type == "FeedbackProfile":
            reason = "opaque_feedback_profile"
        elif node_type in LEGACY_BUSINESS_NODE_TYPES and event_type != "semantic_node":
            reason = "opaque_business_record"
        else:
            continue
        classified.append({
            "node_id": row["node_id"],
            "version_id": row["version_id"],
            "node_type": node_type,
            "classification": "legacy_untyped",
            "reason": reason,
            "auto_conversion": "forbidden",
            "required_action": "preserve verbatim; create new typed assertions only through evidence-backed review",
        })
    return classified


def ontology_migration_report(store: ImprintStore) -> dict[str, Any]:
    """Report version compatibility and opaque legacy records; mutate nothing."""
    verification = verify_ontology_schema(store)
    legacy = _legacy_semantic_records(store)
    return {
        "status": verification["status"],
        "verification": verification,
        "catalog": ontology_migration_catalog(),
        "legacy_policy": {
            "classification": "legacy_untyped",
            "auto_convert_profile_prose": False,
            "auto_convert_business_prose": False,
            "preserve_original_bytes": True,
        },
        "legacy_untyped_count": len(legacy),
        "legacy_untyped_records": legacy,
    }


@dataclass(frozen=True)
class Migration:
    migration_id: str
    from_version: str
    to_version: str
    statements: tuple[str, ...]
    backup_receipt: str

    @property
    def code_sha256(self) -> str:
        return hashlib.sha256(canonical_bytes({
            "id": self.migration_id,
            "from": self.from_version,
            "to": self.to_version,
            "statements": self.statements,
        })).hexdigest()


class MigrationRunner:
    def __init__(self, store: ImprintStore):
        self.store = store
        self.store.initialize()

    def apply(self, migration: Migration) -> str:
        if not migration.migration_id.strip() or not migration.statements:
            raise ValidationError("migration ID and statements are required")
        additive = re.compile(
            r"^(?:CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\b|ALTER\s+TABLE\s+\S+\s+ADD\s+COLUMN\b)",
            re.IGNORECASE,
        )
        if any(not additive.match(statement.lstrip()) for statement in migration.statements):
            raise ValidationError("migration contains a non-additive statement")
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute("SELECT * FROM migrations WHERE migration_id=?", (migration.migration_id,)).fetchone()
            if prior:
                if prior["code_sha256"] != migration.code_sha256:
                    raise ConflictError("migration ID was reused with different code")
                return "already-applied"
            current = conn.execute("SELECT value FROM meta WHERE key='store_schema_version'").fetchone()[0]
            if current != migration.from_version:
                raise ConflictError(f"migration expects {migration.from_version}, store is {current}")
            backup_path = _backup_path(migration.backup_receipt)
            verified = verify_backup(backup_path)
            if verified["store_schema_version"] != migration.from_version:
                raise ValidationError("verified backup schema does not match migration from_version")
            backup_conn = sqlite3.connect(backup_path)
            try:
                backup_digest = _logical_digest(backup_conn)
            finally:
                backup_conn.close()
            if backup_digest != _logical_digest(conn):
                raise ValidationError("verified backup is not an exact logical snapshot of this store")
            canonical_receipt = f"sha256:{verified['sha256']}"
            for statement in migration.statements:
                conn.execute(statement)
            conn.execute("UPDATE meta SET value=? WHERE key='store_schema_version'", (migration.to_version,))
            schema_rows = [tuple(row) for row in conn.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
            ).fetchall()]
            result_hash = hashlib.sha256(canonical_bytes(schema_rows)).hexdigest()
            conn.execute(
                "INSERT INTO migrations VALUES(?,?,?,?,?,?,?)",
                (migration.migration_id, migration.from_version, migration.to_version,
                 migration.code_sha256, utc_now(), canonical_receipt, result_hash),
            )
        return "applied"


def _backup_path(receipt_or_path: str) -> Path:
    if not isinstance(receipt_or_path, str) or not receipt_or_path.strip():
        raise ValidationError("migration requires a verified backup path or receipt path")
    supplied = Path(receipt_or_path).expanduser()
    if supplied.name.endswith(".receipt.json"):
        try:
            receipt = json.loads(supplied.resolve(strict=True).read_text(encoding="utf-8"))
            supplied = supplied.parent / receipt["file"]
        except (OSError, KeyError, json.JSONDecodeError) as exc:
            raise ValidationError("migration backup receipt path is invalid") from exc
    try:
        return supplied.resolve(strict=True)
    except OSError as exc:
        raise ValidationError("migration backup path does not exist") from exc


def _logical_digest(conn: sqlite3.Connection) -> str:
    tables = [
        str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    snapshot = []
    for table in tables:
        columns = [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
        rows = []
        for row in conn.execute(f'SELECT * FROM "{table}"').fetchall():
            values = [value.hex() if isinstance(value, bytes) else value for value in row]
            rows.append(values)
        rows.sort(key=canonical_bytes)
        snapshot.append({"table": table, "columns": columns, "rows": rows})
    return hashlib.sha256(canonical_bytes(snapshot)).hexdigest()
