from __future__ import annotations

import json
from pathlib import Path

import pytest

from imprint.backup import create_backup, restore_backup, verify_backup
from imprint.errors import SafetyError, ValidationError
from imprint.lifecycle import feature_status, review_list, review_show, seed_profile
from imprint.ontology.schema import make_urn
from imprint.projections import jsonld_document, markdown_document
from imprint.purge import hard_purge, preview_purge
from imprint.store import ImprintStore


def _derived(store: ImprintStore, *, status: str = "inferred", node_type: str = "Principle") -> str:
    evidence_ids = [item["node_id"] for item in store.current_nodes(["Evidence"])]
    operator_id = store.current_nodes()[0]["operator_id"]
    return store.append_derived_node(
        node_type=node_type,
        payload={"statement": "Expose every material source failure"},
        provenance_status=status,
        authority_tier="inferred_candidate" if status == "inferred" else "observed_candidate",
        evidence_ids=evidence_ids,
        operator_id=operator_id,
        valid_from="2026-07-14T18:00:00Z",
        proposed_by="test-agent",
    )


def test_review_list_show_ratify_and_reject_preserve_history(tmp_path, capture_envelope):
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    ratified_id = _derived(store)
    rejected_id = _derived(store, status="extracted")
    assert {item["node_id"] for item in review_list(store)} == {ratified_id, rejected_id}
    assert review_show(store, ratified_id)["provenance_status"] == "inferred"

    store.ratify_node(ratified_id, ratifier="operator", note="Confirmed")
    reject_event = store.reject_node(rejected_id, rejector="operator", reason="Overgeneralized")

    assert review_list(store) == []
    assert store.current_nodes(["Principle"])[0]["node_id"] == ratified_id
    assert store.current_nodes(["Principle"])[0]["provenance_status"] == "ratified"
    history = store.node_history(rejected_id)
    assert history["versions"][0]["system_to"] is not None
    assert history["dispositions"][0]["event_id"] == reject_event
    assert history["dispositions"][0]["event_type"] == "rejected"
    with pytest.raises(ValidationError, match="not current"):
        review_show(store, rejected_id)


def test_later_why_appends_evidence_and_preserves_original_null(tmp_path, capture_envelope):
    capture_envelope["verdict"]["reason"] = None
    capture_envelope["verdict"]["reason_status"] = "pending"
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    verdict_id = capture_envelope["verdict"]["verdict_id"]

    event_id = store.add_reason(verdict_id, reason="Because omission changes the result", actor_id="operator")
    history = store.node_history(verdict_id)
    assert [version["payload"]["reason"] for version in history["versions"]] == [None, "Because omission changes the result"]
    assert history["versions"][0]["payload"]["reason_status"] == "pending"
    assert history["versions"][1]["payload"]["reason_status"] == "later_added"
    assert len(history["versions"][1]["evidence"]) == len(history["versions"][0]["evidence"]) + 1
    assert any(item["event_id"] == event_id and item["event_type"] == "reason_added" for item in history["dispositions"])
    with pytest.raises(ValidationError, match="already has a reason"):
        store.add_reason(verdict_id, reason="Rewrite it", actor_id="operator")


def test_reinforcement_appends_version_without_changing_call(tmp_path, capture_envelope):
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    verdict_id = capture_envelope["verdict"]["verdict_id"]
    before = store.current_nodes(["Verdict"])[0]
    store.reinforce_verdict(verdict_id, evidence_text="The same failure happened again", actor_id="operator")
    after = store.current_nodes(["Verdict"])[0]
    assert after["payload"] == before["payload"]
    assert len(after["evidence"]) == len(before["evidence"]) + 1
    assert len(store.node_history(verdict_id)["versions"]) == 2


def test_profile_seed_is_inferred_and_cannot_be_load_bearing(tmp_path, capture_envelope):
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    evidence_ids = [item["node_id"] for item in store.current_nodes(["Evidence"])]
    node_id = seed_profile(
        store,
        operator_id=capture_envelope["operator_id"],
        fields={"directness": "high"},
        evidence_ids=evidence_ids,
        valid_from="2026-07-14T18:00:00Z",
    )
    profile = store.current_nodes(["FeedbackProfile"])[0]
    assert profile["node_id"] == node_id
    assert profile["provenance_status"] == "inferred"
    assert profile["authority_tier"] == "inferred_candidate"
    assert profile["payload"]["load_bearing"] is False
    assert profile["payload"]["production_capture_effect"] == "none_until_ratified"
    with pytest.raises(ValidationError, match="cannot be ratified as ontology authority"):
        store.ratify_node(node_id, ratifier="operator")
    with pytest.raises(ValidationError, match="requires cited"):
        seed_profile(
            store, operator_id=make_urn("operator"), fields={"tone": "direct"},
            evidence_ids=[], valid_from="2026-07-14T18:00:00Z",
        )


