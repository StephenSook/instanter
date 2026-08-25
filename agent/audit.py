"""Append-only audit trail: every computation and every human decision.

The UPL rule set requires it, and it feeds the observability story. Phase B
ships the local JSONL sink; Phase C adds the S3 Object Lock (Compliance
mode) sink behind the same protocol, which is where immutability becomes a
property of the storage rather than a promise of the code.
"""

from __future__ import annotations

import fcntl
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
        # Highest sequence this sink has durably written. Intra-file damage
        # is caught by validating the lines; WHOLE-FILE replacement
        # (logrotate's rename-and-recreate, a copy-truncate) is invisible
        # that way, because the fresh file is internally valid and simply
        # restarts at 1. Anchoring to what this sink already wrote makes a
        # replaced trail loud instead of silently split in two.
        self._written_seq = 0

    def append(self, event: AuditEvent) -> None:
        # Sequence allocation and the append happen under one interprocess
        # lock, with the next seq derived from the file itself at append
        # time: two sinks (or two scheduled processes) sharing a path can
        # never allocate the same value, and the seq stays contiguous. The
        # existing lines are VALIDATED before anything is written: a torn
        # tail or a broken sequence must stop consequential processing at
        # the append, not lie dormant until something happens to read the
        # trail back. The fsync makes the line durable before the lock
        # releases. (The Phase C sink moves allocation into atomic storage;
        # this is the local-mode equivalent, sized for local-mode files.)
        with self._path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                seq = 0
                for lineno, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(
                            f"audit file {self._path} is corrupt at line "
                            f"{lineno} (torn or malformed); refusing to "
                            "append to a damaged audit trail. Staff must "
                            "reconcile the file before any further run."
                        ) from exc
                    recorded_seq = row.get("seq") if isinstance(row, dict) else None
                    if type(recorded_seq) is not int or recorded_seq != seq + 1:
                        raise ValueError(
                            f"audit file {self._path} has a broken sequence "
                            f"at line {lineno} (expected {seq + 1}, found "
                            f"{recorded_seq!r}); refusing to append to a "
                            "damaged audit trail."
                        )
                    seq = recorded_seq
                if seq < self._written_seq:
                    raise ValueError(
                        f"audit file {self._path} lost records this sink "
                        f"already wrote (highest seq on disk {seq}, this "
                        f"sink wrote {self._written_seq}): the trail was "
                        "rotated, truncated, or replaced mid-run. Refusing "
                        "to append; staff must reconcile the audit trail."
                    )
                seq += 1
                row = {
                    "seq": seq,
                    "recorded_at": datetime.now().astimezone().isoformat(),
                    "run_id": event.run_id,
                    "kind": event.kind,
                    "case_id": event.case_id,
                    "payload": event.payload,
                }
                handle.seek(0, os.SEEK_END)
                handle.write(json.dumps(row, default=str) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                self._written_seq = seq
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

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
        # A damaged ORDERING is as disqualifying as a damaged line: the
        # sequence must be exactly 1..n with no duplicates and no gaps.
        seqs = [row.get("seq") for row in rows]
        if seqs != list(range(1, len(rows) + 1)):
            raise ValueError(
                f"audit file {self._path} has a broken sequence (expected "
                f"1..{len(rows)} contiguous); refusing to read a damaged "
                "audit trail"
            )
        return rows
