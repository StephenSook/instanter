"""Hook providers: the attorney-approval interrupt and the audit trail.

The interrupt is the product moment: committing escalations pauses the run
and hands the decision to a licensed attorney. Approval resumes the tool;
deferral cancels it with the reason recorded. Nothing consequential executes
without the human.
"""

from __future__ import annotations

from typing import Any

from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry

from agent.audit import AuditEvent
from agent.run_context import RunContext

_GATED_TOOL = "commit_escalations"


class AttorneyApprovalHook(HookProvider):
    """Pause before committing escalations; a human decides."""

    def __init__(self, ctx: RunContext) -> None:
        self._ctx = ctx

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._approve)

    def _approve(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use["name"] != _GATED_TOOL:
            return
        candidates = [
            {
                "case_id": d.case_id,
                "rank": d.rank,
                "days_remaining": d.days_remaining,
                "factors": list(d.factors),
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
                    "Reply 'approve' or 'defer: <reason>'."
                ),
                "escalations": candidates,
                "run_id": self._ctx.run_id,
            },
        )
        decision = str(response).strip().lower()
        if decision.startswith("approve"):
            self._ctx.attorney_action = "approved"
            self._ctx.audit.append(
                AuditEvent(
                    kind="attorney_decision",
                    case_id=None,
                    payload={"action": "approved"},
                    run_id=self._ctx.run_id,
                )
            )
            return
        self._ctx.attorney_action = "deferred"
        self._ctx.audit.append(
            AuditEvent(
                kind="attorney_decision",
                case_id=None,
                payload={"action": "deferred", "detail": str(response)},
                run_id=self._ctx.run_id,
            )
        )
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
        self._ctx.audit.append(
            AuditEvent(
                kind="tool_call",
                case_id=None,
                payload={
                    "tool": event.tool_use["name"],
                    "status": str(status),
                },
                run_id=self._ctx.run_id,
            )
        )
