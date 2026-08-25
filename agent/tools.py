"""Typed tools for the triage agents.

Every tool has a strict contract: inputs validated through Pydantic, refusal
over guessing, and an audit event per consequential call. The deterministic
machinery (the frozen engine, the ladder) lives INSIDE tools so the trace
shows exactly when computation happened, while the model's own output goes
through the validated models in agent.models before anything accepts it.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError
from strands import tool

from agent.audit import AuditEvent
from agent.hooks import presented_content_digest
from agent.models import (
    LOW_CONFIDENCE_THRESHOLD,
    EscalationRationale,
    ExtractedObservations,
    reject_model_numerics,
)
from agent.run_context import RunContext
from agent.store import (
    EscalationRecord,
    IntakeParseError,
    RunManifest,
    to_case_input,
    validate_intake_types,
)
from agent.triage import TriageCase, triage_queue
from engine.deadline import compute_deadline
from engine.rules import RULES


def rationale_facts(ctx: RunContext, case_id: str) -> str:
    """Deterministic numeric core of an escalation rationale: the date, the
    day count, and the rank always come from engine state (a writer model
    fabricated all three in a probe; the model now contributes only a
    digitless explanation appended after these facts)."""
    decision = ctx.decision_for(case_id)
    deadline = ctx.deadlines.get(case_id)
    bits: list[str] = []
    if deadline is not None and deadline.effective_deadline is not None:
        bits.append(f"Effective deadline {deadline.effective_deadline}.")
    if decision is not None:
        if decision.days_remaining is not None:
            bits.append(
                f"{decision.days_remaining} day(s) remaining at the run date "
                f"{ctx.run_date.isoformat()}; queue rank {decision.rank}."
            )
        else:
            bits.append(f"No reliable deadline was established; queue rank {decision.rank}.")
    return " ".join(bits)


def packet_facts(ctx: RunContext, case_id: str) -> str:
    """Deterministic attorney-packet fact sheet: every number and date in a
    memo comes from engine state, never from a model. (A drafter with no
    facts source fabricated day counts in live runs; memo facts are
    therefore generated here on every path, and the drafter only ever adds
    reviewer notes.)"""
    decision = ctx.decision_for(case_id)
    deadline = ctx.deadlines.get(case_id)
    record = ctx.records.get(case_id)
    from engine.deadline import DeadlineBasis

    parts: list[str] = []
    if deadline is not None and deadline.effective_deadline is not None:
        if deadline.deadline_basis is DeadlineBasis.SUMMONS_ONLY_UNVERIFIED:
            # DeadlineBasis's own contract: never present a summons-only
            # date as a finalized statutory computation.
            parts.append(
                f"Effective deadline {deadline.effective_deadline} "
                "(summons-stated, UNVERIFIED; the statutory computation was "
                "refused and attorney confirmation is required)."
            )
        else:
            parts.append(f"Effective deadline {deadline.effective_deadline}.")
    if decision is not None:
        if decision.days_remaining is not None:
            parts.append(
                f"{decision.days_remaining} day(s) remaining at the run date "
                f"{ctx.run_date.isoformat()}; queue rank {decision.rank}."
            )
        else:
            # The no-clock state is a fact too: never render "None day(s)".
            parts.append(
                "No reliable deadline was established; the attorney must "
                f"resolve the intake facts. Queue rank {decision.rank}."
            )
    if record is not None:
        parts.append(f"Service method recorded as {record.service_method}.")
    if deadline is not None and deadline.flags:
        # The flags' REASON texts are the attorney-facing substance (the
        # competing computed date in a summons conflict, the no-roll
        # closure advisory with the reopen date); bare enum codes told the
        # attorney a conflict existed but never what the other date was.
        parts.append(
            "Flags: " + " ".join(f"[{f.code.value}] {f.reason[:220]}" for f in deadline.flags)
        )
    # Recorded intake-analysis ambiguities are packet facts too: silently
    # dropping them loses exactly the open questions the attorney must
    # resolve (round 11: two cases' recorded ambiguities never reached
    # their memos while the run scored 17/17).
    observation = ctx.observations.get(case_id)
    if observation is not None and observation.ambiguities:
        parts.append(
            "Open questions recorded at intake analysis: " + " ".join(observation.ambiguities)
        )
    parts.append("Staff verify the intake facts against the tenant record.")
    return " ".join(parts)


def _dump_validation_error(exc: ValidationError) -> str:
    issues = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
    return f"VALIDATION FAILED: {issues}. Correct the output and resubmit."


def _rejection_payload(exc: ValidationError) -> dict[str, Any]:
    """Bounded error detail for the audit trail: WHY a submission was
    rejected, not just that one was (diagnosing live-model behavior
    depends on this)."""
    details = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"[:200] for e in exc.errors()[:5]]
    return {"errors": exc.error_count(), "details": details}


def build_tools(ctx: RunContext) -> dict[str, Any]:
    """Build the tool set closed over one run's context."""

    def _audit_tool_refusal(tool: str, case_id: str, reason: str) -> None:
        # Identity refusals (unknown case, not committed, duplicate memo)
        # must be diagnosable from the audit trail alone: a hallucinated
        # case id otherwise leaves no trace beyond the ABSENCE of a
        # recorded event.
        ctx.audit.append(
            AuditEvent(
                kind="tool_refused",
                case_id=case_id,
                payload={"tool": tool, "reason": reason},
                run_id=ctx.run_id,
            )
        )

    @tool
    def list_cases_with_notes() -> str:
        """List every intake case that has free-text notes to read.

        Returns a JSON array of {case_id, notes, label}. The label marks
        synthetic demo data and must be treated as part of the record.
        """
        rows = [
            {"case_id": r.case_id, "notes": r.notes, "label": r.label}
            for r in ctx.records.values()
            if r.notes.strip()
        ]
        return json.dumps(rows)

    @tool
    def submit_case_observations(
        case_id: str,
        summary: str,
        needs_human_confirmation: bool,
        confidence: float,
        mentions_service_by_posting: bool | None = None,
        mentions_answer_already_filed: bool | None = None,
        mentions_hearing_or_deadline_change: bool | None = None,
        mentions_possible_defective_service: bool | None = None,
        ambiguities: list[str] | None = None,
    ) -> str:
        """Submit typed observations extracted from one case's notes.

        summary states only what the notes say (never advice). Each
        mentions_* boolean is true/false when the notes address it, omitted
        when not mentioned. ambiguities is a list of plain-string open
        questions a staff member must confirm; set
        needs_human_confirmation=true whenever any signal is uncertain or
        conflicting. confidence is between 0 and 1.
        """
        if case_id not in ctx.records:
            _audit_tool_refusal("submit_case_observations", case_id, "unknown_case")
            return f"UNKNOWN CASE: {case_id!r} is not in this run's intake."
        try:
            parsed = ExtractedObservations(
                summary=summary,
                needs_human_confirmation=needs_human_confirmation,
                confidence=confidence,
                mentions_service_by_posting=mentions_service_by_posting,
                mentions_answer_already_filed=mentions_answer_already_filed,
                mentions_hearing_or_deadline_change=mentions_hearing_or_deadline_change,
                mentions_possible_defective_service=mentions_possible_defective_service,
                ambiguities=ambiguities or [],
            )
        except ValidationError as exc:
            ctx.audit.append(
                AuditEvent(
                    kind="observation_rejected",
                    case_id=case_id,
                    payload=_rejection_payload(exc),
                    run_id=ctx.run_id,
                )
            )
            return _dump_validation_error(exc)
        ctx.observations[case_id] = parsed
        ctx.audit.append(
            AuditEvent(
                kind="observation_recorded",
                case_id=case_id,
                payload=parsed.model_dump(),
                run_id=ctx.run_id,
            )
        )
        return f"Observations recorded for {case_id}."

    @tool
    def get_ranked_queue() -> str:
        """Compute every case's statutory deadline deterministically, run the
        discriminator-first triage ladder, and return the ranked queue.

        The deadline math and dispositions are deterministic code, not model
        output. Returns JSON: run_date, attorney_capacity, and a ranked list
        of {case_id, level, rank, days_remaining, interrupt_now, held_reason,
        factors, flags, observation_summary}.
        """
        cases: list[TriageCase] = []
        refused: list[dict[str, str]] = []
        for record in ctx.records.values():
            try:
                # TypeError/ValueError here are data-shaped: a wrongly-typed
                # field detonates in exact-type validation, to_case_input, or
                # the frozen engine's CaseInput validation. All of them
                # become refusals; engine COMPUTATION errors stay loud (they
                # are programming errors).
                validate_intake_types(record)
                case_input = to_case_input(record)
            except (IntakeParseError, TypeError, ValueError) as exc:
                # One malformed row must never kill the unattended sweep of
                # the rows that parsed. It becomes a case-level refusal a
                # human resolves, loudly audited, surfaced in the output.
                refused.append({"case_id": record.case_id, "reason": str(exc)})
                ctx.refused_cases[record.case_id] = str(exc)
                ctx.audit.append(
                    AuditEvent(
                        kind="case_refused",
                        case_id=record.case_id,
                        payload={"reason": str(exc)},
                        run_id=ctx.run_id,
                    )
                )
                continue
            rule = RULES.get(record.jurisdiction_id)
            if rule is None:
                # Refuse over guessing: computing a typo'd or unsupported
                # jurisdiction under the Georgia rule surfaces it as a calm
                # L2 row and lets the run read green over a case the sweep
                # never protected. No rule row = a case-level refusal that
                # reaches the report and fails the run.
                reason = (
                    f"jurisdiction {record.jurisdiction_id!r} has no rule "
                    "row; the sweep refuses to compute a deadline under "
                    "another jurisdiction's authority"
                )
                refused.append({"case_id": record.case_id, "reason": reason})
                ctx.refused_cases[record.case_id] = reason
                ctx.audit.append(
                    AuditEvent(
                        kind="case_refused",
                        case_id=record.case_id,
                        payload={"reason": reason},
                        run_id=ctx.run_id,
                    )
                )
                continue
            result = compute_deadline(case_input, rule)
            ctx.deadlines[record.case_id] = result
            ctx.audit.append(
                AuditEvent(
                    kind="deadline_computed",
                    case_id=record.case_id,
                    payload={
                        "computed": str(result.computed_deadline),
                        "effective": str(result.effective_deadline),
                        "basis": result.deadline_basis.value,
                        "flags": [f.code.value for f in result.flags],
                        # Reason texts make the trail self-contained: the
                        # competing date in a summons conflict lived only in
                        # process memory before this.
                        "flag_reasons": [f.reason[:300] for f in result.flags],
                    },
                    run_id=ctx.run_id,
                )
            )
            obs = ctx.observations.get(record.case_id)
            notes_present = bool(record.notes.strip())
            cases.append(
                TriageCase(
                    case_id=record.case_id,
                    deadline=result,
                    answer_filed=record.answer_filed,
                    notes_present=notes_present,
                    observation_missing=notes_present and obs is None,
                    observed_service_by_posting=(obs.mentions_service_by_posting if obs else None),
                    observed_answer_already_filed=(
                        obs.mentions_answer_already_filed if obs else None
                    ),
                    observed_hearing_or_deadline_change=(
                        obs.mentions_hearing_or_deadline_change if obs else None
                    ),
                    observed_possible_defective_service=(
                        obs.mentions_possible_defective_service if obs else None
                    ),
                    observation_needs_confirmation=(
                        bool(obs.needs_human_confirmation) if obs else False
                    ),
                    observation_has_ambiguities=bool(obs.ambiguities) if obs else False,
                    observation_low_confidence=(
                        obs.confidence < LOW_CONFIDENCE_THRESHOLD if obs else False
                    ),
                )
            )

        ctx.decisions = triage_queue(cases, ctx.run_date, ctx.attorney_capacity)
        ctx.audit.append(
            AuditEvent(
                kind="queue_ranked",
                case_id=None,
                payload={
                    "capacity": ctx.attorney_capacity,
                    "interrupts": [d.case_id for d in ctx.interrupt_candidates],
                    "total": len(ctx.decisions),
                },
                run_id=ctx.run_id,
            )
        )

        rows = []
        for d in ctx.decisions:
            obs = ctx.observations.get(d.case_id)
            rows.append(
                {
                    "case_id": d.case_id,
                    "level": d.level.value,
                    "rank": d.rank,
                    "days_remaining": d.days_remaining,
                    "interrupt_now": d.interrupt_now,
                    "held_reason": d.held_reason,
                    "factors": list(d.factors),
                    "raised_by": list(d.raised_by),
                    "flags": list(d.flags),
                    "observation_summary": obs.summary if obs else None,
                    "observation_needs_confirmation": (
                        obs.needs_human_confirmation if obs else None
                    ),
                }
            )
        return json.dumps(
            {
                "run_date": ctx.run_date.isoformat(),
                "attorney_capacity": ctx.attorney_capacity,
                "queue": rows,
                "refused_cases": refused,
            }
        )

    @tool
    def submit_escalation_rationale(
        case_id: str,
        disposition: str,
        explanation: str,
        confidence: float,
    ) -> str:
        """Submit the escalation rationale for ONE interrupt-now case.

        disposition must echo the ladder's level for this case exactly
        (e.g. 'interrupt'). explanation is a short DIGITLESS narrative of
        why this case outranks the others, in plain factual language with
        no advice: every date, day count, and rank is rendered
        deterministically by the system, so name the facts, never figures.
        confidence is between 0 and 1. The ladder decided, you explain.
        """
        decision = ctx.decision_for(case_id)
        if decision is None:
            _audit_tool_refusal("submit_escalation_rationale", case_id, "unknown_case")
            return f"UNKNOWN CASE: {case_id!r} is not in the ranked queue."
        if not decision.interrupt_now:
            _audit_tool_refusal("submit_escalation_rationale", case_id, "not_interrupt_case")
            return (
                f"NOT AN INTERRUPT CASE: {case_id} is {decision.level.value} "
                "this run; rationales are only written for interrupt-now cases."
            )
        try:
            # The attorney-facing numbers all come from engine state; a
            # model-authored figure beside them, in digits OR words ("nine
            # hundred ninety nine days"), is exactly the fabrication the
            # round-9 memo fix closed, resurfacing one field over. The
            # error text states the exact way out (rephrase, or submit an
            # empty explanation): a live writer retries on error text, and
            # a rejection with no stated recovery starves the whole sweep
            # into the floor (observed live: ten straight rejections, zero
            # commits, backstop delivery).
            reject_model_numerics(explanation, "explanation")
        except ValueError as exc:
            ctx.audit.append(
                AuditEvent(
                    kind="rationale_rejected",
                    case_id=case_id,
                    payload={"reason": "numeric_explanation", "detail": str(exc)[:200]},
                    run_id=ctx.run_id,
                )
            )
            return (
                f"VALIDATION FAILED: {exc} Or resubmit with explanation set "
                "to the empty string to record the deterministic facts alone."
            )
        body = f"{rationale_facts(ctx, case_id)} Writer explanation: {explanation.strip()}"
        if not explanation.strip():
            # An empty explanation is a legitimate submission: the
            # deterministic facts stand alone.
            body = rationale_facts(ctx, case_id)
        try:
            parsed = EscalationRationale(
                case_id=case_id,
                disposition=disposition,
                contributing_factors=list(decision.factors)[:8],
                rationale=body[:900],
                confidence=confidence,
            )
        except ValidationError as exc:
            ctx.audit.append(
                AuditEvent(
                    kind="rationale_rejected",
                    case_id=case_id,
                    payload=_rejection_payload(exc),
                    run_id=ctx.run_id,
                )
            )
            return _dump_validation_error(exc)
        if parsed.disposition != decision.level.value:
            # A model attempting to relabel a ladder decision is exactly the
            # behavior the audit story must preserve.
            ctx.audit.append(
                AuditEvent(
                    kind="rationale_rejected",
                    case_id=case_id,
                    payload={
                        "reason": "disposition_mismatch",
                        "ladder": decision.level.value,
                        "submitted": str(parsed.disposition)[:60],
                    },
                    run_id=ctx.run_id,
                )
            )
            return (
                f"DISPOSITION MISMATCH: the ladder decided "
                f"{decision.level.value!r} for {case_id}; the rationale must "
                "echo it exactly. The ladder decides, you explain."
            )
        ctx.rationales[case_id] = parsed
        ctx.audit.append(
            AuditEvent(
                kind="rationale_recorded",
                case_id=case_id,
                payload=parsed.model_dump(),
                run_id=ctx.run_id,
            )
        )
        return f"Rationale recorded for {case_id}."

    @tool
    def commit_escalations() -> str:
        """Commit every interrupt-now escalation for attorney review.

        Requires a validated rationale for each interrupt-now case. This
        action pauses for a licensed attorney's approval before executing;
        the attorney may approve or defer the whole set.
        """
        # Fail closed BEFORE anything touches the store. Two authorities can
        # commit, nothing else: (a) a human approval bound to a nonempty
        # snapshot with a verifiable content digest, or (b) the runner's
        # unattended floor, which writes pending-review records under an
        # explicit authorization flag and NEVER claims approval. This holds
        # even if the approval hook were unwired (a green unit suite beside
        # dead runtime wiring must still not be able to commit).
        pending_floor = ctx.attorney_action == "pending" and ctx.floor_commit_authorized
        if not pending_floor:
            if ctx.attorney_action != "approved":
                ctx.audit.append(
                    AuditEvent(
                        kind="commit_refused",
                        case_id=None,
                        payload={
                            "reason": "not_approved",
                            "attorney_action": ctx.attorney_action,
                        },
                        run_id=ctx.run_id,
                    )
                )
                return (
                    "NOT APPROVED: committing escalations requires an attorney "
                    "approval; none is recorded for this run."
                )
            if not ctx.approved_case_ids or ctx.approval_digest is None:
                ctx.audit.append(
                    AuditEvent(
                        kind="commit_refused",
                        case_id=None,
                        payload={"reason": "approval_unbound"},
                        run_id=ctx.run_id,
                    )
                )
                return (
                    "APPROVAL UNBOUND: the recorded approval carries no candidate "
                    "snapshot or content digest; a fresh attorney approval is "
                    "required before anything can be committed."
                )
            if presented_content_digest(ctx, ctx.approved_case_ids) != ctx.approval_digest:
                ctx.audit.append(
                    AuditEvent(
                        kind="commit_refused",
                        case_id=None,
                        payload={"reason": "approved_content_changed"},
                        run_id=ctx.run_id,
                    )
                )
                return (
                    "APPROVED CONTENT CHANGED: the queue or a rationale differs "
                    "from what the attorney approved; a fresh approval is required."
                )
        missing = [d.case_id for d in ctx.interrupt_candidates if d.case_id not in ctx.rationales]
        if missing:
            ctx.audit.append(
                AuditEvent(
                    kind="commit_refused",
                    case_id=None,
                    payload={"reason": "missing_rationales", "cases": missing},
                    run_id=ctx.run_id,
                )
            )
            return "MISSING RATIONALES: submit_escalation_rationale first for: " + ", ".join(
                missing
            )

        # Idempotent against DURABLE state, verified by CONTENT: the store
        # is the truth about what committed, and a durable row only counts
        # as this run's commit if it says exactly what this run would write
        # (a same-key row carrying a stale rank or a different rationale is
        # a conflict a human must resolve, never a silent success).
        def expected_record(decision: Any) -> EscalationRecord:
            rationale = ctx.rationales[decision.case_id]
            return EscalationRecord(
                case_id=decision.case_id,
                disposition=decision.level.value,
                rank=decision.rank,
                factors=decision.factors,
                rationale=rationale.rationale,
                confidence=rationale.confidence,
                run_id=ctx.run_id,
                status="pending_attorney",
            )

        def content_key(record: EscalationRecord) -> tuple[Any, ...]:
            # The FULL record, storage metadata excluded: a same-key row
            # whose lifecycle already moved (status "deferred", an attorney
            # note) is NOT this run's pending commit and must conflict, not
            # satisfy. (Lifecycle transitions become store operations in
            # the Phase C DynamoDB model; within a run, exact match only.)
            return (
                record.disposition,
                record.rank,
                tuple(record.factors),
                record.rationale,
                record.confidence,
                record.status,
                record.attorney_note,
            )

        # Run-scope integrity, part 1: the durable manifest. The FIRST
        # commit attempt for a run id reserves (inputs digest, candidate
        # set, run date, capacity) atomically; every retry must present the
        # identical manifest. A subset overlap is not enough (a partial
        # first commit plus mutated intake can leave the durable rows
        # inside the retry's candidate set while an original urgent case
        # silently disappears), so the comparison is against what the
        # ORIGINAL invocation promised, not against what happens to be on
        # disk.
        # Invariant guard (defense in depth behind the loader's duplicate
        # refusal): a case the run REFUSED can never also be committed. A
        # contested identity must reach the attorney as a refusal, not as a
        # durable escalation written from the surviving row's data.
        refused_candidates = sorted(
            d.case_id for d in ctx.interrupt_candidates if d.case_id in ctx.refused_cases
        )
        if refused_candidates:
            ctx.audit.append(
                AuditEvent(
                    kind="commit_refused",
                    case_id=None,
                    payload={
                        "reason": "refused_case_in_candidates",
                        "cases": refused_candidates,
                    },
                    run_id=ctx.run_id,
                )
            )
            return (
                f"REFUSED CASE IN QUEUE: {', '.join(refused_candidates)} carry "
                "case-level refusals this run and cannot be committed; staff "
                "must resolve the refusals first. Nothing was committed."
            )
        own_manifest = RunManifest(
            run_id=ctx.run_id,
            inputs_digest=ctx.inputs_digest,
            candidates=tuple(sorted(d.case_id for d in ctx.interrupt_candidates)),
            run_date=ctx.run_date.isoformat(),
            capacity=ctx.attorney_capacity,
        )
        stored_manifest = ctx.store.reserve_run_manifest(own_manifest)
        if stored_manifest != own_manifest:
            ctx.audit.append(
                AuditEvent(
                    kind="store_conflict",
                    case_id=None,
                    payload={
                        "reason": (
                            "this run id was reserved by an invocation with "
                            "different inputs; a stable run id promises the "
                            "same sweep, and this retry is a different one"
                        ),
                        "original_candidates": list(stored_manifest.candidates),
                        "retry_candidates": list(own_manifest.candidates),
                    },
                    run_id=ctx.run_id,
                )
            )
            return (
                "STORE CONFLICT: this run id was reserved by an invocation "
                "with different inputs (original candidates: "
                f"{', '.join(stored_manifest.candidates) or 'none'}; this retry: "
                f"{', '.join(own_manifest.candidates) or 'none'}). Nothing was "
                "committed; staff must reconcile the store."
            )
        stored = {e.case_id: e for e in ctx.store.list_escalations(run_id=ctx.run_id)}
        # Run-scope integrity, part 2 (defense in depth): durable rows for
        # cases outside the current interrupt set mean the sweeps diverged
        # even if the manifest was somehow satisfied. Fail closed before
        # any write.
        candidate_ids = {d.case_id for d in ctx.interrupt_candidates}
        foreign = sorted(cid for cid in stored if cid not in candidate_ids)
        if foreign:
            ctx.audit.append(
                AuditEvent(
                    kind="store_conflict",
                    case_id=None,
                    payload={
                        "reason": (
                            "durable escalations exist for this run id on "
                            "cases outside the current interrupt set; the "
                            "retry's inputs do not match the original "
                            "invocation"
                        ),
                        "cases": foreign,
                    },
                    run_id=ctx.run_id,
                )
            )
            return (
                f"STORE CONFLICT: durable escalations for this run id cover "
                f"{', '.join(foreign)}, which are not in the current queue; "
                "the retry does not match the original invocation's inputs. "
                "Nothing was committed; staff must reconcile the store."
            )
        already: list[str] = []
        conflicts: list[str] = []
        to_write = []
        for decision in ctx.interrupt_candidates:
            durable_row = stored.get(decision.case_id)
            if durable_row is None:
                to_write.append(decision)
            elif content_key(durable_row) == content_key(expected_record(decision)):
                already.append(decision.case_id)
            else:
                conflicts.append(decision.case_id)
                ctx.audit.append(
                    AuditEvent(
                        kind="store_conflict",
                        case_id=decision.case_id,
                        payload={
                            "reason": (
                                "a durable row with this run's key carries "
                                "different content; staff must reconcile"
                            ),
                            "stored_rank": durable_row.rank,
                            "expected_rank": decision.rank,
                        },
                        run_id=ctx.run_id,
                    )
                )
        ctx.committed_case_ids = tuple(already)
        # A detected conflict is a PREFLIGHT failure: once any durable row
        # disagrees with what this run would write, the sweeps have already
        # diverged, and writing the remaining cases would merge rows from
        # different approval snapshots under one run id. Nothing further is
        # written until staff reconcile.
        if conflicts:
            return (
                f"STORE CONFLICT: durable rows for {', '.join(conflicts)} "
                "carry different content than this run would write; staff "
                "must reconcile the escalation store. Nothing further was "
                "committed."
            )
        # Approval binding: once an attorney has approved, ONLY the cases in
        # the approval snapshot may be committed under it. Anything the
        # queue has since minted needs a fresh interrupt, never a ride on an
        # old approval.
        if ctx.attorney_action == "approved" and ctx.approved_case_ids is not None:
            approved = set(ctx.approved_case_ids)
            outside = [d.case_id for d in to_write if d.case_id not in approved]
            if outside:
                ctx.audit.append(
                    AuditEvent(
                        kind="commit_refused",
                        case_id=None,
                        payload={"reason": "requires_new_approval", "cases": outside},
                        run_id=ctx.run_id,
                    )
                )
            to_write = [d for d in to_write if d.case_id in approved]
        if not to_write:
            uncommitted = [
                d.case_id
                for d in ctx.interrupt_candidates
                if d.case_id not in ctx.committed_case_ids
            ]
            ctx.audit.append(
                AuditEvent(
                    kind="commit_refused",
                    case_id=None,
                    payload={
                        "reason": "already_committed",
                        "cases": already,
                        "uncommitted_outside_approval": uncommitted,
                    },
                    run_id=ctx.run_id,
                )
            )
            if uncommitted:
                # Tell the caller the truth: the approval's cases are done,
                # but candidates OUTSIDE the approval remain undelivered
                # (the report's parity keeps the run red until a fresh
                # approval resolves them).
                return (
                    "COMMITTED WITHIN APPROVAL: every escalation the attorney "
                    f"approved is durably recorded; {', '.join(uncommitted)} "
                    "remain uncommitted and require a fresh attorney approval."
                )
            return (
                "ALREADY COMMITTED: every interrupt-now escalation is already "
                "durably recorded this run; do not call commit_escalations again."
            )
        newly: list[str] = []
        for decision in to_write:
            rationale = ctx.rationales[decision.case_id]
            try:
                ctx.store.record_escalation(
                    EscalationRecord(
                        case_id=decision.case_id,
                        disposition=decision.level.value,
                        rank=decision.rank,
                        factors=decision.factors,
                        rationale=rationale.rationale,
                        confidence=rationale.confidence,
                        run_id=ctx.run_id,
                        status="pending_attorney",
                    )
                )
            except Exception as exc:
                # A store failure mid-commit must never pass silently: audit
                # exactly what was and was not written, surface it to the
                # attorney path, and do not pretend the set committed.
                ctx.committed_case_ids = tuple(already + newly)
                ctx.audit.append(
                    AuditEvent(
                        kind="store_write_failed",
                        case_id=decision.case_id,
                        payload={
                            "error": str(exc)[:300],
                            "written": already + newly,
                            "not_written": [
                                d.case_id
                                for d in ctx.interrupt_candidates
                                if d.case_id not in ctx.committed_case_ids
                            ],
                        },
                        run_id=ctx.run_id,
                    )
                )
                return (
                    f"STORE WRITE FAILED on {decision.case_id}: {exc}. "
                    f"Durably written so far: {', '.join(already + newly) or 'none'}. "
                    "The remaining escalations were NOT committed; staff must "
                    "review the escalation store before any retry."
                )
            newly.append(decision.case_id)
        ctx.committed_case_ids = tuple(already + newly)
        ctx.audit.append(
            AuditEvent(
                kind="escalation_committed",
                case_id=None,
                payload={
                    "cases": newly,
                    "total_committed": already + newly,
                    "attorney_action": ctx.attorney_action,
                },
                run_id=ctx.run_id,
            )
        )
        return f"Committed {len(newly)} escalation(s): {', '.join(newly)}."

    @tool
    def write_packet_memo(case_id: str, notes: str = "") -> str:
        """Record the attorney-facing cover memo for one committed escalation.

        The memo's fact sheet (effective deadline, days remaining, queue
        rank, service method, flags) is generated DETERMINISTICALLY from
        engine state; never restate numbers or dates yourself. notes is
        optional: short open questions staff should confirm, drawn from the
        recorded observations, in plain factual language with no advice and
        no legal conclusions. Pass notes as an empty string when there is
        nothing to add.
        """
        if case_id not in ctx.committed_case_ids:
            _audit_tool_refusal("write_packet_memo", case_id, "not_committed")
            return f"NOT COMMITTED: {case_id!r} has no committed escalation this run."
        if case_id in ctx.packet_memos:
            _audit_tool_refusal("write_packet_memo", case_id, "already_recorded")
            return f"ALREADY RECORDED: {case_id} has a packet memo this run; do not submit another."
        from agent.models import _reject_advice_language  # runtime boundary, same floor

        if notes:
            try:
                # Notes are structurally incapable of carrying a date, a
                # day count, or a rank, in digits or in words: every number
                # in a memo comes from the deterministic fact sheet, and a
                # note stating a contradictory figure beside it would
                # defeat exactly that guarantee.
                reject_model_numerics(notes, "notes")
                _reject_advice_language(notes, "notes")
            except ValueError as exc:
                # A drafter drifting into advice language is a UPL-boundary
                # event; it must be diagnosable from the audit trail alone.
                ctx.audit.append(
                    AuditEvent(
                        kind="memo_rejected",
                        case_id=case_id,
                        payload={"reason": str(exc)[:300]},
                        run_id=ctx.run_id,
                    )
                )
                return f"VALIDATION FAILED: {exc}"
        memo = packet_facts(ctx, case_id)
        if notes:
            memo = f"{memo} Reviewer notes: {notes.strip()}"
        if len(memo) > 4000:
            # Never slice: a truncated memo silently drops recorded facts
            # (a long first ambiguity previously swallowed the second).
            # Refuse loudly; the packet stays incomplete and the report's
            # missing-memo parity keeps the run red until it is resolved.
            ctx.audit.append(
                AuditEvent(
                    kind="memo_rejected",
                    case_id=case_id,
                    payload={"reason": "memo_too_long", "length": len(memo)},
                    run_id=ctx.run_id,
                )
            )
            return (
                "VALIDATION FAILED: the assembled memo exceeds the packet "
                "limit; shorten notes and resubmit. Nothing was recorded."
            )
        ctx.packet_memos[case_id] = memo
        ctx.audit.append(
            AuditEvent(
                kind="packet_memo_recorded",
                case_id=case_id,
                payload={"memo": memo},
                run_id=ctx.run_id,
            )
        )
        return f"Packet memo recorded for {case_id}."

    return {
        "list_cases_with_notes": list_cases_with_notes,
        "submit_case_observations": submit_case_observations,
        "get_ranked_queue": get_ranked_queue,
        "submit_escalation_rationale": submit_escalation_rationale,
        "commit_escalations": commit_escalations,
        "write_packet_memo": write_packet_memo,
    }
