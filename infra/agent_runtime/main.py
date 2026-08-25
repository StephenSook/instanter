"""The real Instanter agent on AgentCore Runtime, suspending for the attorney.

Spike 0001 proved a Strands Graph interrupt survives `@app.entrypoint` and
resumes on fresh compute. This is that mechanism carrying the actual product:
the three-node triage graph, its typed tools, the deterministic ladder, and the
attorney-approval interrupt that is the whole point of the thing.

Two invocations make one run:

* ``start``  loads the intake, runs the graph, and STOPS at the interrupt. It
             returns the cases the attorney is being asked about and nothing
             else happens until a human answers.
* ``resume`` rebuilds the run, restores the graph, applies the human's actual
             words, and finishes through ``_finish_live`` (the deterministic
             floor, the approved-but-uncommitted recovery, packet memos, and
             the report).

## What has to cross the pause, and why

The compute between the two calls is gone, so three things are persisted and
restored or the run's guarantees quietly stop holding:

1. **The run directory** (escalations, run manifests, audit trail). These carry
   the commit idempotency and the durable run identity, so losing them would
   let a resume double-commit.
2. **The Strands graph state**, via `S3SessionManager`, which is what lets the
   interrupted node continue rather than start over.
3. **`rationales` and `pending_approval_digest`.** Strands RE-EXECUTES the
   approval hook when the interrupt resumes, rebuilding the presented content
   from RunContext. If the rationales came back empty the digest would differ
   and every resume would void its own approval; if the digest itself came back
   as ``None`` the hook would SET it instead of VERIFYING it, and the check
   that proves the queue did not shift under the attorney would silently stop
   checking. Persisting both is what keeps that guarantee real.

The run itself is deterministic given (intake, run date, capacity), so the
records, deadlines and ranked queue are rebuilt by recomputation rather than
carried, which is cheaper and cannot drift from the engine.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import date
from pathlib import Path
from typing import Any

import boto3
from bedrock_agentcore import BedrockAgentCoreApp

from agent.audit import JsonlAuditSink
from agent.graph import build_triage_graph
from agent.models import EscalationRationale, ExtractedObservations
from agent.run_context import RunContext
from agent.runner import _finish_live, _load_records
from agent.store import JsonFileCaseStore
from agent.tools import build_tools

REGION = os.environ.get("AWS_REGION", "us-east-1")
STATE_BUCKET = os.environ.get("STATE_BUCKET", "")
WORK = Path("/tmp/instanter-runs")  # the runtime's writable scratch
SEED = Path(__file__).parent / "seed" / "synthetic_intake.json"
SIDECAR = "ctx_state.json"

TASK = (
    "Run the unattended triage sweep for this intake queue: analyze notes, "
    "rank deterministically, escalate through attorney approval."
)

app = BedrockAgentCoreApp()


def _s3() -> Any:
    return boto3.client("s3", region_name=REGION)


def _require_bucket() -> str:
    if not STATE_BUCKET:
        raise RuntimeError(
            "STATE_BUCKET is unset, so a paused run could never be resumed. "
            "Refusing to start a run whose attorney approval would be lost."
        )
    return STATE_BUCKET


def _run_dir(run_id: str) -> Path:
    return WORK / run_id


def _prefix(run_id: str) -> str:
    return f"runs/{run_id}/"


def hydrate(run_id: str) -> int:
    """Pull a run's durable state back down. Returns the object count."""
    bucket = _require_bucket()
    target = _run_dir(run_id)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    pages = _s3().get_paginator("list_objects_v2")
    restored = 0
    for page in pages.paginate(Bucket=bucket, Prefix=_prefix(run_id)):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(_prefix(run_id)) :]
            if not rel:
                continue
            path = target / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            _s3().download_file(bucket, obj["Key"], str(path))
            restored += 1
    return restored


def persist(run_id: str) -> int:
    """Push the run directory up. Returns the object count."""
    bucket = _require_bucket()
    source = _run_dir(run_id)
    written = 0
    for path in sorted(p for p in source.rglob("*") if p.is_file()):
        key = _prefix(run_id) + str(path.relative_to(source))
        _s3().upload_file(str(path), bucket, key)
        written += 1
    return written


def save_sidecar(ctx: RunContext) -> None:
    """Persist exactly the RunContext state a resume cannot recompute."""
    payload = {
        "rationales": {k: v.model_dump() for k, v in ctx.rationales.items()},
        "observations": {k: v.model_dump() for k, v in ctx.observations.items()},
        "pending_approval_digest": ctx.pending_approval_digest,
    }
    (_run_dir(ctx.run_id) / SIDECAR).write_text(json.dumps(payload, default=str))


