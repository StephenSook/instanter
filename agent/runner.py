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


def _apply_attorney_decision(ctx: RunContext, tools: dict[str, Any], response: str) -> None:
    """Execute the attorney's decision through the same commit tool the
    writer agent calls: one code path for store writes, partial-failure
    handling, and audit events."""
    if response.strip().lower().startswith("approve"):
        ctx.attorney_action = "approved"
        tools["commit_escalations"]()
    else:
        ctx.attorney_action = "deferred"
        ctx.audit.append(
            AuditEvent(
                kind="attorney_decision",
                case_id=None,
                payload={"action": "deferred", "detail": response},
                run_id=ctx.run_id,
            )
        )


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
    attorney_response: str = "approve",
    plugins: list[Any] | None = None,
) -> RunReport:
    """Full graph run with the attorney interrupt resumed in-session.

    The multi-day persist-and-reinvoke wait ships with the Phase C
    infrastructure; here the attorney's response arrives as an argument
    (the console supplies it interactively in the deployed product).
    ``plugins`` passes through to the graph for the evals chaos harness.
    """
    from strands.multiagent import Status
    from strands.types.interrupt import InterruptResponseContent

    from agent.graph import build_triage_graph

    _load_records(ctx)
    graph = build_triage_graph(ctx, plugins=plugins)
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

    # Deterministic floor: chaos testing showed the model layer can end a
    # run without ever reaching the attorney (a node dies, or the writer
    # declines to act when upstream context is degraded). Undertriage is
    # the catastrophic error, so if no attorney decision was recorded, the
    # runner itself completes the sweep deterministically, loudly audited.
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
                        "graph_status": str(result.status),
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

    ctx.audit.append(
        AuditEvent(
            kind="run_finished",
            case_id=None,
            payload={"status": str(result.status), "backstop_used": backstop_used},
            run_id=ctx.run_id,
        )
    )
    return _report(ctx, mode="live", backstop_used=backstop_used)


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


def _report(ctx: RunContext, mode: str, backstop_used: bool = False) -> RunReport:
    return RunReport(
        run_id=ctx.run_id,
        mode=mode,
        total_cases=len(ctx.records),
        interrupts=tuple(d.case_id for d in ctx.interrupt_candidates),
        committed=ctx.committed_case_ids,
        attorney_action=ctx.attorney_action,
        audit_path="",
        backstop_used=backstop_used,
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
                "backstop_used": report.backstop_used,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
