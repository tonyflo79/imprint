"""Closed semantic contracts for chosen direction and predicted default direction.

Chosen direction is self-authored authority.  A prediction of what will happen
by default is analysis.  They deliberately use different partitions and write
policies so retrieval cannot silently turn a prediction into an intention (or
an intention into an observation about the operator).
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Iterable

from imprint.errors import ValidationError
from imprint.ontology.schema import require_urn


CHOSEN_PARTITION = "chosen_future"
DEFAULT_PARTITION = "default_future"
COMPARISON_PARTITION = "direction_comparison"

DIRECTION_NODE_TYPES = frozenset({
    "ChosenFuture", "DefaultFuture", "Aim", "TradeOff", "AbandonedWant",
    "DirectionScore",
})

PARTITION_BY_TYPE = {
    "ChosenFuture": CHOSEN_PARTITION,
    "Aim": CHOSEN_PARTITION,
    "TradeOff": CHOSEN_PARTITION,
    "AbandonedWant": CHOSEN_PARTITION,
    "DefaultFuture": DEFAULT_PARTITION,
    "DirectionScore": COMPARISON_PARTITION,
}

_FIELDS = {
    "ChosenFuture": frozenset({"partition", "statement", "authored_at", "effective_from"}),
    "DefaultFuture": frozenset({"partition", "statement", "projected_at", "horizon", "basis_evidence_ids"}),
    "Aim": frozenset({"partition", "statement", "success_criterion", "target_date"}),
    "TradeOff": frozenset({"partition", "chosen", "foregone", "reason"}),
    "AbandonedWant": frozenset({"partition", "statement", "abandoned_at", "reason"}),
    "DirectionScore": frozenset({
        "partition", "candidate_move", "chosen_future_id", "chosen_future_version_id",
        "score", "dimensions", "assessed_at", "evidence_ids",
    }),
}


def _time(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} must include timezone")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value


def _provenance(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("direction provenance must be an object")
    return value


def _require_operator_ratification(provenance: dict[str, Any]) -> None:
    if provenance.get("status") != "ratified":
        raise ValidationError("chosen direction must be operator-ratified")
    if provenance.get("authority_tier") != "ratified_knowledge":
        raise ValidationError("chosen direction requires ratified_knowledge authority")
    if provenance.get("actor_class") != "operator":
        raise ValidationError("chosen direction must be self-authored by the operator")
    actor_id = _text(provenance.get("actor_id"), "provenance.actor_id")
    ratifier = _text(provenance.get("ratifier_id"), "provenance.ratifier_id")
    if actor_id != ratifier:
        raise ValidationError("chosen direction author and ratifier must be the same operator")


def _require_inference(provenance: dict[str, Any], what: str) -> None:
    if provenance.get("status") != "inferred" or provenance.get("authority_tier") != "inferred_candidate":
        raise ValidationError(f"{what} must remain an inferred candidate")
    if provenance.get("actor_class") == "operator":
        raise ValidationError(f"{what} is analysis, not an operator declaration")


def validate_direction_payload(
    node_type: str,
    payload: Any,
    provenance: Any,
) -> dict[str, Any]:
    """Validate one direction payload and its non-negotiable authority policy."""
    if node_type not in DIRECTION_NODE_TYPES:
        raise ValidationError("unsupported direction node type")
    if not isinstance(payload, dict):
        raise ValidationError("direction payload must be an object")
    expected = _FIELDS[node_type]
    unknown = set(payload) - expected
    missing = expected - set(payload)
    if unknown or missing:
        detail = []
        if missing:
            detail.append(f"missing fields: {sorted(missing)}")
        if unknown:
            detail.append(f"unknown fields: {sorted(unknown)}")
        raise ValidationError("; ".join(detail))
    expected_partition = PARTITION_BY_TYPE[node_type]
    if payload.get("partition") != expected_partition:
        raise ValidationError(f"{node_type} must use the {expected_partition} partition")

    prov = _provenance(provenance)
    if expected_partition == CHOSEN_PARTITION:
        _require_operator_ratification(prov)
    else:
        _require_inference(prov, node_type)

    if node_type == "ChosenFuture":
        _text(payload["statement"], "statement")
        _time(payload["authored_at"], "authored_at")
        _time(payload["effective_from"], "effective_from")
    elif node_type == "DefaultFuture":
        _text(payload["statement"], "statement")
        _time(payload["projected_at"], "projected_at")
        _text(payload["horizon"], "horizon")
        _urn_list(payload["basis_evidence_ids"], "basis_evidence_ids", required=True)
    elif node_type == "Aim":
        _text(payload["statement"], "statement")
        _text(payload["success_criterion"], "success_criterion")
        if payload["target_date"] is not None:
            _time(payload["target_date"], "target_date")
    elif node_type == "TradeOff":
        _text(payload["chosen"], "chosen")
        _text(payload["foregone"], "foregone")
        _text(payload["reason"], "reason")
    elif node_type == "AbandonedWant":
        _text(payload["statement"], "statement")
        _time(payload["abandoned_at"], "abandoned_at")
        _text(payload["reason"], "reason")
    else:
        _text(payload["candidate_move"], "candidate_move")
        require_urn(payload["chosen_future_id"], "chosenfuture")
        require_urn(payload["chosen_future_version_id"], "node-version")
        _score(payload["score"], "score")
        if not isinstance(payload["dimensions"], dict) or not payload["dimensions"]:
            raise ValidationError("dimensions must be a non-empty object")
        for name, score in payload["dimensions"].items():
            _text(name, "dimension name")
            _score(score, f"dimensions.{name}")
        _time(payload["assessed_at"], "assessed_at")
        _urn_list(payload["evidence_ids"], "evidence_ids", required=True)
    return deepcopy(payload)


def _score(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ValidationError(f"{field} must be a number from 0 through 1")
    return float(value)


def _urn_list(value: Any, field: str, *, required: bool) -> list[str]:
    if not isinstance(value, list) or (required and not value):
        raise ValidationError(f"{field} must be a non-empty list")
    for item in value:
        require_urn(item)
    if len(value) != len(set(value)):
        raise ValidationError(f"{field} cannot contain duplicates")
    return value


def partition_direction_records(
    records: Iterable[tuple[str, dict[str, Any]]],
) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """Return explicitly labelled partitions; never return a blended record list."""
    result: dict[str, list[tuple[str, dict[str, Any]]]] = {
        CHOSEN_PARTITION: [], DEFAULT_PARTITION: [], COMPARISON_PARTITION: [],
    }
    for node_type, payload in records:
        if node_type not in PARTITION_BY_TYPE or not isinstance(payload, dict):
            raise ValidationError("invalid direction record")
        partition = PARTITION_BY_TYPE[node_type]
        if payload.get("partition") != partition:
            raise ValidationError("direction record partition does not match its type")
        result[partition].append((node_type, deepcopy(payload)))
    return result
