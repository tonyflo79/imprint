from __future__ import annotations

import json
from copy import deepcopy

import pytest

from imprint.capture.detector import FeedbackDetection
from imprint.cli import main
from imprint.derive.proposals import route_capture_to_proposal
from imprint.derive.spool import ProposalSpoolWriter, compile_pending_proposals
from imprint.errors import ConflictError, ValidationError
from imprint.store import ImprintStore


def _proposal(envelope):
    return route_capture_to_proposal(
        envelope,
        FeedbackDetection(True, "correction", "correct", "explicit", 1.0),
    )


def test_immutable_spool_and_canonical_writer_are_idempotent(tmp_path, capture_envelope):
    root = tmp_path / "operator"
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    proposal = _proposal(capture_envelope)
    writer = ProposalSpoolWriter(root)

    assert writer.submit_proposal(proposal) == proposal["proposal_id"]
    assert writer.submit_proposal(proposal) == proposal["proposal_id"]
    pending = list((root / "proposal-spool" / "pending").glob("*.json"))
    assert len(pending) == 1
    altered = deepcopy(proposal)
    altered["payload"]["call_type"] = "prefer"
    with pytest.raises(ConflictError):
        writer.submit_proposal(altered)

    first = compile_pending_proposals(root, store)
    assert first == {"applied": 1, "duplicates": 0, "rejected": 0, "skipped": 0, "failures": []}
    second = compile_pending_proposals(root, store)
    assert second == {"applied": 0, "duplicates": 0, "rejected": 0, "skipped": 1, "failures": []}
    node = store.current_nodes(["Proposal"])[0]
    assert node["node_id"] == proposal["proposal_id"]
    assert node["provenance_status"] == "extracted"
    assert node["authority_tier"] == "inferred_candidate"
    assert node["provenance"]["proposal_id"] == proposal["proposal_id"]
    assert node["evidence"] == proposal["references"]["evidence_ids"]
    with pytest.raises(ValidationError, match="cannot be ratified"):
        store.ratify_node(node["node_id"], ratifier="operator")


def test_writer_rejects_cross_event_references_and_records_content_free_failure(tmp_path, capture_envelope):
    root = tmp_path / "operator"
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    proposal = _proposal(capture_envelope)
    proposal["references"]["case_id"] = "urn:imprint:case:00000000-0000-4000-8000-000000000001"
    ProposalSpoolWriter(root).submit_proposal(proposal)

    result = compile_pending_proposals(root, store)
    assert result["rejected"] == 1
    assert result["failures"][0]["error_type"] == "ValidationError"
    assert store.current_nodes(["Proposal"]) == []
    receipt = json.loads(next((root / "proposal-spool" / "receipts").glob("*.json")).read_text())
    assert receipt["status"] == "rejected"
    assert "payload" not in receipt and "evidence" not in receipt


def test_cli_submit_and_derive_pending(tmp_path, capsys, capture_envelope):
    data = tmp_path / "data"
    root = data / "test-operator"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "operator_slug": "test-operator", "data_root": str(data),
        "node_id": capture_envelope["node_id"], "compiler": True,
    }))
    identity = root / "identity.json"
    identity.parent.mkdir(parents=True)
    identity.write_text(json.dumps({"identity_schema_version": "1.0.0", "operator_id": capture_envelope["operator_id"]}))
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(json.dumps(_proposal(capture_envelope)))

    assert main(["--config", str(config), "derive", "--submit", str(proposal_path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "queued"
    assert main(["--config", str(config), "derive", "--pending"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ok" and result["applied"] == 1
