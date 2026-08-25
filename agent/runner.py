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
import hashlib
import json
from dataclasses import asdict, dataclass
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
    # Committed cases whose attorney packet lacks a cover memo: an
    # incomplete packet is an incomplete run.
    missing_memos: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        # A backstop run delivered the sweep but the model layer did not do
        # its job: that is a DEGRADED run, and a scheduler must hear about
        # it even when every escalation landed.
        return (
            not self.failures
            and not self.model_error
            and not self.refused
            and not self.missing_memos
            and not self.backstop_used
        )


def _load_records(ctx: RunContext) -> None:
    if ctx.started:
        raise RuntimeError(
            f"RunContext {ctx.run_id} already carried a run; contexts are "
            "single-use. Construct a fresh context (and run_id) per invocation."
        )
    ctx.started = True
    loaded = ctx.store.load_intake()
    # Malformed RAW rows (unconstructible: wrong shape, unknown fields) are
    # case-level refusals exactly like unparseable field values: audited,
    # surfaced in the report, failing the run, never aborting the sweep of
    # the rows that did parse.
    for malformed_row in loaded.malformed:
        ctx.refused_cases[malformed_row.row_key] = malformed_row.reason
        ctx.audit.append(
            AuditEvent(
                kind="case_refused",
                case_id=malformed_row.row_key,
                payload={"reason": malformed_row.reason},
                run_id=ctx.run_id,
            )
        )
    for record in loaded.records:
        ctx.records[record.case_id] = record
    # Canonical digest of this invocation's inputs, recorded beside the run
    # id: a stable id promises the SAME inputs, and an at-least-once retry
    # over mutated intake is diagnosable from the audit trail alone (the
    # commit tool independently refuses durable rows outside the retry's
    # candidate set).
    inputs_digest = hashlib.sha256(
        json.dumps(
            {
                "run_date": ctx.run_date.isoformat(),
                "capacity": ctx.attorney_capacity,
                "records": [asdict(r) for r in sorted(loaded.records, key=lambda r: r.case_id)],
                "malformed": [[m.row_key, m.reason] for m in loaded.malformed],
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    ctx.inputs_digest = inputs_digest
    ctx.audit.append(
        AuditEvent(
            kind="run_started",
            case_id=None,
            payload={
                "mode_records": len(ctx.records),
                "malformed_rows": len(loaded.malformed),
                "run_date": ctx.run_date.isoformat(),
                "capacity": ctx.attorney_capacity,
                "inputs_digest": inputs_digest,
            },
            run_id=ctx.run_id,
        )
    )


def _apply_attorney_decision(ctx: RunContext, tools: dict[str, Any], response: str) -> None:
    """Execute the attorney's decision through the same commit tool the
    writer agent calls: one code path for store writes, partial-failure
    handling, and audit events. Parsing is the same strict fail-closed
    parse the approval hook uses."""
    from agent.hooks import bind_approval, parse_attorney_response, response_audit_fields

    action, reason = parse_attorney_response(response)
    if action == "invalid":
        # Not a human decision: no deferral is recorded, the exchange is
        # voided, and the report's outstanding obligation keeps the run
        # red until a real response resolves the candidates.
        ctx.approval_invalidated = True
        ctx.audit.append(
            AuditEvent(
                kind="attorney_decision",
                case_id=None,
                payload={
                    "action": action,
                    "reason": reason,
                    **response_audit_fields(response),
                },
                run_id=ctx.run_id,
            )
        )
        return
    ctx.attorney_action = action
    if action == "approved" and ctx.approved_case_ids is None:
        # Bind the approval to the presented candidates, same as the hook:
        # snapshot ids plus the presented-content digest.
        bind_approval(ctx, tuple(d.case_id for d in ctx.interrupt_candidates))
    ctx.audit.append(
        AuditEvent(
            kind="attorney_decision",
            case_id=None,
            payload={
                "action": action,
                "reason": reason,
                "approved_cases": list(ctx.approved_case_ids or ()),
                **response_audit_fields(response),
            },
            run_id=ctx.run_id,
        )
    )
    if action == "approved":
        tools["commit_escalations"]()


def _ensure_packet_memos(ctx: RunContext, tools: dict[str, Any]) -> None:
    """Every committed escalation must carry an attorney-packet cover memo.
    The memo's fact sheet is generated deterministically on EVERY path (the
    drafter only ever adds reviewer notes), so when the drafter never ran
    or shortchanged a case, writing the memo with empty notes completes the
    packet; the report's memo parity remains the final net."""
    for case_id in ctx.committed_case_ids:
        if case_id in ctx.packet_memos:
            continue
        tools["write_packet_memo"](case_id=case_id, notes="")


def _rank_and_template(ctx: RunContext, tools: dict[str, Any]) -> None:
    """Deterministic prelude shared by every floor path: rank if needed,
    template any missing rationale."""
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


def _deterministic_floor(ctx: RunContext, tools: dict[str, Any], attorney_response: str) -> None:
    """The presented-response floor (run_deterministic: the invoking human's
    realtime response IS the decision): rank, template missing rationales,
    execute the decision, complete the attorney packet."""
    _rank_and_template(ctx, tools)
    if ctx.interrupt_candidates:
        _apply_attorney_decision(ctx, tools, attorney_response)
        _ensure_packet_memos(ctx, tools)


def _pending_commit(ctx: RunContext, tools: dict[str, Any]) -> None:
    """The unattended floor: nobody was presented anything, so no approval
    is claimed. Interrupt-now cases are committed as PENDING attorney
    review under the runner's explicit floor authority, packet completed
    with template memos, everything audited."""
    _rank_and_template(ctx, tools)
    if not ctx.interrupt_candidates:
        return
    ctx.attorney_action = "pending"
    ctx.floor_commit_authorized = True
    try:
        ctx.audit.append(
            AuditEvent(
                kind="pending_review_commit",
                case_id=None,
                payload={
                    "cases": [d.case_id for d in ctx.interrupt_candidates],
                    "reason": (
                        "committing the sweep for attorney review without a "
                        "realtime approval (no human was presented this "
                        "queue); the commit events that follow record the "
                        "outcome"
                    ),
                },
                run_id=ctx.run_id,
            )
        )
        tools["commit_escalations"]()
        _ensure_packet_memos(ctx, tools)
    finally:
        ctx.floor_commit_authorized = False


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
        # The supplied response is ONE human decision: it answers exactly
        # one interrupt. Any further interrupt in the same run (a retry
        # after a digest mismatch, a second commit attempt) gets a fail-
        # closed deferral instead of a replayed approval the human never
        # gave for that content.
        response_consumed = False
        # Hard bound on the interrupt cycle: the graph's own caps cannot
        # bound it (an interrupted node execution returns before it is
        # counted toward max_node_executions, and the execution timeout
        # resets on every resume), so a writer that keeps retrying the
        # commit would otherwise spin interrupt -> synthetic answer ->
        # retry forever, burning model calls with no floor, no report,
        # and no exit. After the cap the run is a model error; the floor
        # below still delivers the sweep.
        interrupt_rounds = 0
        while result.status == Status.INTERRUPTED:
            interrupt_rounds += 1
            if interrupt_rounds > 3:
                model_error = (
                    "interrupt limit reached: the graph raised more than 3 "
                    "interrupts in one run; treating the model layer as failed"
                )
                ctx.audit.append(
                    AuditEvent(
                        kind="model_error",
                        case_id=None,
                        payload={"error": model_error, "phase": "interrupt_limit"},
                        run_id=ctx.run_id,
                    )
                )
                graph_status = "INTERRUPT_LIMIT"
                break
            responses: list[InterruptResponseContent] = []
            for interrupt in result.interrupts:
                if response_consumed:
                    answer = (
                        "defer: the single-use attorney response was already "
                        "consumed this run; a fresh approval is required"
                    )
                else:
                    answer = attorney_response
                    response_consumed = True
                responses.append(
                    {
                        "interruptResponse": {
                            "interruptId": interrupt.id,
                            "response": answer,
                        }
                    }
                )
            result = graph(responses)
        if graph_status != "INTERRUPT_LIMIT":
            graph_status = str(result.status)
            if result.status != Status.COMPLETED:
                # A FAILED terminal status is a model-layer death even though
                # nothing raised; the floor may still bound the damage, but
                # the run must never read green over it.
                model_error = f"graph ended with terminal status {result.status}"
                ctx.audit.append(
                    AuditEvent(
                        kind="model_error",
                        case_id=None,
                        payload={"error": model_error, "phase": "graph_terminal_status"},
                        run_id=ctx.run_id,
                    )
                )
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
    # the runner itself completes the sweep and commits the interrupt-now
    # cases as PENDING attorney review. No approval is claimed: the launch
    # response answered a presented interrupt or nothing; a commit nobody
    # saw is pending, and the console review is where it gets its human.
    backstop_used = False
    tools: dict[str, Any] | None = None
    if not ctx.attorney_action or ctx.approval_invalidated:
        # An invalidated approval left candidates NO human resolved (the
        # recorded deferral is synthetic); the unattended floor owes them a
        # pending-review commit exactly as if no decision existed.
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
                            "decision; committing the sweep as pending review"
                        ),
                    },
                    run_id=ctx.run_id,
                )
            )
            _pending_commit(ctx, tools)
    elif ctx.approved_case_ids is not None:
        # Approval is a decision, not a completed commit: an exception after
        # the hook approved but before the writes finished leaves approved
        # cases undelivered. Recover through the commit tool, which
        # reconciles against DURABLE store state first, so recovery can
        # never duplicate a row that actually landed. Keyed on the BOUND
        # APPROVAL (the immutable obligation the report's parity also uses),
        # not on the mutable action scalar: a later write to that scalar
        # must never strand the recovery this branch exists to perform.
        ctx.attorney_action = "approved"
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

    # Attorney-packet completeness: every committed case carries a memo,
    # whichever path committed it (drafter, floor, or recovery).
    if (
        ctx.attorney_action in ("approved", "pending") or ctx.approved_case_ids is not None
    ) and ctx.committed_case_ids:
        if tools is None:
            tools = build_tools(ctx)
        _ensure_packet_memos(ctx, tools)

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
    # Parity check: once a commit was owed (approved or pending), EVERY
    # case that needed delivery must be durably committed: the approval
    # snapshot AND every current interrupt-now candidate. A case minted
    # after the approval that could not ride it is still an undelivered
    # urgent case; it fails the run until a fresh approval resolves it.
    failures: tuple[str, ...] = ()
    obligation_outstanding = (
        ctx.attorney_action in ("approved", "pending")
        # A bound approval is an OBLIGATION: a later response (including the
        # synthetic single-use deferral on a retry interrupt) never erases
        # it. If the approved cases are not durably committed, the run
        # failed, whatever the last recorded action says.
        or ctx.approved_case_ids is not None
        # A voided approval is equally outstanding: nobody resolved the
        # candidates, so they must be durably committed (pending review) or
        # the run failed.
        or ctx.approval_invalidated
    )
    if obligation_outstanding:
        owed: list[str] = []
        if ctx.attorney_action == "approved" and ctx.approved_case_ids is None:
            owed.append("approval-not-bound-to-candidates")
        for case_id in ctx.approved_case_ids or ():
            if case_id not in ctx.committed_case_ids and case_id not in owed:
                owed.append(case_id)
        # Current-candidate parity runs under ANY outstanding obligation,
        # including a bound approval later overwritten by the synthetic
        # single-use deferral: an urgent case no human resolved must never
        # hide behind the latest scalar action.
        for decision in ctx.interrupt_candidates:
            if decision.case_id not in ctx.committed_case_ids and decision.case_id not in owed:
                owed.append(decision.case_id)
        failures = tuple(owed)
    missing_memos: tuple[str, ...] = ()
    if obligation_outstanding:
        missing_memos = tuple(
            case_id for case_id in ctx.committed_case_ids if case_id not in ctx.packet_memos
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
        missing_memos=missing_memos,
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
    parser.add_argument(
        "--run-id",
        default=None,
        help=(
            "stable scheduler invocation id (e.g. the EventBridge event id). "
            "An at-least-once retry MUST reuse the id so its commit "
            "reconciles against durable state instead of duplicating "
            "escalations under a fresh random id. Omitted: a fresh id."
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

    if args.capacity < 1:
        parser.error(f"--capacity must be >= 1, got {args.capacity}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # None means the flag was omitted (default to today). An explicitly
    # passed value must parse or fail LOUDLY: --run-date "" (an unset shell
    # variable) previously fell through to today, silently swapping the
    # deadline engine's clock anchor.
    if args.run_date is None:
        run_date = date.today()
    else:
        try:
            run_date = date.fromisoformat(args.run_date)
        except ValueError:
            parser.error(f"--run-date {args.run_date!r} is not an ISO date")

    store = JsonFileCaseStore(
        intake_path=Path(args.seed),
        escalations_path=out_dir / "escalations.jsonl",
    )
    audit = JsonlAuditSink(out_dir / "audit.jsonl")
    ctx_kwargs: dict[str, str] = {}
    if args.run_id is not None:
        ctx_kwargs["run_id"] = args.run_id
    ctx = RunContext(
        run_date=run_date,
        attorney_capacity=args.capacity,
        store=store,
        audit=audit,
        **ctx_kwargs,  # type: ignore[arg-type]
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
                "missing_memos": list(report.missing_memos),
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
