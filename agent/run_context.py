"""Shared state for one unattended triage run.

The tools close over this context: deterministic results accumulate here,
the model's validated outputs land here, and the runner reads the final
report from here. Nothing in this object is ever exposed raw to the model;
tools serialize exactly the fields the current step needs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

from agent.audit import AuditSink
from agent.models import EscalationRationale, ExtractedObservations
from agent.store import CaseStore, IntakeRecord
from agent.triage import TriageDecision
from engine.deadline import DeadlineResult


@dataclass
class RunContext:
    run_date: date
    attorney_capacity: int
    store: CaseStore
    audit: AuditSink
    run_id: str = field(default_factory=lambda: f"run-{uuid.uuid4().hex[:12]}")

    records: dict[str, IntakeRecord] = field(default_factory=dict)
    deadlines: dict[str, DeadlineResult] = field(default_factory=dict)
    # Case-level refusals (unparseable rows): first-class run outcomes. A
    # refused case is a case the sweep could NOT protect; it must reach the
    # report and fail the run, never just an audit line.
    refused_cases: dict[str, str] = field(default_factory=dict)
    observations: dict[str, ExtractedObservations] = field(default_factory=dict)
    decisions: list[TriageDecision] = field(default_factory=list)
    rationales: dict[str, EscalationRationale] = field(default_factory=dict)
    packet_memos: dict[str, str] = field(default_factory=dict)
    committed_case_ids: tuple[str, ...] = ()
    attorney_action: str = ""  # "approved" | "deferred" | "" before review
    # Immutable snapshot of exactly what the attorney approved: the case ids
    # presented at the interrupt. Approval binds to THESE cases; nothing
    # outside the snapshot may ever be committed under that approval, and a
    # changed queue requires a fresh interrupt.
    approved_case_ids: tuple[str, ...] | None = None
    # Digest of the full presented content (case ids, ranks, days, factors,
    # rationale texts) captured BEFORE the interrupt suspends, re-verified
    # when the interrupt resumes AND again at commit time: state that
    # changed during the pause voids the approval, fail closed.
    pending_approval_digest: str | None = None
    approval_digest: str | None = None
    # Single-use lifecycle: a context that already carried a run must never
    # carry another. Reuse would replay stale decisions and committed ids
    # under the same run_id; the runner enforces this at entry.
    started: bool = False

    @property
    def interrupt_candidates(self) -> list[TriageDecision]:
        return [d for d in self.decisions if d.interrupt_now]

    def decision_for(self, case_id: str) -> TriageDecision | None:
        for decision in self.decisions:
            if decision.case_id == case_id:
                return decision
        return None
