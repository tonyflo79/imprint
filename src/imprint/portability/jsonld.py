"""Lossless JSON-LD ledger projection and compatible-store importer."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from imprint.constants import ONTOLOGY_SCHEMA_VERSION, STORE_SCHEMA_VERSION
from imprint.errors import ConflictError, ValidationError
from imprint.ontology.schema import canonical_bytes
from imprint.ontology.contracts import validate_node_contract, validate_relation_contract
from imprint.ontology.references import validate_payload_references
from imprint.projections.jsonld import CONTEXT
from imprint.store import ImprintStore


TABLES = (
    "meta", "events", "nodes", "node_versions", "edges", "edge_versions",
    "source_receipts", "ingest_items", "ingest_rulings", "migrations",
    "consumed_inputs", "projection_state", "purge_receipts",
)
PRIMARY_KEYS = {
    "meta": "key", "events": "event_id", "nodes": "node_id",
    "node_versions": "version_id", "edges": "edge_id",
    "edge_versions": "version_id", "source_receipts": "source_id",
    "ingest_items": "item_id", "ingest_rulings": "ruling_id",
    "migrations": "migration_id", "consumed_inputs": "input_event_id",
    "projection_state": "projection",
    "purge_receipts": "operation_id",
}
TABLE_COLUMNS = {
    "meta": ("key", "value"),
    "events": ("event_id", "event_type", "operator_id", "system_time", "valid_time", "payload_json", "payload_sha256", "prior_event_id", "provenance_status"),
    "nodes": ("node_id", "node_type", "operator_id", "created_event_id"),
    "node_versions": ("version_id", "node_id", "payload_json", "payload_sha256", "provenance_status", "authority_tier", "provenance_json", "evidence_json", "valid_from", "valid_to", "system_from", "system_to", "event_id", "prior_version_id"),
    "edges": ("edge_id", "edge_type", "source_id", "target_id", "operator_id", "created_event_id"),
    "edge_versions": ("version_id", "edge_id", "payload_json", "payload_sha256", "provenance_status", "authority_tier", "provenance_json", "evidence_json", "valid_from", "valid_to", "system_from", "system_to", "event_id", "prior_version_id"),
    "source_receipts": ("source_id", "kind", "locator", "content_sha256", "event_id"),
    "ingest_items": ("item_id", "operator_id", "session_id", "node_id", "source_id", "source_kind", "source_locator", "source_sha256", "payload_json", "payload_sha256", "discovered_at", "status", "kept_node_id"),
    "ingest_rulings": ("ruling_id", "item_id", "verdict", "why", "event_id"),
    "migrations": ("migration_id", "from_version", "to_version", "code_sha256", "applied_at", "backup_receipt", "result_sha256"),
    "consumed_inputs": ("input_event_id", "payload_sha256", "consumed_at", "source_path"),
    "projection_state": ("projection", "snapshot_sha256", "generator_version", "generated_at"),
    "purge_receipts": ("operation_id", "purged_at", "schema_version", "scope_class", "counts_json"),
}


def _rows(store: ImprintStore, table: str) -> list[dict[str, Any]]:
    key = PRIMARY_KEYS[table]
    with store.connect() as conn:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY {key}").fetchall()]


def semantic_digest(document: dict[str, Any]) -> str:
    portable = {
        "schemaVersion": document.get("schemaVersion"),
        "ontologySchemaVersion": document.get("ontologySchemaVersion"),
        "ledger": document.get("imprint:ledger"),
        "graph": document.get("@graph"),
    }
    return hashlib.sha256(canonical_bytes(portable)).hexdigest()


def _graph_from_ledger(ledger: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    graph: list[dict[str, Any]] = []
    nodes = {row["node_id"]: row for row in ledger["nodes"]}
    edges = {row["edge_id"]: row for row in ledger["edges"]}
    for row in ledger["node_versions"]:
        node = nodes[row["node_id"]]
        graph.append({
            "@id": row["version_id"],
            "@type": "imprint:NodeVersion",
            "imprint:entity": {"@id": row["node_id"]},
            "imprint:entityType": node["node_type"],
            "imprint:operator": node["operator_id"],
            "imprint:payload": json.loads(row["payload_json"]),
            "imprint:payloadSha256": row["payload_sha256"],
            "imprint:provenance": row["provenance_status"],
            "imprint:provenanceRecord": json.loads(row["provenance_json"]),
            "imprint:authorityTier": row["authority_tier"],
            "imprint:evidence": json.loads(row["evidence_json"]),
            "imprint:validFrom": row["valid_from"],
            "imprint:validTo": row["valid_to"],
            "imprint:systemFrom": row["system_from"],
            "imprint:systemTo": row["system_to"],
        })
    for row in ledger["edge_versions"]:
        edge = edges[row["edge_id"]]
        graph.append({
            "@id": row["version_id"],
            "@type": "imprint:EdgeVersion",
            "imprint:entity": {"@id": row["edge_id"]},
            "imprint:relationType": edge["edge_type"],
            "imprint:operator": edge["operator_id"],
            "imprint:source": {"@id": edge["source_id"]},
            "imprint:target": {"@id": edge["target_id"]},
            "imprint:payload": json.loads(row["payload_json"]),
            "imprint:payloadSha256": row["payload_sha256"],
            "imprint:provenance": row["provenance_status"],
            "imprint:provenanceRecord": json.loads(row["provenance_json"]),
            "imprint:authorityTier": row["authority_tier"],
            "imprint:evidence": json.loads(row["evidence_json"]),
            "imprint:validFrom": row["valid_from"],
            "imprint:validTo": row["valid_to"],
            "imprint:systemFrom": row["system_from"],
            "imprint:systemTo": row["system_to"],
        })
    graph.sort(key=lambda item: (item["@type"], item["@id"]))
    return graph


def export_jsonld(store: ImprintStore) -> dict[str, Any]:
    """Export every canonical/version/receipt row, including opaque extensions."""
    store.initialize()
    ledger = {table: _rows(store, table) for table in TABLES}
    document = {
        "@context": {**CONTEXT, "ledger": "imprint:ledger"},
        "schemaVersion": STORE_SCHEMA_VERSION,
        "ontologySchemaVersion": ONTOLOGY_SCHEMA_VERSION,
        "@graph": _graph_from_ledger(ledger),
        "imprint:ledger": ledger,
    }
    document["imprint:semanticSha256"] = semantic_digest(document)
    return document


def _assert_payload_hashes(ledger: dict[str, list[dict[str, Any]]]) -> None:
    for table in ("events", "node_versions", "edge_versions", "ingest_items"):
        for row in ledger[table]:
            try:
                payload = json.loads(row["payload_json"])
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ValidationError(f"invalid payload_json in {table}") from exc
            actual = hashlib.sha256(canonical_bytes(payload)).hexdigest()
            if actual != row["payload_sha256"]:
                raise ValidationError(f"payload hash mismatch in {table}")


def _contract_provenance(row: dict[str, Any]) -> dict[str, Any]:
    try:
        stored = json.loads(row["provenance_json"])
        evidence_ids = json.loads(row["evidence_json"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValidationError("invalid semantic provenance or evidence JSON") from exc
    return {
        "status": row["provenance_status"],
        "authority_tier": row["authority_tier"],
        "actor_class": stored.get("actor_class"),
        "actor_id": stored.get("actor_id"),
        "mechanism": stored.get("mechanism"),
        "evidence_ids": evidence_ids,
        "model": stored.get("model"),
        "ratifier_id": stored.get("ratifier"),
    }


def _validate_semantic_rows(
    ledger: dict[str, list[dict[str, Any]]], ontology_version: str,
) -> None:
    """Revalidate typed ledger rows; valid hashes alone cannot grant meaning."""
    events = {row["event_id"]: row for row in ledger["events"]}
    nodes = {row["node_id"]: row for row in ledger["nodes"]}
    edges = {row["edge_id"]: row for row in ledger["edges"]}
    receipts = {row["source_id"]: row for row in ledger["source_receipts"]}
    versions = {row["version_id"]: row for row in ledger["node_versions"]}
    versions_by_node: dict[str, list[dict[str, Any]]] = {}
    for version in ledger["node_versions"]:
        versions_by_node.setdefault(version["node_id"], []).append(version)

    def node_lookup(identifier: str) -> tuple[str, str] | None:
        node = nodes.get(identifier)
        if node:
            return node["node_type"], node["operator_id"]
        receipt = receipts.get(identifier)
        event = events.get(receipt["event_id"]) if receipt else None
        return ("Evidence", event["operator_id"]) if event else None

    def version_lookup(identifier: str) -> tuple[str, str] | None:
        version = versions.get(identifier)
        node = nodes.get(version["node_id"]) if version else None
        return (node["node_id"], node["operator_id"]) if node else None

    typed_nodes: set[str] = set()
    for node_id, node in nodes.items():
        created = events.get(node["created_event_id"])
        if created and str(created["event_type"]).startswith("semantic_"):
            try:
                created_payload = json.loads(created["payload_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValidationError("invalid typed semantic creation event") from exc
            if created_payload.get("ontology_schema_version") != ontology_version:
                raise ValidationError("typed node ontology schema version mismatch")
            if created["operator_id"] != node["operator_id"]:
                raise ValidationError("typed node creation event operator mismatch")
            created_versions = [
                row for row in versions_by_node.get(node_id, [])
                if row["event_id"] == node["created_event_id"]
            ]
            if len(created_versions) != 1:
                raise ValidationError("typed node creation event must create exactly one version")
            created_version = created_versions[0]
            expected_creation = {
                "ontology_schema_version": ontology_version,
                "node_id": node_id, "node_type": node["node_type"],
                "payload": json.loads(created_version["payload_json"]),
                "provenance": _contract_provenance(created_version),
            }
            if created_payload != expected_creation:
                raise ValidationError("typed node creation event does not match its created version")
            typed_nodes.add(node_id)

    for row in ledger["node_versions"]:
        if row["node_id"] not in typed_nodes:
            continue
        node = nodes[row["node_id"]]
        if node["node_type"] == "DirectionScore":
            raise ValidationError("DirectionScore is non-persistent analytical output")
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValidationError("invalid typed semantic node payload") from exc
        contract = validate_node_contract({
            "record_schema_version": ontology_version,
            "node_id": node["node_id"], "node_type": node["node_type"],
            "operator_id": node["operator_id"], "payload": payload,
            "provenance": _contract_provenance(row),
        })
        validate_payload_references(
            node["node_type"],
            contract["payload"],
            operator_id=node["operator_id"],
            provenance_evidence_ids=contract["provenance"]["evidence_ids"],
            node_lookup=node_lookup,
            version_lookup=version_lookup,
        )
        if node["node_type"] in {"Observation", "Outcome"}:
            grant_id = payload.get("consent_grant_id")
            if grant_id is not None:
                observation_system_time = datetime.fromisoformat(
                    row["system_from"].replace("Z", "+00:00")
                )
                active_grants = []
                for grant_version in versions_by_node.get(grant_id, []):
                    system_from = datetime.fromisoformat(
                        grant_version["system_from"].replace("Z", "+00:00")
                    )
                    system_to = (
                        datetime.fromisoformat(grant_version["system_to"].replace("Z", "+00:00"))
                        if grant_version["system_to"] is not None else None
                    )
                    if system_from <= observation_system_time and (
                        system_to is None or observation_system_time < system_to
                    ):
                        active_grants.append(grant_version)
                if len(active_grants) != 1:
                    raise ValidationError("semantic observation lacks one active ConsentGrant version")
                from imprint.ontology.operator import consent_authorizes, validate_operator_payload
                grant_payload = validate_operator_payload(
                    "ConsentGrant", json.loads(active_grants[0]["payload_json"])
                )
                purpose = "outcome_learning" if node["node_type"] == "Outcome" else "behavioral_observation"
                if not consent_authorizes(
                    grant_payload, source_class=payload["source_class"], purpose=purpose,
                    operation="store", at=row["valid_from"],
                ):
                    raise ValidationError("ConsentGrant does not authorize imported semantic observation")

    for row in ledger["edge_versions"]:
        edge = edges[row["edge_id"]]
        created = events.get(edge["created_event_id"])
        if not created or created["event_type"] != "semantic_relation":
            continue
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValidationError("invalid typed semantic relation payload") from exc
        if payload.get("ontology_schema_version") != ontology_version:
            raise ValidationError("typed relation ontology schema version mismatch")
        source = nodes.get(edge["source_id"])
        target = nodes.get(edge["target_id"])
        if not source or not target or created["operator_id"] != edge["operator_id"]:
            raise ValidationError("typed relation has invalid canonical endpoints or operator")
        relation_contract = validate_relation_contract({
            "record_schema_version": ontology_version,
            "relation_id": edge["edge_id"], "relation_type": edge["edge_type"],
            "source_id": edge["source_id"], "source_type": source["node_type"],
            "target_id": edge["target_id"], "target_type": target["node_type"],
            "operator_id": edge["operator_id"], "evidence_mode": payload.get("evidence_mode"),
            "why": payload.get("why"), "provenance": _contract_provenance(row),
        })
        if row["event_id"] == edge["created_event_id"]:
            try:
                creation_payload = json.loads(created["payload_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValidationError("invalid typed relation creation event") from exc
            if creation_payload != relation_contract:
                raise ValidationError("typed relation creation event does not match its created version")


def import_jsonld(store: ImprintStore, document: dict[str, Any], *, dry_run: bool = False) -> str:
    """Import a complete export only into an empty compatible store."""
    if not isinstance(document, dict) or document.get("schemaVersion") != STORE_SCHEMA_VERSION:
        raise ValidationError("incompatible or missing JSON-LD schemaVersion")
    if document.get("ontologySchemaVersion") != ONTOLOGY_SCHEMA_VERSION:
        raise ValidationError("incompatible or missing ontologySchemaVersion")
    ledger = document.get("imprint:ledger")
    if not isinstance(ledger, dict) or set(ledger) != set(TABLES):
        raise ValidationError("JSON-LD ledger is missing or has unknown tables")
    if document.get("imprint:semanticSha256") != semantic_digest(document):
        raise ValidationError("JSON-LD semantic digest mismatch")
    for table in TABLES:
        if not isinstance(ledger[table], list):
            raise ValidationError(f"ledger table {table} must be an array")
    _assert_payload_hashes(ledger)
    _validate_semantic_rows(ledger, document["ontologySchemaVersion"])
    if document.get("@graph") != _graph_from_ledger(ledger):
        raise ValidationError("JSON-LD graph does not match its canonical ledger")
    store.initialize()
    with store.connect() as conn:
        non_meta = sum(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES if table != "meta")
        if non_meta:
            raise ConflictError("JSON-LD import requires an empty compatible store")
        if dry_run:
            return document["imprint:semanticSha256"]
        conn.execute("BEGIN IMMEDIATE")
        for table in TABLES:
            rows = ledger[table]
            if table == "meta":
                conn.execute("DELETE FROM meta")
            for row in rows:
                if not isinstance(row, dict) or set(row) != set(TABLE_COLUMNS[table]):
                    raise ValidationError(f"invalid row in {table}")
                columns = TABLE_COLUMNS[table]
                placeholders = ",".join("?" for _ in columns)
                names = ",".join(columns)
                conn.execute(
                    f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
                    tuple(row[column] for column in columns),
                )
        inserted = {
            table: [dict(row) for row in conn.execute(
                f"SELECT * FROM {table} ORDER BY {PRIMARY_KEYS[table]}"
            ).fetchall()]
            for table in TABLES
        }
        replay = {
            "@context": {**CONTEXT, "ledger": "imprint:ledger"},
            "schemaVersion": STORE_SCHEMA_VERSION,
            "ontologySchemaVersion": ONTOLOGY_SCHEMA_VERSION,
            "@graph": _graph_from_ledger(inserted),
            "imprint:ledger": inserted,
        }
        replay["imprint:semanticSha256"] = semantic_digest(replay)
        if replay["imprint:semanticSha256"] != document["imprint:semanticSha256"]:
            raise ConflictError("imported store semantic digest differs from source")
    return document["imprint:semanticSha256"]
