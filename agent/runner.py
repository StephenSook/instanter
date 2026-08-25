"""Unattended run entrypoint.

Two modes:
* ``live``: the full graph (three model nodes, attorney interrupt). This is
  what the scheduled trigger invokes and what the demo films.
* ``deterministic``: no model calls at all. The same store, engine, ladder,
  audit, and escalation writes run end to end with templated rationales
  clearly labeled as model-disabled. CI exercises this mode (no AWS
  credentials in CI); the live model path is smoked separately per the
  live-validation rule.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from agent.audit import AuditEvent, JsonlAuditSink
from agent.models import EscalationRationale
from agent.run_context import RunContext
from agent.store import JsonFileCaseStore
from agent.tools import build_tools
from agent.triage import TriageDecision


@dataclass(frozen=True)
class RunReport:
    run_id: str
    mode: str
    total_cases: int
    interrupts: tuple[str, ...]
    committed: tuple[str, ...]
    attorney_action: str
    audit_path: str
    backstop_used: bool = False
    model_error: str = ""
    # Interrupt-now cases the attorney approved that are NOT durably
    # committed. An attorney decision is a decision, never proof the commit
    # succeeded; parity between these two sets is the run's success
    # criterion, and main() exits non-zero when it does not hold.
    failures: tuple[str, ...] = ()
    # Cases the sweep REFUSED to compute (unparseable intake): the sweep
    # could not protect them, so they fail the run exactly like a lost
    # commit does. A scheduler must never see green over a dropped case.
    refused: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return not self.failures and not self.model_error and not self.refused


def _load_records(ctx: RunContext) -> None:
    if ctx.started:
        raise RuntimeError(
            f"RunContext {ctx.run_id} already carried a run; contexts are "
            "single-use. Construct a fresh context (and run_id) per invocation."
        )
    ctx.started = True
    for record in ctx.store.load_intake():
        ctx.records[record.case_id] = record
    ctx.audit.append(
        AuditEvent(
            kind="run_started",
            case_id=None,
            payload={
                "mode_records": len(ctx.records),
                "run_date": ctx.run_date.isoformat(),
                "capacity": ctx.attorney_capacity,
            },
            run_id=ctx.run_id,
        )
    )


def _apply_attorney_decision(ctx: RunContext, tools: dict[str, Any], response: str) -> None:
    """Execute the attorney's decision through the same commit tool the
    writer agent calls: one code path for store writes, partial-failure
    handling, and audit events. Parsing is the same strict fail-closed
    parse the approval hook uses."""
    from agent.hooks import parse_attorney_response

    action, reason = parse_attorney_response(response)
    ctx.attorney_action = action
    if action == "approved" and ctx.approved_case_ids is None:
        # Bind the approval to the presented candidates, same as the hook.
        ctx.approved_case_ids = tuple(d.case_id for d in ctx.interrupt_candidates)
    ctx.audit.append(
        AuditEvent(
            kind="attorney_decision",
            case_id=None,
            payload={
                "action": action,
                "detail": response.strip()[:400],
                "reason": reason,
                "approved_cases": list(ctx.approved_case_ids or ()),
            },
            run_id=ctx.run_id,
        )
    )
    if action == "approved":
        tools["commit_escalations"]()


def _deterministic_floor(ctx: RunContext, tools: dict[str, Any], attorney_response: str) -> None:
    """The part of the sweep that must NEVER depend on model discretion:
    compute every deadline, rank the queue, put every interrupt-now case in
    front of the attorney (template rationale if the model never wrote one),
    and execute the attorney's decision."""
    if not ctx.decisions:
        tools["get_ranked_queue"]()
    for decision in ctx.interrupt_candidates:
        if decision.case_id in ctx.rationales:
            continue
        ctx.rationales[decision.case_id] = _template_rationale(decision)
        ctx.audit.append(
            AuditEvent(
                kind="rationale_recorded",
                case_id=decision.case_id,
                payload={"template": True},
                run_id=ctx.run_id,
            )
        )
    if ctx.interrupt_candidates:
        _apply_attorney_decision(ctx, tools, attorney_response)


def run_deterministic(ctx: RunContext, attorney_response: str = "approve") -> RunReport:
    """Model-free end-to-end run: engine + ladder + escalation writes."""
    _load_records(ctx)
    tools = build_tools(ctx)
    _deterministic_floor(ctx, tools, attorney_response)
    return _report(ctx, mode="deterministic")


