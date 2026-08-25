"""Append-only audit trail: every computation and every human decision.

The UPL rule set requires it, and it feeds the observability story. Phase B
ships the local JSONL sink; Phase C adds the S3 Object Lock (Compliance
mode) sink behind the same protocol, which is where immutability becomes a
property of the storage rather than a promise of the code.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class AuditEvent:
    kind: str  # e.g. "run_started", "deadline_computed", "observation_recorded",
    # "queue_ranked", "escalation_committed", "attorney_decision", "model_error"
    case_id: str | None
    payload: dict[str, Any]
    run_id: str


class AuditSink(Protocol):
    def append(self, event: AuditEvent) -> None: ...


class JsonlAuditSink:
    """Local append-only file. One JSON object per line, timestamped at
    write, never rewritten. The Phase C sink mirrors this shape into S3
    Object Lock objects keyed by run and sequence."""

    def __init__(self, path: Path) -> None:
        self._path = path
        # The sequence continues from the existing file, so appending
        # multiple runs to one file never reuses a seq value: the gap
        # detection the audit story leans on stays meaningful.
        self._sequence = self._existing_line_count()

    def _existing_line_count(self) -> int:
        if not self._path.exists():
            return 0
        return sum(1 for line in self._path.read_text().splitlines() if line.strip())

    def append(self, event: AuditEvent) -> None:
        self._sequence += 1
        row = {
            "seq": self._sequence,
            "recorded_at": datetime.now().astimezone().isoformat(),
            "run_id": event.run_id,
            "kind": event.kind,
            "case_id": event.case_id,
            "payload": event.payload,
        }
        # Flush and fsync per event: an audit line either exists durably or
        # the append raises. A legal audit trail cannot sit in a page cache.
        with self._path.open("a") as handle:
            handle.write(json.dumps(row, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def read_all(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for lineno, line in enumerate(self._path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"audit file {self._path} is corrupt at line {lineno}; "
                    "refusing to read a damaged audit trail"
                ) from exc
        return rows
