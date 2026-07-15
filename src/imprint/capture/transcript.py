"""Bounded parsing of native Claude Code transcript JSONL hook input."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from imprint.errors import ValidationError

MAX_TRANSCRIPT_BYTES = 16 * 1024 * 1024


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def parse_native_stop_transcript(path_value: str) -> dict[str, str | None]:
    path = Path(path_value).expanduser()
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValidationError("transcript_path must be an absolute regular non-symlink file")
    size = path.stat().st_size
    if size <= 0 or size > MAX_TRANSCRIPT_BYTES:
        raise ValidationError("transcript_path size is outside the supported bound")
    messages: list[tuple[str, str]] = []
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for number, raw_line in enumerate(lines, start=1):
        complete = raw_line.endswith(("\n", "\r"))
        raw = raw_line.rstrip("\r\n")
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            if not complete and number == len(lines):
                raise ValidationError(f"incomplete transcript line {number}") from exc
            raise ValidationError(f"malformed complete transcript line {number}") from exc
        if not isinstance(item, dict) or item.get("type") not in {"user", "assistant"}:
            continue
        message = item.get("message", {})
        if not isinstance(message, dict):
            continue
        text = _message_text(message.get("content"))
        if text.strip():
            messages.append((item["type"], text))
    user_indexes = [index for index, (kind, _) in enumerate(messages) if kind == "user"]
    if not user_indexes:
        raise ValidationError("transcript contains no user message")
    user_index = user_indexes[-1]
    operator_text = messages[user_index][1]
    prior_assistant = next(
        (messages[index][1] for index in range(user_index - 1, -1, -1) if messages[index][0] == "assistant"),
        None,
    )
    locator = "transcript:sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "operator_text": operator_text,
        "prior_assistant_output": prior_assistant,
        "case_description": "Explicit operator feedback witnessed in the Claude Code transcript",
        "source_locator": locator,
    }
