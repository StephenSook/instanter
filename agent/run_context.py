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
    # "approved": a human answered a presented interrupt with an exact
    # approval. "deferred": a human deferred (or the response failed the
    # strict parse). "pending": the unattended floor committed escalations
    # for LATER attorney review because no human was presented anything
    # (approval is never claimed for a commit nobody saw). "": no decision.
    attorney_action: str = ""
    # Set by the RUNNER only, around a floor/pending commit: the commit
    # tool's fail-closed gate accepts either a bound human approval or this
    # explicit floor authority, never a bare unapproved call.
    floor_commit_authorized: bool = False
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
    # Set when a pending approval was VOIDED (presented content changed
    # while the attorney decision was in flight). No human resolved the
    # candidates, so the run still owes them: the floor commits them as
    # pending review and the report can never read green over them.
    approval_invalidated: bool = False
    # Canonical digest of this invocation's inputs (run date, capacity,
    # records, malformed rows), set at load. Part of the durable run
    # manifest a stable run id reserves at first commit; a retry whose
    # digest differs is a different sweep and fails closed.
    inputs_digest: str = ""
    # Single-use lifecycle: a context that already carried a run must never
    # carry another. Reuse would replay stale decisions and committed ids
    # under the same run_id; the runner enforces this at entry.
    started: bool = False

    def __post_init__(self) -> None:
        # The run id scopes durable idempotency ((run_id, case_id)
        # insert-if-absent) and stamps every audit row; a malformed id would
        # silently break both, so it fails here, not at the first write.
        if not self.run_id or len(self.run_id) > 64:
            raise ValueError("run_id must be 1-64 characters")
        if any(ch.isspace() or not ch.isprintable() for ch in self.run_id):
            raise ValueError("run_id must be printable with no whitespace")

    @property
    def interrupt_candidates(self) -> list[TriageDecision]:
        return [d for d in self.decisions if d.interrupt_now]

    def decision_for(self, case_id: str) -> TriageDecision | None:
        for decision in self.decisions:
            if decision.case_id == case_id:
                return decision
        return None
