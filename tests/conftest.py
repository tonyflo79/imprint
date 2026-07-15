from __future__ import annotations

from copy import deepcopy

import pytest

from imprint.capture.schema import build_capture_envelope, new_urn


@pytest.fixture
def capture_envelope():
    text = "Do not hide a failed source; say which source failed because missing evidence changes the conclusion."
    envelope = build_capture_envelope(
        operator_id=new_urn("operator"),
        session_id=new_urn("session"),
        node_id="workstation-a",
        case_description="Reviewing a multi-source research synthesis",
        raw_operator_text=text,
        call_type="correct",
        capture_mechanism="explicit_cli",
        captured_by="imprint-test",
        reason="Missing evidence changes the conclusion.",
        captured_at="2026-07-14T18:00:00Z",
    )
    return deepcopy(envelope)
