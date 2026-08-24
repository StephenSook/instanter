"""Case store: intake records in, escalation records out.

Phase B ships the JSON-file implementation the demo seed uses; Phase C adds
a DynamoDB implementation behind the same protocol (conditional-put
idempotency lives there). Parsing intake into engine types happens HERE, on
purpose: serialized strings become real enums and dates at the boundary, so
the frozen engine's fail-closed validation is the second line of defense,
not the first.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from engine.deadline import CaseInput
from engine.rules import ServiceMethod


@dataclass(frozen=True)
class IntakeRecord:
    """One clinic intake row, exactly as staff captured it. Synthetic in
    every demo: ``label`` is rendered wherever the record appears."""

    case_id: str
    jurisdiction_id: str
    service_date: str | None  # ISO date or None when unknown/disputed
    service_method: str  # "personal" | "notorious" | "tack_and_mail" | "unknown"
    posting_date: str | None = None
    mailing_date: str | None = None
    summons_stated_deadline: str | None = None
    amended_affidavit: bool = False
    answer_filed: bool = False
    notes: str = ""
    tenant_display_name: str = ""
    property_address: str = ""
    label: str = "EXAMPLE DATA"


class IntakeParseError(ValueError):
    """A record that cannot be converted safely. Never guessed past."""


def _parse_date(value: str | None, field_name: str, case_id: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise IntakeParseError(
            f"case {case_id}: {field_name} {value!r} is not an ISO date"
        ) from exc


def to_case_input(record: IntakeRecord) -> CaseInput:
    """Boundary conversion; raises IntakeParseError instead of guessing."""
    try:
        method = ServiceMethod(record.service_method)
    except ValueError as exc:
        raise IntakeParseError(
            f"case {record.case_id}: unknown service_method {record.service_method!r}"
        ) from exc
    return CaseInput(
        case_id=record.case_id,
        jurisdiction_id=record.jurisdiction_id,
        service_date=_parse_date(record.service_date, "service_date", record.case_id),
        service_method=method,
        posting_date=_parse_date(record.posting_date, "posting_date", record.case_id),
        mailing_date=_parse_date(record.mailing_date, "mailing_date", record.case_id),
        summons_stated_deadline=_parse_date(
            record.summons_stated_deadline, "summons_stated_deadline", record.case_id
        ),
        amended_affidavit=record.amended_affidavit,
    )


@dataclass(frozen=True)
class EscalationRecord:
    """A committed escalation: the ladder's decision plus the model's
    validated rationale, awaiting or reflecting an attorney's action."""

    case_id: str
    disposition: str
    rank: int
    factors: tuple[str, ...]
    rationale: str
    confidence: float
    run_id: str
    status: str = "pending_attorney"  # -> "approved" | "deferred"
    attorney_note: str = ""


class CaseStore(Protocol):
    def load_intake(self) -> list[IntakeRecord]: ...

    def record_escalation(self, escalation: EscalationRecord) -> None: ...

    def list_escalations(self, run_id: str | None = None) -> list[EscalationRecord]: ...


class JsonFileCaseStore:
    """Seed-file intake + append-only escalation log. Demo/local backend."""

    def __init__(self, intake_path: Path, escalations_path: Path) -> None:
        self._intake_path = intake_path
        self._escalations_path = escalations_path

    def load_intake(self) -> list[IntakeRecord]:
        raw = json.loads(self._intake_path.read_text())
        records = [IntakeRecord(**row) for row in raw["records"]]
        if not records:
            raise IntakeParseError("intake file contains zero records")
        seen: set[str] = set()
        for record in records:
            if record.case_id in seen:
                raise IntakeParseError(f"duplicate case_id {record.case_id!r} in intake")
            seen.add(record.case_id)
        return records

    def record_escalation(self, escalation: EscalationRecord) -> None:
        payload = asdict(escalation)
        payload["recorded_at"] = datetime.now().astimezone().isoformat()
        with self._escalations_path.open("a") as handle:
            handle.write(json.dumps(payload) + "\n")

    def list_escalations(self, run_id: str | None = None) -> list[EscalationRecord]:
        if not self._escalations_path.exists():
            return []
        out: list[EscalationRecord] = []
        for line in self._escalations_path.read_text().splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            data.pop("recorded_at", None)
            record = EscalationRecord(**{**data, "factors": tuple(data.get("factors", ()))})
            if run_id is None or record.run_id == run_id:
                out.append(record)
        return out
