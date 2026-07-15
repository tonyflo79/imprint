import pytest

from imprint.constants import ONTOLOGY_SCHEMA_VERSION
from imprint.errors import ValidationError
from imprint.ontology.direction import partition_direction_records, validate_direction_payload
from imprint.ontology.schema import make_urn
from imprint.store import ImprintStore


RATIFIED = {
    "status": "ratified", "authority_tier": "ratified_knowledge",
    "actor_class": "operator", "actor_id": "rich", "ratifier_id": "rich",
}
INFERRED = {
    "status": "inferred", "authority_tier": "inferred_candidate",
    "actor_class": "model", "actor_id": "model-a", "ratifier_id": None,
}


def chosen():
    return {
        "partition": "chosen_future", "statement": "Build the judgment infrastructure.",
        "authored_at": "2026-07-14T20:00:00Z", "effective_from": "2026-07-14T20:00:00Z",
    }


def test_chosen_future_is_self_authored_and_ratified_only():
    assert validate_direction_payload("ChosenFuture", chosen(), RATIFIED) == chosen()
    with pytest.raises(ValidationError, match="operator-ratified"):
        validate_direction_payload("ChosenFuture", chosen(), INFERRED)
    delegated = dict(RATIFIED, actor_id="delegate")
    with pytest.raises(ValidationError, match="same operator"):
        validate_direction_payload("ChosenFuture", chosen(), delegated)


def test_default_future_is_inferred_dated_and_separately_partitioned():
    value = {
        "partition": "default_future", "statement": "Fragmentation continues.",
        "projected_at": "2026-07-14T20:00:00Z", "horizon": "90 days",
        "basis_evidence_ids": [make_urn("evidence")],
    }
    assert validate_direction_payload("DefaultFuture", value, INFERRED) == value
    with pytest.raises(ValidationError, match="inferred candidate"):
        validate_direction_payload("DefaultFuture", value, RATIFIED)
    with pytest.raises(ValidationError, match="default_future partition"):
        validate_direction_payload("DefaultFuture", dict(value, partition="chosen_future"), INFERRED)


def test_direction_score_names_candidate_and_exact_chosen_future_version():
    value = {
        "partition": "direction_comparison", "candidate_move": "Publish Imprint v3",
        "chosen_future_id": make_urn("chosenfuture"),
        "chosen_future_version_id": make_urn("node-version"), "score": 0.82,
        "dimensions": {"alignment": 0.9, "cost": 0.7},
        "assessed_at": "2026-07-14T20:00:00Z", "evidence_ids": [make_urn("evidence")],
    }
    assert validate_direction_payload("DirectionScore", value, INFERRED) == value
    with pytest.raises(ValidationError, match="node-version"):
        validate_direction_payload("DirectionScore", dict(value, chosen_future_version_id=make_urn("chosenfuture")), INFERRED)


def test_direction_score_is_validatable_but_never_persisted(tmp_path):
    operator_id = make_urn("operator")
    evidence_id = make_urn("evidence")
    payload = {
        "partition": "direction_comparison", "candidate_move": "Publish Imprint v3",
        "chosen_future_id": make_urn("chosenfuture"),
        "chosen_future_version_id": make_urn("node-version"), "score": 0.82,
        "dimensions": {"alignment": 0.9}, "assessed_at": "2026-07-14T20:00:00Z",
        "evidence_ids": [evidence_id],
    }
    contract = {
        "record_schema_version": ONTOLOGY_SCHEMA_VERSION,
        "node_id": make_urn("directionscore"), "node_type": "DirectionScore",
        "operator_id": operator_id, "payload": payload,
        "provenance": {
            "status": "inferred", "authority_tier": "inferred_candidate",
            "actor_class": "model", "actor_id": make_urn("model"),
            "mechanism": "analytical_comparison", "evidence_ids": [evidence_id],
            "model": "synthetic-model", "ratifier_id": None,
        },
    }
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    with pytest.raises(ValidationError, match="cannot be persisted"):
        store.append_semantic_node(contract, valid_from="2026-07-14T20:00:00Z")
    assert store.current_nodes() == []


def test_partition_api_never_returns_an_unlabelled_blend():
    default = {
        "partition": "default_future", "statement": "x", "projected_at": "2026-07-14T20:00:00Z",
        "horizon": "soon", "basis_evidence_ids": [make_urn("evidence")],
    }
    parts = partition_direction_records([("ChosenFuture", chosen()), ("DefaultFuture", default)])
    assert len(parts["chosen_future"]) == 1
    assert len(parts["default_future"]) == 1
    assert parts["direction_comparison"] == []