def load_sidecar(ctx: RunContext) -> bool:
    path = _run_dir(ctx.run_id) / SIDECAR
    if not path.exists():
        return False
    payload = json.loads(path.read_text())
    ctx.rationales = {
        k: EscalationRationale(**v) for k, v in (payload.get("rationales") or {}).items()
    }
    ctx.observations = {
        k: ExtractedObservations(**v) for k, v in (payload.get("observations") or {}).items()
    }
    ctx.pending_approval_digest = payload.get("pending_approval_digest")
    return True


def build_context(run_id: str, run_date: date, capacity: int) -> RunContext:
    directory = _run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    return RunContext(
        run_date=run_date,
        attorney_capacity=capacity,
        store=JsonFileCaseStore(
            intake_path=SEED,
            escalations_path=directory / "escalations.jsonl",
        ),
        audit=JsonlAuditSink(directory / "audit.jsonl"),
        run_id=run_id,
    )


def graph_for(ctx: RunContext) -> Any:
    """One definition of the graph, used by both calls.

    Resuming against changed topology raises, so start and resume must go
    through the same builder with the same session manager.
    """
    from strands.session.s3_session_manager import S3SessionManager

    return build_triage_graph(
        ctx,
        session_manager=S3SessionManager(
            session_id=ctx.run_id,
            bucket=_require_bucket(),
            prefix="graph-sessions",
            region_name=REGION,
        ),
    )


def describe_interrupt(result: Any, ctx: RunContext) -> dict[str, Any]:
    raw = getattr(result, "interrupts", None) or []
    return {
        "status": str(result.status),
        "interrupted": True,
        "interrupts": [
            {"id": i.id, "name": getattr(i, "name", None), "reason": getattr(i, "reason", None)}
            for i in raw
        ],
        "awaiting": [
            {
                "case_id": d.case_id,
                "rank": d.rank,
                "days_remaining": d.days_remaining,
                "factors": list(d.factors),
                "flags": list(d.flags),
                "rationale": (
                    ctx.rationales[d.case_id].rationale if d.case_id in ctx.rationales else None
                ),
            }
            for d in ctx.interrupt_candidates
        ],
        "total_cases": len(ctx.records),
        "refused": sorted(ctx.refused_cases),
    }


def report_dict(report: Any) -> dict[str, Any]:
    return {
        "interrupted": False,
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
    }


@app.entrypoint
def invoke(payload: dict[str, Any], context: Any = None) -> dict[str, Any]:
    action = str(payload.get("action", "start"))
    run_id = str(payload.get("run_id") or "")
    if not run_id:
        return {"error": "run_id_required"}
    capacity = int(payload.get("capacity", 2))
    run_date = (
        date.fromisoformat(str(payload["run_date"]))
        if payload.get("run_date")
        else json_seed_run_date()
    )

    if action == "ping":
        return {
            "ok": True,
            "bucket": STATE_BUCKET,
            "seed_present": SEED.exists(),
            "runtime_session_id": getattr(context, "session_id", None),
        }

    if action == "start":
        ctx = build_context(run_id, run_date, capacity)
        _load_records(ctx)
        graph = graph_for(ctx)
        result = graph(TASK)
        from strands.multiagent import Status

        if result.status == Status.INTERRUPTED:
            save_sidecar(ctx)
            persist(run_id)
            return describe_interrupt(result, ctx)
        # No interrupt raised at all: the model layer never reached the
        # attorney. _finish_live is what decides whether the floor owes the
        # queue a pending-review commit, so it runs on this path too.
        report = _finish_live(ctx, str(result.status), "")
        save_sidecar(ctx)
        persist(run_id)
        return report_dict(report)

    if action == "resume":
        restored = hydrate(run_id)
        if restored == 0:
            return {"error": "no_such_run", "run_id": run_id}
        ctx = build_context(run_id, run_date, capacity)
        _load_records(ctx)
        load_sidecar(ctx)
        tools = build_tools(ctx)
        # Rebuild the ranked queue deterministically. The hook re-executes on
        # resume and reads interrupt_candidates from this.
        tools["get_ranked_queue"]()
        graph = graph_for(ctx)
        answer = str(payload.get("response", ""))
        interrupt_id = str(payload.get("interrupt_id", ""))
        responses = [{"interruptResponse": {"interruptId": interrupt_id, "response": answer}}]
        graph_status = "NOT_RUN"
        model_error = ""
        try:
            result = graph(responses)
            graph_status = str(result.status)
        except Exception as exc:  # the floor still owes the queue a decision
            graph_status = f"RAISED:{type(exc).__name__}"
            model_error = f"{type(exc).__name__}: {exc}"[:400]
        report = _finish_live(ctx, graph_status, model_error)
        save_sidecar(ctx)
        persist(run_id)
        return report_dict(report)

    return {"error": "unknown_action", "action": action}


def json_seed_run_date() -> date:
    return date.fromisoformat(json.loads(SEED.read_text())["demo_run_date"])


if __name__ == "__main__":
    app.run()
