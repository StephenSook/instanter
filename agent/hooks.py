"""Hook providers: the attorney-approval interrupt and the audit trail.

The interrupt is the product moment: committing escalations pauses the run
and hands the decision to a licensed attorney. Approval resumes the tool;
deferral cancels it with the reason recorded. Nothing consequential executes
without the human.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry

from agent.audit import AuditEvent
from agent.run_context import RunContext

_GATED_TOOL = "commit_escalations"


def presented_content_digest(ctx: RunContext, case_ids: tuple[str, ...]) -> str:
    """Digest of everything the attorney is shown AND everything the commit
    would persist for these cases. Approval binds to this content; any
    change to it voids the approval.

    Canonical JSON, not separator-joined bytes: joining free text on a
    separator lets two different snapshots serialize to identical bytes
    when a field's content contains the separator (factors ('a', 'b') with
    rationale 'r' vs factors ('a',) with rationale 'b\\x00r'). JSON escapes
    every string and keeps the structure explicit, so distinct snapshots
    cannot collide by construction."""
    entries = []
    for case_id in sorted(case_ids):
        decision = ctx.decision_for(case_id)
        rationale = ctx.rationales.get(case_id)
        entries.append(
            {
                "case_id": case_id,
                "rank": decision.rank if decision else None,
                "days_remaining": decision.days_remaining if decision else None,
                "level": decision.level.value if decision else None,
                "held_reason": decision.held_reason if decision else None,
                "factors": list(decision.factors) if decision else None,
                "flags": list(decision.flags) if decision else None,
                "rationale": rationale.rationale if rationale else None,
                "disposition": rationale.disposition if rationale else None,
                "confidence": rationale.confidence if rationale else None,
            }
        )
    canonical = json.dumps(entries, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def bind_approval(ctx: RunContext, case_ids: tuple[str, ...]) -> str:
    """Record the immutable approval snapshot: the case ids and the digest
    of the presented content. Returns the digest."""
    digest = presented_content_digest(ctx, case_ids)
    ctx.approved_case_ids = case_ids
    ctx.approval_digest = digest
    return digest


_RESPONSE_DETAIL_LIMIT = 2000


def response_audit_fields(response: object) -> dict[str, Any]:
    """The attorney's words, reconstructible from the audit trail: stored
    verbatim when bounded; for an enormous response, a bounded excerpt
    plus the full content's digest and length, so the record still proves
    exactly what was said without an unbounded audit row. A non-string
    transport value has no words to record; its type is recorded instead
    (stringifying an arbitrary object would let a stateful __str__ show
    the auditor different words than the parser saw)."""
    if not isinstance(response, str):
        return {"detail_invalid_type": type(response).__name__}
    detail = response.strip()
    if len(detail) <= _RESPONSE_DETAIL_LIMIT:
        return {"detail": detail}
    return {
        "detail_excerpt": detail[:_RESPONSE_DETAIL_LIMIT],
        "detail_sha256": hashlib.sha256(detail.encode()).hexdigest(),
        "detail_length": len(detail),
    }


def parse_attorney_response(response: object) -> tuple[str, str]:
    """Strict, fail-closed parse of an attorney response.

    Returns (action, reason) where action is "approved", "deferred", or
    "invalid". Only an EXACT approval approves and only an EXPLICIT
    "defer..." defers; everything else (empty, non-string, a typo, a
    conditional, an ambiguity) is "invalid" and the caller must treat the
    exchange as unresolved: pending review, never a green no-op. A
    conditional flattened into blanket approval is a UPL-grade audit
    failure; a near-miss flattened into a green deferral silently drops
    urgent cases from the durable queue.
    """
    if not isinstance(response, str):
        return "invalid", f"response must be a string, got {type(response).__name__}"
    detail = response.strip()
    if not detail:
        return "invalid", "empty response; not a decision"
    normalized = detail.lower().rstrip(".!")
    if normalized in ("approve", "approved", "approve all"):
        return "approved", "exact approval"
    # Deferral is a STRUCTURED channel, not free prose: exactly "defer" /
    # "deferred", or the colon form "defer: <reason>". Round 18 tried to
    # adjudicate free-text deferrals by token-matching approval words and
    # lost twice in one round: synonyms sailed through ("defer nothing,
    # commit them all" parsed as a clean green deferral) and the runner's
    # own synthetic deferral text ("...a fresh approval is required") was
    # friendly-fired into invalid. A parser cannot adjudicate intent; it
    # can require a channel. The space form ("defer the rest, approve
    # only X") is therefore NOT a deferral: invalid, exchange unresolved,
    # pending floor, red. A colon-form reason is a deferral by explicit
    # channel choice, whatever prose follows; the verbatim words are
    # audited and reviewed by the console human. Deferral reasons that
    # MENTION approval ("defer: needs partner approval first") are
    # legitimate and no longer false-positive.
    if normalized in ("defer", "deferred") or re.match(r"^defer(?:red)?\s*:", normalized):
        return "deferred", "explicit deferral"
    # A near-miss ("aprove", "yes", a conditional approval) is NOT a
    # decision either way: flattening it into blanket approval is a
    # UPL-grade failure, and flattening it into a green deferral let a
    # typo'd scheduler wrapper produce perpetual green no-op runs with
    # nothing durable recorded (round 17). Invalid = the exchange stays
    # unresolved: the floor delivers the sweep as pending review and the
    # run reads red until a real response resolves it.
    return (
        "invalid",
        "response was not an exact approval or an explicit deferral; "
        "not treated as a decision (fail closed)",
    )


class AttorneyApprovalHook(HookProvider):
    """Pause before committing escalations; a human decides."""

    def __init__(self, ctx: RunContext) -> None:
        self._ctx = ctx

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._approve)

    def _approve(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use["name"] != _GATED_TOOL:
            return
        # Preconditions BEFORE the attorney is interrupted: an approval must
        # bind to a complete, nonempty candidate set. An empty queue or a
        # missing rationale cancels the tool instead of asking a human to
        # approve something unpresentable (which would leave a recorded
        # approval bound to nothing).
        if not self._ctx.interrupt_candidates:
            self._ctx.audit.append(
                AuditEvent(
                    kind="commit_refused",
                    case_id=None,
                    payload={"reason": "no_candidates_at_interrupt"},
                    run_id=self._ctx.run_id,
                )
            )
            event.cancel_tool = (
                "NOTHING TO APPROVE: no interrupt-now cases exist; do not "
                "call commit_escalations on an empty queue."
            )
            return
        unrationalized = [
            d.case_id
            for d in self._ctx.interrupt_candidates
            if d.case_id not in self._ctx.rationales
        ]
        if unrationalized:
            self._ctx.audit.append(
                AuditEvent(
                    kind="commit_refused",
                    case_id=None,
                    payload={
                        "reason": "missing_rationales_at_interrupt",
                        "cases": unrationalized,
                    },
                    run_id=self._ctx.run_id,
                )
            )
            event.cancel_tool = (
                "MISSING RATIONALES: submit_escalation_rationale first for: "
                + ", ".join(unrationalized)
            )
            return
        # Capture the presented-content digest BEFORE the interrupt
        # suspends. Strands re-executes this callback when the interrupt
        # resumes, rebuilding everything from mutable RunContext state, so
        # the digest recorded on the first pass is what proves the state
        # did not shift under the attorney during the pause.
        presented_ids = tuple(d.case_id for d in self._ctx.interrupt_candidates)
        current_digest = presented_content_digest(self._ctx, presented_ids)
        if self._ctx.pending_approval_digest is None:
            self._ctx.pending_approval_digest = current_digest
        elif self._ctx.pending_approval_digest != current_digest:
            # The queue or a rationale changed while the approval was
            # pending: whatever the attorney answered, it was about content
            # that no longer exists. Void the exchange, fail closed. This is
            # NOT an ordinary deferral: no human resolved these candidates,
            # so the invalidation flag keeps the obligation alive (the floor
            # commits them as pending review; the run never reads green).
            self._ctx.attorney_action = "deferred"
            self._ctx.approval_invalidated = True
            self._ctx.pending_approval_digest = None
            self._ctx.audit.append(
                AuditEvent(
                    kind="attorney_decision",
                    case_id=None,
                    payload={
                        "action": "deferred",
                        "approval_invalidated": True,
                        "reason": (
                            "presented content changed while approval was "
                            "pending; a fresh interrupt is required"
                        ),
                    },
                    run_id=self._ctx.run_id,
                )
            )
            event.cancel_tool = (
                "STATE CHANGED DURING APPROVAL: the queue or a rationale "
                "changed while the attorney decision was pending; the "
                "commit was deferred and requires a fresh approval."
            )
            return
        candidates = [
            {
                "case_id": d.case_id,
                "rank": d.rank,
                "days_remaining": d.days_remaining,
                "factors": list(d.factors),
                "flags": list(d.flags),
                "rationale": (
                    self._ctx.rationales[d.case_id].rationale
                    if d.case_id in self._ctx.rationales
                    else None
                ),
            }
            for d in self._ctx.interrupt_candidates
        ]
        response = event.interrupt(
            "attorney-approval",
            reason={
                "question": (
                    "Approve committing these escalations for review? "
                    "Reply exactly 'approve', or 'defer: <reason>'."
                ),
                "escalations": candidates,
                "run_id": self._ctx.run_id,
            },
        )
        # The attorney's actual words are recorded on EVERY branch:
        # verbatim when bounded, digest-anchored when enormous; the audit
        # trail exists to preserve exactly this.
        action, reason = parse_attorney_response(response)
        payload: dict[str, Any] = {
            "action": action,
            "reason": reason,
            **response_audit_fields(response),
        }
        # A RECORDED HUMAN DECISION IS IMMUTABLE FOR THE RUN. The attorney
        # response is single-use, so any later interrupt is answered by the
        # runner's own synthetic text, and letting that text overwrite the
        # decision scalar stranded the approved-recovery path exactly when
        # it was needed: a writer retrying a partially failed commit flipped
        # "approved" to "deferred", so the recovery branch keyed on that
        # scalar never ran and an approved case sat undelivered behind a red
        # run the runner could have healed. The retry is refused and
        # audited; the human's decision stands.
        if self._ctx.approved_case_ids is not None:
            self._ctx.audit.append(
                AuditEvent(
                    kind="commit_refused",
                    case_id=None,
                    payload={
                        "reason": "approval_already_recorded",
                        "retry_response": payload,
                    },
                    run_id=self._ctx.run_id,
                )
            )
            event.cancel_tool = (
                "APPROVAL ALREADY RECORDED: this run's attorney decision was "
                "made and cannot be re-answered. Do NOT retry "
                "commit_escalations; stop and reply COMMITTED."
            )
            return
        if action == "invalid":
            # An empty or non-string value is not a human decision: no
            # deferral is recorded (a transport bug reading as a deferral
            # would drop urgent cases behind a green no-op). The exchange
            # is voided, and the unattended floor delivers the sweep as
            # pending review with the run reading red.
            self._ctx.approval_invalidated = True
            self._ctx.pending_approval_digest = None
            self._ctx.audit.append(
                AuditEvent(
                    kind="attorney_decision",
                    case_id=None,
                    payload=payload,
                    run_id=self._ctx.run_id,
                )
            )
            event.cancel_tool = (
                f"INVALID RESPONSE: {reason}. The commit was not executed; "
                "the sweep will be delivered as pending attorney review. Do "
                "NOT retry commit_escalations; stop and reply DEFERRED."
            )
            return
        self._ctx.attorney_action = action
        if action == "approved":
            # Bind the approval to exactly what was presented: the case ids
            # and the digest captured before the interrupt suspended.
            digest = bind_approval(self._ctx, presented_ids)
            payload["approved_cases"] = list(presented_ids)
            payload["content_digest"] = digest
        self._ctx.pending_approval_digest = None
        self._ctx.audit.append(
            AuditEvent(
                kind="attorney_decision",
                case_id=None,
                payload=payload,
                run_id=self._ctx.run_id,
            )
        )
        if action == "approved":
            return
        event.cancel_tool = f"Attorney deferred the commit: {response}"


class AuditToolHook(HookProvider):
    """Append an audit event after every tool call, success or failure."""

    def __init__(self, ctx: RunContext) -> None:
        self._ctx = ctx

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self._record)

    def _record(self, event: AfterToolCallEvent) -> None:
        result = event.result
        status = result.get("status") if isinstance(result, dict) else None
        payload: dict[str, Any] = {
            "tool": event.tool_use["name"],
            "status": str(status),
        }
        if status == "error" and isinstance(result, dict):
            # Framework-level rejections (e.g. schema validation before the
            # tool body runs) only surface here; keep a bounded excerpt so
            # the audit says WHY, not just that an error happened.
            texts = [
                str(block.get("text", ""))[:300]
                for block in result.get("content", [])
                if isinstance(block, dict) and "text" in block
            ]
            payload["error"] = " | ".join(texts)[:600]
        self._ctx.audit.append(
            AuditEvent(
                kind="tool_call",
                case_id=None,
                payload=payload,
                run_id=self._ctx.run_id,
            )
        )