def test_experimental_status_does_not_claim_scheduler_proof():
    disabled = feature_status({"experimental": {"digest": False, "profile_learning": False}})
    enabled = feature_status({"experimental": {"digest": True, "profile_learning": True}})
    assert {item["status"] for item in disabled.values()} == {"disabled"}
    assert {item["status"] for item in enabled.values()} == {"experimental_unverified"}
    assert all(item["scheduler_proven"] is False and item["load_bearing"] is False for item in enabled.values())


def test_backup_is_verified_tamper_evident_and_restore_is_separately_confirmed(tmp_path, capture_envelope):
    root = tmp_path / "operator"
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    backup = create_backup(store, root)
    assert verify_backup(Path(backup["path"]))["status"] == "verified"

    store.tombstone_node(capture_envelope["verdict"]["verdict_id"], reason="test mutation")
    with pytest.raises(SafetyError, match="exactly name"):
        restore_backup(store, root, Path(backup["path"]), confirmation="YES")
    restored = restore_backup(store, root, Path(backup["path"]), confirmation=Path(backup["path"]).name)
    assert restored["status"] == "restored"
    assert capture_envelope["verdict"]["verdict_id"] in {node["node_id"] for node in store.current_nodes()}
    assert restored["safety_backup"] is not None

    tampered = Path(backup["path"])
    tampered.write_bytes(tampered.read_bytes() + b"tamper")
    with pytest.raises(ValidationError, match="hash"):
        verify_backup(tampered)


def test_hard_purge_requires_exact_confirmation_removes_dependency_content_and_keeps_noncontent_receipt(tmp_path, capture_envelope):
    sentinel = "PRIVATE-SENTINEL-DO-NOT-SURVIVE"
    capture_envelope["case"]["description"] = sentinel
    root = tmp_path / "operator"
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    projection_dir = root / "projections"
    projection_dir.mkdir(parents=True)
    snapshot = store.snapshot()
    (projection_dir / "imprint.md").write_text(markdown_document(snapshot), encoding="utf-8")
    (projection_dir / "imprint.jsonld").write_text(json.dumps(jsonld_document(snapshot)), encoding="utf-8")
    backup = create_backup(store, root)
    scope = capture_envelope["verdict"]["verdict_id"]
    preview = preview_purge(store, root, scope)
    assert preview["counts"]["nodes"] >= 4
    assert preview["confirmation_required"] == scope
    with pytest.raises(SafetyError, match="exactly name"):
        hard_purge(store, root, scope, confirmation="PURGE", sentinel=sentinel)

    result = hard_purge(store, root, scope, confirmation=scope, sentinel=sentinel)
    assert result["status"] == "purged"
    assert result["active_root_scan"] == "clear"
    assert store.current_nodes() == []
    assert store.current_edges() == []
    assert not Path(backup["path"]).exists()
    assert sentinel not in (projection_dir / "imprint.md").read_text(encoding="utf-8")
    assert sentinel.encode() not in (projection_dir / "imprint.jsonld").read_bytes()
    assert sentinel.encode() not in store.path.read_bytes()
    assert not Path(backup["receipt_path"]).exists()
    with store.connect() as conn:
        receipt = dict(conn.execute("SELECT * FROM purge_receipts").fetchone())
        event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert event_count == 0
    assert set(receipt) == {"operation_id", "purged_at", "schema_version", "scope_class", "counts_json"}
    assert sentinel not in json.dumps(receipt)
    assert scope not in json.dumps(receipt)


def test_backup_rejects_unsafe_and_cloud_sync_targets(tmp_path, capture_envelope):
    root = tmp_path / "operator"
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    with pytest.raises(SafetyError):
        create_backup(store, root, Path.home() / "unsafe.sqlite3")
    with pytest.raises(SafetyError, match="Cloud-sync"):
        create_backup(store, root, tmp_path / "Dropbox" / "copy.sqlite3")


@pytest.mark.parametrize("scope_key,expected_class", [
    ("operator_id", "operator"),
    ("session_id", "session"),
    ("source_id", "source"),
])
def test_hard_purge_supports_exact_operator_session_and_source_scopes(
    tmp_path, capture_envelope, scope_key, expected_class
):
    root = tmp_path / expected_class
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    scope = {
        "operator_id": capture_envelope["operator_id"],
        "session_id": capture_envelope["session_id"],
        "source_id": capture_envelope["evidence"][0]["evidence_id"],
    }[scope_key]
    preview = preview_purge(store, root, scope)
    assert preview["scope_class"] == expected_class
    result = hard_purge(store, root, scope, confirmation=scope)
    assert result["status"] == "purged"
    assert result["scope_class"] == expected_class
    assert store.current_nodes() == []
