"""The retrieval payload must carry a judgment's referent (its Case summary).

A captured judgment ("The headline should be shorter.") is meaningless without
the thing it judged. Before this fix the render emitted only opaque Case URNs,
so judgments surfaced as deictic fragments. These tests pin that the Case
summary now reaches the render boundary, both as a plain record field and end to
end from canonical store state.
"""

from __future__ import annotations

import json

from imprint.retrieve import RetrievalEngine, StoreRetrievalSource
from imprint.retrieve.models import RetrievalRecord
from imprint.store import ImprintStore


class _Source:
    def __init__(self, records):
        self.records = records

    def retrieval_candidates(self, snapshot_id):
        assert snapshot_id
        return tuple(self.records)


def test_render_carries_case_summary_referent():
    record = RetrievalRecord(
        record_id="verdict-1",
        text="The headline should be shorter.",
        section="general",
        provenance_status="captured",
        authority_tier="captured_judgment",
        evidence_ids=("e-1",),
        case_ids=("case-1",),
        case_summaries=("Draft VSL headline v3",),
        provenance_complete=True,
    )
    result = RetrievalEngine(_Source([record])).retrieve(snapshot_id="s")
    line = json.loads(result.payload.splitlines()[0])
    assert line["case_summaries"] == ["Draft VSL headline v3"]
    # The human anchor is literally present, not just referenced by URN.
    assert "Draft VSL headline v3" in result.payload.decode("utf-8")


def test_render_always_emits_case_summaries_key_deterministically():
    # A referent-less record (ratified knowledge) still emits the key, so the
    # additive field keeps the per-record shape uniform and deterministic.
    record = RetrievalRecord(
        record_id="belief-1",
        text="Ship weekly.",
        section="general",
        provenance_status="ratified",
        authority_tier="ratified_knowledge",
        evidence_ids=("e-1",),
        case_ids=(),
        case_summaries=(),
        provenance_complete=True,
    )
    result = RetrievalEngine(_Source([record])).retrieve(snapshot_id="s")
    line = json.loads(result.payload.splitlines()[0])
    assert line["case_summaries"] == []


def test_store_source_projects_case_description_as_referent(tmp_path, capture_envelope):
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)

    records = StoreRetrievalSource(store).retrieval_candidates("snapshot")
    verdicts = [item for item in records if item.ontology_type == "Verdict"]
    assert verdicts, "expected a captured Verdict candidate"
    verdict = verdicts[0]

    # The Case link exists as a URN, and its human summary now rides alongside.
    assert verdict.case_ids, "captured verdict must link a Case"
    assert verdict.case_summaries == ("Reviewing a multi-source research synthesis",)
