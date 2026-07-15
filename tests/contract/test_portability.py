from __future__ import annotations

from copy import deepcopy

import pytest

from imprint.adapters import atlas_documents, generic_graph
from imprint.constants import ONTOLOGY_SCHEMA_VERSION
from imprint.errors import ConflictError, ValidationError
from imprint.ingest import IngestCandidate, IngestService
from imprint.ontology.business import append_business_relationship
from imprint.ontology.schema import make_urn
from imprint.ontology.schema import canonical_bytes, payload_sha256
from imprint.portability import export_jsonld, import_jsonld
from imprint.portability.jsonld import semantic_digest
from imprint.store import ImprintStore


def _business_node(store, capture_envelope, node_type, text):
    evidence_id = capture_envelope["evidence"][0]["evidence_id"]
    operator_id = capture_envelope["operator_id"]
    observed = node_type == "Result"
    primary = {
        "Customer": {"name": text}, "Promise": {"statement": text},
        "Offer": {"name": text}, "Result": {"metric": "result", "value": text, "unit": "label"},
    }[node_type]
    payload = {
        **primary, "evidence_mode": "observed" if observed else "declared",
        "effective_at": capture_envelope["captured_at"],
        "source_refs": [evidence_id], "attributes": {},
    }
    status = "extracted" if observed else "captured"
    actor_id = make_urn("software") if observed else operator_id
    node_id = make_urn(node_type.lower())
    return store.append_semantic_node({
        "record_schema_version": ONTOLOGY_SCHEMA_VERSION,
        "node_id": node_id, "node_type": node_type, "operator_id": operator_id,
        "payload": payload,
        "provenance": {
            "status": status,
            "authority_tier": "observed_candidate" if observed else "captured_judgment",
            "actor_class": "software" if observed else "operator", "actor_id": actor_id,
            "mechanism": "contract_test", "evidence_ids": [evidence_id],
            "model": None, "ratifier_id": None,
        },
    }, valid_from=capture_envelope["captured_at"])


def test_lossless_jsonld_roundtrip_extensions_history_receipts_and_relations(tmp_path, capture_envelope):
    capture_envelope["extensions"] = {
        "org.example.future": {"schema_version": "9.7.0", "payload": {"opaque": [1, {"x": "y"}]}}
    }
    chosen = "urn:imprint:alternative:11111111-1111-4111-8111-111111111111"
    rejected = "urn:imprint:alternative:22222222-2222-4222-8222-222222222222"
    capture_envelope["alternatives"] = [
        {"alternative_id": chosen, "description": "Preserve all provenance", "disposition": "chosen"},
        {"alternative_id": rejected, "description": "Flatten history", "disposition": "rejected"},
    ]
    capture_envelope["verdict"]["chosen_alternative_ids"] = [chosen]
    capture_envelope["verdict"]["rejected_alternative_ids"] = [rejected]
    source = ImprintStore(tmp_path / "source.db")
    source.initialize()
    source.apply_capture(capture_envelope)
    customer = _business_node(source, capture_envelope, "Customer", "Founders")
    promise = _business_node(source, capture_envelope, "Promise", "Complete evidence")
    evidence_id = capture_envelope["evidence"][0]["evidence_id"]
    declared = append_business_relationship(
        source, source_id=customer, target_id=promise, relation_type="declares",
        evidence_mode="declared", evidence_ids=[evidence_id], why="Offer copy says so", actor_id="test",
    )
    observed = append_business_relationship(
        source, source_id=customer, target_id=promise, relation_type="weakens",
        evidence_mode="observed", evidence_ids=[evidence_id], why="Operating evidence differs", actor_id="test",
    )
    service = IngestService(source, capture_envelope["operator_id"])
    item_id = service.scan([IngestCandidate(
        "memory_export", "synthetic://portable", "opaque imported bytes", {},
        {"org.example.unknown": {"schema_version": "1.0.0", "payload": {"future": True}}},
    )])[0]["item_id"]
    service.keep(item_id, why="portable floor evidence")

    document = export_jsonld(source)
    assert all(
        isinstance(item["imprint:provenanceRecord"], dict)
        and item["imprint:provenanceRecord"]["status"] == item["imprint:provenance"]
        for item in document["@graph"]
    )
    target = ImprintStore(tmp_path / "target.db")
    digest = import_jsonld(target, document)
    replay = export_jsonld(target)
    assert replay["imprint:semanticSha256"] == digest == document["imprint:semanticSha256"]
    assert replay["imprint:ledger"] == document["imprint:ledger"]
    raw_capture = next(row for row in replay["imprint:ledger"]["events"] if row["event_type"] == "captured")
    assert "org.example.future" in raw_capture["payload_json"]
    alternatives = {node["payload"]["description"] for node in target.current_nodes(["Alternative"])}
    assert alternatives == {"Preserve all provenance", "Flatten history"}
    edges = {row["edge_id"]: row for row in replay["imprint:ledger"]["edges"]}
    assert edges[declared]["edge_type"] == "declares"
    assert edges[observed]["edge_type"] == "weakens"

    generic = generic_graph(replay)
    atlas = atlas_documents(replay)
    assert len(generic["nodes"]) == len(target.current_nodes())
    assert len(generic["edges"]) == len(target.current_edges())
    assert {item["_id"] for item in atlas["imprint_edges"]} == {item["id"] for item in generic["edges"]}


