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

from agent.audit import AuditEvent, JsonlAuditSink
from agent.models import EscalationRationale
from agent.run_context import RunContext
from agent.store import EscalationRecord, JsonFileCaseStore
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


def _load_records(ctx: RunContext) -> None:
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


def run_deterministic(ctx: RunContext, attorney_response: str = "approve") -> RunReport:
    """Model-free end-to-end run: engine + ladder + escalation writes."""
    _load_records(ctx)
    tools = build_tools(ctx)
    # The same tool the writer agent calls, invoked directly: deterministic
    # by construction, and the audit trail is identical in shape.
    tools["get_ranked_queue"]()

    for decision in ctx.interrupt_candidates:
        ctx.rationales[decision.case_id] = _template_rationale(decision)
        ctx.audit.append(
            AuditEvent(
                kind="rationale_recorded",
                case_id=decision.case_id,
                payload={"template": True},
                run_id=ctx.run_id,
            )
        )

    if attorney_response.strip().lower().startswith("approve"):
        ctx.attorney_action = "approved"
        committed: list[str] = []
        for decision in ctx.interrupt_candidates:
            rationale = ctx.rationales[decision.case_id]
            ctx.store.record_escalation(
                EscalationRecord(
                    case_id=decision.case_id,
                    disposition=decision.level.value,
                    rank=decision.rank,
                    factors=decision.factors,
                    rationale=rationale.rationale,
                    confidence=rationale.confidence,
                    run_id=ctx.run_id,
                )
            )
            committed.append(decision.case_id)
        ctx.committed_case_ids = tuple(committed)
        ctx.audit.append(
            AuditEvent(
                kind="escalation_committed",
                case_id=None,
                payload={"cases": committed, "attorney_action": "approved"},
                run_id=ctx.run_id,
            )
        )
    else:
        ctx.attorney_action = "deferred"
        ctx.audit.append(
            AuditEvent(
                kind="attorney_decision",
                case_id=None,
                payload={"action": "deferred", "detail": attorney_response},
                run_id=ctx.run_id,
            )
        )

    return _report(ctx, mode="deterministic")


def run_live(ctx: RunContext, attorney_response: str = "approve") -> RunReport:
    """Full graph run with the attorney interrupt resumed in-session.

    The multi-day persist-and-reinvoke wait ships with the Phase C
    infrastructure; here the attorney's response arrives as an argument
    (the console supplies it interactively in the deployed product).
    """
    from strands.multiagent import Status
    from strands.types.interrupt import InterruptResponseContent

    from agent.graph import build_triage_graph

    _load_records(ctx)
    graph = build_triage_graph(ctx)
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
    ctx.audit.append(
        AuditEvent(
            kind="run_finished",
            case_id=None,
            payload={"status": str(result.status)},
            run_id=ctx.run_id,
        )
    )
    return _report(ctx, mode="live")


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


def _report(ctx: RunContext, mode: str) -> RunReport:
    return RunReport(
        run_id=ctx.run_id,
        mode=mode,
        total_cases=len(ctx.records),
        interrupts=tuple(d.case_id for d in ctx.interrupt_candidates),
        committed=ctx.committed_case_ids,
        attorney_action=ctx.attorney_action,
        audit_path="",
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
        default="approve",
        help="'approve' or 'defer: <reason>' (demo short-path)",
    )
    args = parser.parse_args()

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
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
