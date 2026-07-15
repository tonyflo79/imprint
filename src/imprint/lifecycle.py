"""Operator-controlled review, history, and experimental-state services."""

from __future__ import annotations

from typing import Any

from .errors import ValidationError
from .store import ImprintStore


REVIEWABLE = {"inferred", "extracted"}


def review_list(store: ImprintStore) -> list[dict[str, Any]]:
    """Return proposals awaiting an explicit operator disposition."""
    return [
        node for node in store.current_nodes()
        if node["provenance_status"] in REVIEWABLE
    ]


def review_show(store: ImprintStore, node_id: str) -> dict[str, Any]:
    matches = [node for node in store.current_nodes() if node["node_id"] == node_id]
    if not matches:
        raise ValidationError("review object is missing or not current")
    node = matches[0]
    if node["provenance_status"] not in REVIEWABLE:
        raise ValidationError("object is not awaiting review")
    return node


def feature_status(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Never infer scheduler health: disabled flags are the only production truth."""
    configured = config.get("experimental", {})
    if not isinstance(configured, dict):
        configured = {}
    return {
        name: {
            "classification": "experimental",
            "enabled": bool(configured.get(name, False)),
            "status": "experimental_unverified" if configured.get(name, False) else "disabled",
            "scheduler_proven": False,
            "load_bearing": False,
        }
        for name in ("digest", "profile_learning")
    }


def seed_profile(
    store: ImprintStore,
    *,
    operator_id: str,
    fields: dict[str, Any],
    evidence_ids: list[str],
    valid_from: str,
) -> str:
    """Store onboarding output as an inferred, non-load-bearing proposal."""
    if not fields or not isinstance(fields, dict):
        raise ValidationError("profile seed fields must be a non-empty object")
    if not evidence_ids:
        raise ValidationError("every profile seed requires cited feedback evidence")
    payload = {
        "profile_schema_version": "3.0.0",
        "fields": fields,
        "load_bearing": False,
        "production_capture_effect": "none_until_ratified",
    }
    return store.append_derived_node(
        node_type="FeedbackProfile",
        payload=payload,
        provenance_status="inferred",
        authority_tier="inferred_candidate",
        evidence_ids=evidence_ids,
        operator_id=operator_id,
        valid_from=valid_from,
        proposed_by="onboarding-profile-seed",
    )
