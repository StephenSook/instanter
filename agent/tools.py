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
from agent.models import EscalationRationale, ExtractedObservations
from agent.run_context import RunContext
from agent.store import EscalationRecord, IntakeParseError, to_case_input
from agent.triage import TriageCase, triage_queue
from engine.deadline import compute_deadline
from engine.rules import RULES


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
                # field detonates in to_case_input or the frozen engine's
                # CaseInput validation. All of them become refusals; engine
                # COMPUTATION errors stay loud (they are programming errors).
                case_input = to_case_input(record)
            except (IntakeParseError, TypeError, ValueError) as exc:
                # One malformed row must never kill the unattended sweep of
                # the rows that parsed. It becomes a case-level refusal a
                # human resolves, loudly audited, surfaced in the output.
                refused.append({"case_id": record.case_id, "reason": str(exc)})
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
                # Refusal is a case-level fact, not a crash: unsupported rows
                # surface for a human with no computed deadline.
                from engine.rules import GEORGIA_RULE

                result = compute_deadline(case_input, GEORGIA_RULE)
            else:
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
                    },
                    run_id=ctx.run_id,
                )
            )
            cases.append(
                TriageCase(
                    case_id=record.case_id,
                    deadline=result,
                    answer_filed=record.answer_filed,
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
        contributing_factors: list[str],
        rationale: str,
        confidence: float,
    ) -> str:
        """Submit the escalation rationale for ONE interrupt-now case.

        disposition must echo the ladder's level for this case exactly
        (e.g. 'interrupt'); contributing_factors restates the ladder's
        deterministic factors as plain strings; rationale is a short
        factual explanation (deadline date, days remaining, flags, queue
        position) with no advice; confidence is between 0 and 1. Facts
        only; the ladder decided, you explain.
        """
        decision = ctx.decision_for(case_id)
        if decision is None:
            return f"UNKNOWN CASE: {case_id!r} is not in the ranked queue."
        if not decision.interrupt_now:
            return (
                f"NOT AN INTERRUPT CASE: {case_id} is {decision.level.value} "
                "this run; rationales are only written for interrupt-now cases."
            )
        try:
            parsed = EscalationRationale(
                case_id=case_id,
                disposition=disposition,
                contributing_factors=contributing_factors,
                rationale=rationale,
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
        missing = [d.case_id for d in ctx.interrupt_candidates if d.case_id not in ctx.rationales]
        if missing:
            return "MISSING RATIONALES: submit_escalation_rationale first for: " + ", ".join(
                missing
            )
        committed: list[str] = []
        for decision in ctx.interrupt_candidates:
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
                ctx.committed_case_ids = tuple(committed)
                ctx.audit.append(
                    AuditEvent(
                        kind="store_write_failed",
                        case_id=decision.case_id,
                        payload={
                            "error": str(exc)[:300],
                            "written": committed,
                            "not_written": [
                                d.case_id
                                for d in ctx.interrupt_candidates
                                if d.case_id not in committed
                            ],
                        },
                        run_id=ctx.run_id,
                    )
                )
                return (
                    f"STORE WRITE FAILED on {decision.case_id}: {exc}. "
                    f"Written before failure: {', '.join(committed) or 'none'}. "
                    "The remaining escalations were NOT committed; staff must "
                    "review the escalation store before any retry."
                )
            committed.append(decision.case_id)
        ctx.committed_case_ids = tuple(committed)
        ctx.audit.append(
            AuditEvent(
                kind="escalation_committed",
                case_id=None,
                payload={"cases": committed, "attorney_action": ctx.attorney_action},
                run_id=ctx.run_id,
            )
        )
        return f"Committed {len(committed)} escalation(s): {', '.join(committed)}."

    @tool
    def write_packet_memo(case_id: str, memo: str) -> str:
        """Record the attorney-facing cover memo for one committed escalation.

        The memo restates the operative facts (deadline, service method,
        flags, queue position) for the review packet's cover sheet. It must
        contain no advice and no legal conclusions; the packet's draft
        answer skeleton itself is generated deterministically with every
        defense field blank.
        """
        if case_id not in ctx.committed_case_ids:
            return f"NOT COMMITTED: {case_id!r} has no committed escalation this run."
        if case_id in ctx.packet_memos:
            return f"ALREADY RECORDED: {case_id} has a packet memo this run; do not submit another."
        from agent.models import _reject_advice_language  # runtime boundary, same floor

        try:
            _reject_advice_language(memo, "memo")
        except ValueError as exc:
            return f"VALIDATION FAILED: {exc}"
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