def run_live(
    ctx: RunContext,
    attorney_response: str,
    plugins: list[Any] | None = None,
) -> RunReport:
    """Full graph run with the attorney interrupt resumed in-session.

    ``attorney_response`` is deliberately required, never defaulted: a live
    path that auto-approves its own human interrupt is not human-in-the-
    loop. The caller supplies the human's actual response (the console in
    the deployed product; an explicit flag in the demo harness), and the
    multi-day persist-and-reinvoke wait ships with the Phase C
    infrastructure. ``plugins`` passes through for the evals chaos harness.
    """
    from strands.multiagent import Status
    from strands.types.interrupt import InterruptResponseContent

    from agent.graph import build_triage_graph

    _load_records(ctx)
    graph = build_triage_graph(ctx, plugins=plugins)

    # Failure boundary: Strands re-raises node exceptions and node timeouts
    # (fail-fast by design), so without this boundary a Bedrock outage or a
    # 300s node timeout would exit run_live before the deterministic floor
    # ever ran. The model layer's death is audited, then the floor runs.
    graph_status = "NOT_RUN"
    model_error = ""
    try:
        result = graph(
            "Run the unattended triage sweep for this intake queue: analyze "
            "notes, rank deterministically, escalate through attorney approval."
        )
        while result.status == Status.INTERRUPTED:
            responses: list[InterruptResponseContent] = [
                {
                    "interruptResponse": {
                        "interruptId": interrupt.id,
                        "response": attorney_response,
                    }
                }
                for interrupt in result.interrupts
            ]
            result = graph(responses)
        graph_status = str(result.status)
    except Exception as exc:
        graph_status = f"RAISED:{type(exc).__name__}"
        model_error = f"{type(exc).__name__}: {exc}"[:400]
        ctx.audit.append(
            AuditEvent(
                kind="model_error",
                case_id=None,
                payload={"error": model_error, "phase": "graph"},
                run_id=ctx.run_id,
            )
        )

    # Deterministic floor: chaos testing showed the model layer can end a
    # run without ever reaching the attorney (a node dies, the writer
    # declines to act on degraded context, or the graph raises). Undertriage
    # is the catastrophic error, so if no attorney decision was recorded,
    # the runner itself completes the sweep deterministically, audited.
    backstop_used = False
    if not ctx.attorney_action:
        tools = build_tools(ctx)
        if ctx.interrupt_candidates or not ctx.decisions:
            backstop_used = True
            ctx.audit.append(
                AuditEvent(
                    kind="deterministic_backstop",
                    case_id=None,
                    payload={
                        "graph_status": graph_status,
                        "had_ranked_queue": bool(ctx.decisions),
                        "reason": (
                            "model layer ended the run without an attorney "
                            "decision; completing the sweep deterministically"
                        ),
                    },
                    run_id=ctx.run_id,
                )
            )
            _deterministic_floor(ctx, tools, attorney_response)
    elif ctx.attorney_action == "approved":
        # Approval is a decision, not a completed commit: an exception after
        # the hook approved but before the writes finished leaves approved
        # cases undelivered. Recover through the commit tool, which
        # reconciles against DURABLE store state first, so recovery can
        # never duplicate a row that actually landed.
        tools = build_tools(ctx)
        if not ctx.decisions:
            tools["get_ranked_queue"]()
        # Recovery is bounded by the approval snapshot: only what the
        # attorney actually approved may be delivered under this approval.
        missing = [
            case_id
            for case_id in (ctx.approved_case_ids or ())
            if case_id not in ctx.committed_case_ids
        ]
        if missing:
            backstop_used = True
            ctx.audit.append(
                AuditEvent(
                    kind="deterministic_backstop",
                    case_id=None,
                    payload={
                        "graph_status": graph_status,
                        "reason": (
                            "attorney approved but the commit did not complete "
                            "for every case; recovering through the reconciling "
                            "commit tool"
                        ),
                        "missing_before_recovery": missing,
                    },
                    run_id=ctx.run_id,
                )
            )
            for decision in ctx.interrupt_candidates:
                if decision.case_id in ctx.rationales:
                    continue
                ctx.rationales[decision.case_id] = _template_rationale(decision)
                ctx.audit.append(
                    AuditEvent(
                        kind="rationale_recorded",
                        case_id=decision.case_id,
                        payload={"template": True},
                        run_id=ctx.run_id,
                    )
                )
            tools["commit_escalations"]()

    report = _report(ctx, mode="live", backstop_used=backstop_used, model_error=model_error)
    ctx.audit.append(
        AuditEvent(
            kind="run_finished",
            case_id=None,
            payload={
                "status": graph_status,
                "backstop_used": backstop_used,
                "failures": list(report.failures),
                "model_error": model_error,
            },
            run_id=ctx.run_id,
        )
    )
    return report