def test_declared_and_observed_never_merge(tmp_path, capture_envelope):
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    source = _business_node(store, capture_envelope, "Offer", "Advisory")
    target = _business_node(store, capture_envelope, "Result", "Growth")
    evidence = [capture_envelope["evidence"][0]["evidence_id"]]
    append_business_relationship(store, source_id=source, target_id=target, relation_type="declares", evidence_mode="declared", evidence_ids=evidence, why="declared", actor_id="test")
    append_business_relationship(store, source_id=source, target_id=target, relation_type="contradicts", evidence_mode="observed", evidence_ids=evidence, why="observed", actor_id="test")
    modes = [edge["payload"]["evidence_mode"] for edge in store.current_edges() if edge["source_id"] == source]
    assert sorted(modes) == ["declared", "observed"]


def test_jsonld_tampering_and_nonempty_target_fail_without_mutation(tmp_path, capture_envelope):
    source = ImprintStore(tmp_path / "source.db")
    source.initialize()
    source.apply_capture(capture_envelope)
    original = export_jsonld(source)
    tampered = deepcopy(original)
    tampered["imprint:ledger"]["node_versions"][0]["payload_json"] = "{}"
    target = ImprintStore(tmp_path / "target.db")
    target.initialize()
    with pytest.raises(ValidationError):
        import_jsonld(target, tampered)
    assert target.current_nodes() == []
    target.apply_capture(capture_envelope)
    with pytest.raises(ConflictError):
        import_jsonld(target, original)


def test_jsonld_revalidates_typed_rows_and_rolls_back_graph_mismatch(tmp_path, capture_envelope):
    source = ImprintStore(tmp_path / "source.db")
    source.initialize()
    source.apply_capture(capture_envelope)
    customer_id = _business_node(source, capture_envelope, "Customer", "Founders")
    document = export_jsonld(source)

    invalid_payload = deepcopy(document)
    version = next(
        row for row in invalid_payload["imprint:ledger"]["node_versions"]
        if row["node_id"] == customer_id
    )
    version["payload_json"] = canonical_bytes({}).decode()
    version["payload_sha256"] = payload_sha256({})
    graph_item = next(item for item in invalid_payload["@graph"] if item["@id"] == version["version_id"])
    graph_item["imprint:payload"] = {}
    graph_item["imprint:payloadSha256"] = version["payload_sha256"]
    invalid_payload["imprint:semanticSha256"] = semantic_digest(invalid_payload)
    target = ImprintStore(tmp_path / "invalid-payload.db")
    with pytest.raises(ValidationError):
        import_jsonld(target, invalid_payload)
    assert not target.path.exists()

    graph_mismatch = deepcopy(document)
    graph_mismatch["@graph"][0]["imprint:payload"] = {"tampered": True}
    graph_mismatch["imprint:semanticSha256"] = semantic_digest(graph_mismatch)
    target = ImprintStore(tmp_path / "graph-mismatch.db")
    with pytest.raises(ValidationError, match="graph does not match"):
        import_jsonld(target, graph_mismatch)
    assert not target.path.exists()


def test_ratified_business_relationship_requires_endpoint_operator(tmp_path, capture_envelope):
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    source = _business_node(store, capture_envelope, "Offer", "Advisory")
    target = _business_node(store, capture_envelope, "Result", "Growth")
    evidence = [capture_envelope["evidence"][0]["evidence_id"]]
    with pytest.raises(ValidationError, match="authored by the endpoint operator"):
        append_business_relationship(
            store, source_id=source, target_id=target, relation_type="confirms",
            evidence_mode="ratified", evidence_ids=evidence, why="forged authority", actor_id="test",
        )


def test_adapter_requires_lossless_export():
    with pytest.raises(ValidationError):
        generic_graph({"@graph": []})
