"""Atomic node-local once-delivery receipts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

_SAFE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class DeliveryReceipts:
    def __init__(self, session_root: Path):
        self.root = Path(session_root)

    @staticmethod
    def _safe(value: str, label: str) -> str:
        if not _SAFE.fullmatch(value):
            raise ValueError(f"unsafe {label}")
        return value

    def _claim(self, session_id: str, snapshot_id: str, kind: str, domain_id: str | None) -> bool:
        session = self._safe(session_id, "session id")
        snapshot = hashlib.sha256(snapshot_id.encode("utf-8")).hexdigest()[:24]
        suffix = "session-start" if domain_id is None else f"domain-{self._safe(domain_id, 'domain id')}"
        directory = self.root / session
        directory.mkdir(parents=True, exist_ok=True)
        final = directory / f"{snapshot}-{suffix}.json"
        receipt = json.dumps(
            {"receipt_schema_version": "1.0.0", "snapshot_ref": snapshot, "type": kind},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(receipt)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, final)
                return True
            except FileExistsError:
                return False
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def claim_session_start(self, session_id: str, snapshot_id: str) -> bool:
        return self._claim(session_id, snapshot_id, "session_start", None)

    def claim_domain(self, session_id: str, snapshot_id: str, domain_id: str) -> bool:
        return self._claim(session_id, snapshot_id, "domain", domain_id)