def _template_rationale(decision: TriageDecision) -> EscalationRationale:
    return EscalationRationale(
        case_id=decision.case_id,
        disposition=decision.level.value,
        contributing_factors=list(decision.factors)[:8],
        rationale=(
            "[MODEL DISABLED: templated rationale] This case ranks "
            f"{decision.rank} this run. " + " ".join(decision.factors)
        )[:900],
        confidence=1.0,
    )


def _report(
    ctx: RunContext, mode: str, backstop_used: bool = False, model_error: str = ""
) -> RunReport:
    # Parity check: an approved decision whose commit did not durably land
    # for every APPROVED case is a FAILED run, whatever else happened. The
    # approval snapshot is the reference (never the current queue, which
    # recovery may have recomputed); an approval bound to no snapshot is
    # itself a failure.
    failures: tuple[str, ...] = ()
    if ctx.attorney_action == "approved":
        if ctx.approved_case_ids is None:
            failures = ("approval-not-bound-to-candidates",)
        else:
            failures = tuple(
                case_id
                for case_id in ctx.approved_case_ids
                if case_id not in ctx.committed_case_ids
            )
    return RunReport(
        run_id=ctx.run_id,
        mode=mode,
        total_cases=len(ctx.records),
        interrupts=tuple(d.case_id for d in ctx.interrupt_candidates),
        committed=ctx.committed_case_ids,
        attorney_action=ctx.attorney_action,
        audit_path="",
        backstop_used=backstop_used,
        model_error=model_error,
        failures=failures,
        refused=tuple(sorted(ctx.refused_cases)),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Instanter unattended triage run")
    parser.add_argument("--seed", default="seed/synthetic_intake.json")
    parser.add_argument("--out-dir", default="out")
    parser.add_argument("--run-date", default=None, help="ISO date; defaults to today")
    parser.add_argument("--capacity", type=int, default=2)
    parser.add_argument("--mode", choices=["live", "deterministic"], default="live")
    parser.add_argument(
        "--attorney-response",
        default=None,
        help=(
            "the attorney's exact response: 'approve' or 'defer: <reason>'. "
            "REQUIRED in live mode (a live run never auto-approves its own "
            "interrupt); deterministic mode defaults to 'approve' as a CI "
            "harness convenience."
        ),
    )
    args = parser.parse_args()
    if args.mode == "live" and args.attorney_response is None:
        parser.error(
            "--attorney-response is required in live mode: the human's "
            "response is an input, never a default"
        )
    if args.attorney_response is None:
        args.attorney_response = "approve"

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_date = date.fromisoformat(args.run_date) if args.run_date else date.today()

    store = JsonFileCaseStore(
        intake_path=Path(args.seed),
        escalations_path=out_dir / "escalations.jsonl",
    )
    audit = JsonlAuditSink(out_dir / "audit.jsonl")
    ctx = RunContext(
        run_date=run_date,
        attorney_capacity=args.capacity,
        store=store,
        audit=audit,
    )

    if args.mode == "deterministic":
        report = run_deterministic(ctx, args.attorney_response)
    else:
        report = run_live(ctx, args.attorney_response)

    print(
        json.dumps(
            {
                "run_id": report.run_id,
                "mode": report.mode,
                "total_cases": report.total_cases,
                "interrupts": list(report.interrupts),
                "committed": list(report.committed),
                "attorney_action": report.attorney_action,
                "backstop_used": report.backstop_used,
                "model_error": report.model_error,
                "failures": list(report.failures),
                "refused": list(report.refused),
                "succeeded": report.succeeded,
            },
            indent=2,
        )
    )
    # A scheduler must never see green on a run that lost an escalation or
    # a model layer; the floor completing the sweep does not un-fail the
    # run, it bounds the damage.
    if not report.succeeded:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
