"""Declared-versus-observed business ontology relationships."""

from __future__ import annotations

import json
from typing import Any

from imprint.errors import ValidationError
from imprint.ontology.schema import canonical_bytes, make_urn, payload_sha256
from imprint.store import ImprintStore
from imprint.store.service import utc_now, version_provenance


BUSINESS_NODE_TYPES = frozenset({
    "Customer", "Segment", "Problem", "Desire", "Situation", "Claim",
    "Promise", "Expectation", "Mechanism", "RequiredBehavior", "Offer",
    "Price", "Channel", "Objection", "Proof", "Intervention",
    "SupportAction", "Purchase", "Usage", "Result", "Refund", "Retention",
    "Referral",
})
EVIDENCE_MODES = frozenset({"declared", "observed", "inferred", "ratified"})
RELATION_TYPES = frozenset({
    "declares", "observes", "supported_by", "confirms", "weakens",
    "contradicts", "extends",
})


def append_business_relationship(
    store: ImprintStore,
    *,
    source_id: str,
    target_id: str,
    relation_type: str,
    evidence_mode: str,
    evidence_ids: list[str],
    why: str,
    actor_id: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Append one evidence-linked relation without merging other evidence modes."""
    if relation_type not in RELATION_TYPES:
        raise ValidationError("unsupported business relation type")
    if evidence_mode not in EVIDENCE_MODES:
        raise ValidationError("unsupported evidence_mode")
    if not evidence_ids:
        raise ValidationError("business relationships require evidence")
    if not isinstance(why, str) or not why.strip():
        raise ValidationError("relationship WHY is required")
    if not isinstance(metadata or {}, dict):
        raise ValidationError("metadata must be an object")
    now = utc_now()
    event_id = make_urn("event")
    edge_id = make_urn("edge")
    provenance = "inferred" if evidence_mode == "inferred" else "ratified" if evidence_mode == "ratified" else "extracted"
    authority = "inferred_candidate" if evidence_mode == "inferred" else "ratified_knowledge" if evidence_mode == "ratified" else "imported_floor"
    payload = {
        "relation": relation_type,
        "evidence_mode": evidence_mode,
        "why": why,
        "metadata": metadata or {},
    }
    event_payload = {
        "edge_id": edge_id, "source_id": source_id, "target_id": target_id,
        "payload": payload, "evidence_ids": evidence_ids, "actor_id": actor_id,
    }
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        endpoints = conn.execute(
            "SELECT node_id,node_type,operator_id FROM nodes WHERE node_id IN (?,?)",
            (source_id, target_id),
        ).fetchall()
        if len(endpoints) != 2:
            raise ValidationError("business relationship endpoints must exist")
        if any(row["node_type"] not in BUSINESS_NODE_TYPES for row in endpoints):
            raise ValidationError("business relationship endpoint has a non-business node type")
        known_evidence = 0
        for evidence_id in set(evidence_ids):
            if conn.execute("SELECT 1 FROM source_receipts WHERE source_id=?", (evidence_id,)).fetchone():
                known_evidence += 1
            elif conn.execute("SELECT 1 FROM nodes WHERE node_id=? AND node_type='Evidence'", (evidence_id,)).fetchone():
                known_evidence += 1
        if known_evidence != len(set(evidence_ids)):
            raise ValidationError("relationship evidence must reference Evidence nodes or source receipts")
        operator_ids = {row["operator_id"] for row in endpoints}
        if len(operator_ids) != 1:
            raise ValidationError("cross-operator relationship is forbidden")
        operator_id = operator_ids.pop()
        conn.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
            (event_id, "derived", operator_id, now, now,
             canonical_bytes(event_payload).decode(), payload_sha256(event_payload), None, provenance),
        )
        conn.execute(
            "INSERT INTO edges VALUES(?,?,?,?,?,?)",
            (edge_id, relation_type, source_id, target_id, operator_id, event_id),
        )
        conn.execute(
            "INSERT INTO edge_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (make_urn("edge-version"), edge_id, canonical_bytes(payload).decode(), payload_sha256(payload),
             provenance, authority, canonical_bytes(version_provenance(
                 status=provenance, authority_tier=authority,
                 actor_class="operator" if evidence_mode in {"declared", "ratified"} else "software",
                 actor_id=actor_id, mechanism=f"business_{evidence_mode}", event_id=event_id,
                 ratifier=actor_id if evidence_mode == "ratified" else None, relation=relation_type,
             )).decode(), json.dumps(evidence_ids), now, None, now, None, event_id, None),
        )
    return edge_id
