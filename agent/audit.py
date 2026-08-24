"""Append-only audit trail: every computation and every human decision.

The UPL rule set requires it, and it feeds the observability story. Phase B
ships the local JSONL sink; Phase C adds the S3 Object Lock (Compliance
mode) sink behind the same protocol, which is where immutability becomes a
property of the storage rather than a promise of the code.
"""

from __future__ import annotations

import json
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
        self._sequence = 0

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
        with self._path.open("a") as handle:
            handle.write(json.dumps(row, default=str) + "\n")

    def read_all(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        return [json.loads(line) for line in self._path.read_text().splitlines() if line.strip()]
