"""Closed capture schemas with a namespaced extension escape hatch."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Any

from imprint.constants import (
    AUTHORITY_TIERS,
    CALL_TYPES,
    MAX_EVENT_BYTES,
    PROVENANCE,
    REASON_STATUSES,
    RECORD_SCHEMA_VERSION,
)
from imprint.errors import ValidationError

URN_RE = re.compile(r"^urn:imprint:([a-z][a-z0-9_-]*):([0-9a-f-]{36})$")
TOP_LEVEL = {
    "record_schema_version", "input_event_id", "operator_id", "session_id",
    "node_id", "captured_at", "capture_mechanism", "case", "verdict",
    "evidence", "alternatives", "provenance", "extensions",
}


def make_urn(kind: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", kind):
        raise ValidationError(f"invalid URN kind: {kind}")
    return f"urn:imprint:{kind}:{uuid.uuid4()}"


def require_urn(value: Any, kind: str | None = None) -> str:
    if not isinstance(value, str):
        raise ValidationError("URN must be a string")
    match = URN_RE.fullmatch(value)
    if not match or (kind and match.group(1) != kind):
        raise ValidationError(f"invalid {kind or 'Imprint'} URN")
    return value


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def payload_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _require_time(value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError("timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("timestamp must be RFC3339/ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValidationError("timestamp must include timezone")
    return value


def validate_provenance(value: Any, *, raw_capture: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("provenance must be an object")
    status = value.get("status")
    if status not in PROVENANCE:
        raise ValidationError("unknown provenance status")
    if raw_capture and status != "captured":
        raise ValidationError("raw operator capture must be captured")
    if raw_capture and value.get("actor_class") != "operator":
        raise ValidationError("raw captured authority requires operator actor_class")
    return value


def _retired_validate_capture_envelope_v3_draft(envelope: Any) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        raise ValidationError("capture envelope must be an object")
    if len(canonical_bytes(envelope)) > MAX_EVENT_BYTES:
        raise ValidationError("capture envelope exceeds maximum size")
    unknown = set(envelope) - TOP_LEVEL
    if unknown:
        raise ValidationError(f"unknown top-level fields: {sorted(unknown)}")
    if envelope.get("record_schema_version") != RECORD_SCHEMA_VERSION:
        raise ValidationError("unsupported record schema version")
    require_urn(envelope.get("input_event_id"), "event")
    require_urn(envelope.get("operator_id"), "operator")
    require_urn(envelope.get("session_id"), "session")
    node_id = envelope.get("node_id")
    if not isinstance(node_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", node_id):
        raise ValidationError("invalid node_id")
    _require_time(envelope.get("captured_at"))
    if envelope.get("capture_mechanism") not in {
        "claude_code_stop_hook", "explicit_cli", "approved_import",
    }:
        raise ValidationError("invalid capture_mechanism")

    case = envelope.get("case")
    if not isinstance(case, dict):
        raise ValidationError("case is required")
    require_urn(case.get("case_id"), "case")
    if not isinstance(case.get("description"), str) or not case["description"].strip():
        raise ValidationError("case description is required")

    verdict = envelope.get("verdict")
    if not isinstance(verdict, dict):
        raise ValidationError("verdict is required")
    require_urn(verdict.get("verdict_id"), "verdict")
    if not isinstance(verdict.get("raw_operator_text"), str) or not verdict["raw_operator_text"].strip():
        raise ValidationError("verbatim operator text is required")
    call = verdict.get("call")
    if not isinstance(call, dict):
        raise ValidationError("call is required")
    require_urn(call.get("call_id"), "call")
    if call.get("call_type") not in CALL_TYPES:
        raise ValidationError("invalid call_type")
    if verdict.get("reason_status") not in REASON_STATUSES:
        raise ValidationError("invalid reason_status")
    reason = verdict.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValidationError("reason must be a string or null")
    if verdict.get("reason_status") in {"supplied", "later_added"} and not reason:
        raise ValidationError("supplied reason cannot be empty")

    alternatives = envelope.get("alternatives", [])
    if not isinstance(alternatives, list):
        raise ValidationError("alternatives must be a list")
    alternative_ids = set()
    for item in alternatives:
        if not isinstance(item, dict):
            raise ValidationError("alternative must be an object")
        alternative_ids.add(require_urn(item.get("alternative_id"), "alternative"))
        if not isinstance(item.get("text"), str) or not item["text"].strip():
            raise ValidationError("alternative text is required")
    for field in ("chosen_alternative_ids", "rejected_alternative_ids"):
        ids = verdict.get(field, [])
        if not isinstance(ids, list) or any(item not in alternative_ids for item in ids):
            raise ValidationError(f"{field} must reference declared alternatives")

    evidence = envelope.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValidationError("at least one evidence record is required")
    evidence_ids = set()
    for item in evidence:
        if not isinstance(item, dict):
            raise ValidationError("evidence must be an object")
        evidence_ids.add(require_urn(item.get("evidence_id"), "evidence"))
        content = item.get("content")
        if not isinstance(content, str) or not content:
            raise ValidationError("evidence content is required")
        expected = hashlib.sha256(content.encode()).hexdigest()
        if item.get("content_sha256") != expected:
            raise ValidationError("evidence hash mismatch")
    refs = case.get("source_refs", [])
    if not isinstance(refs, list) or any(ref not in evidence_ids for ref in refs):
        raise ValidationError("case source_refs must reference evidence")

    validate_provenance(envelope.get("provenance"), raw_capture=True)
    extensions = envelope.get("extensions", {})
    if not isinstance(extensions, dict):
        raise ValidationError("extensions must be an object")
    for namespace, body in extensions.items():
        if "." not in namespace or not isinstance(body, dict):
            raise ValidationError("extensions require namespaced object keys")
    return envelope


# Compatibility import only. Raw capture has one canonical validator; keeping
# this alias prevents older direct imports from invoking the retired divergent
# implementation above. New code imports from ``imprint.capture.schema``.
from imprint.capture.schema import validate_capture_envelope as validate_capture_envelope
