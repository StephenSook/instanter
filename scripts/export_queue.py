"""Export one real sweep as the console's fail-closed data snapshot.

The web console renders ENGINE OUTPUT, never invented props. Its primary
source is the live door (`GET /api/queue`); the snapshot this script writes
is only the labelled fallback the cabinet shows when the door is
unreachable. With AWS credentials this script prefers a LIVE sweep, so the
snapshot carries real model rationales (`"mode": "live"`); without them it
falls back to the deterministic path CI runs, and the rationale fields say
[MODEL DISABLED] rather than pretending. Either way every field the UI shows
(the ranked queue, each case's statutory computation trace, its flags with
the engine's own reason texts, and the run's honest outcome) goes to
web/public/queue.json.

Regenerate with:

    .venv/bin/python scripts/export_queue.py
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from pathlib import Path

from agent.audit import JsonlAuditSink
from agent.run_context import RunContext
from agent.runner import run_deterministic
from agent.store import JsonFileCaseStore

ROOT = Path(__file__).parent.parent
SEED = ROOT / "seed" / "synthetic_intake.json"
OUT_DIR = ROOT / "out" / "console"
TARGET = ROOT / "web" / "public" / "queue.json"
RUN_DATE = date(2026, 9, 9)  # the seed's demo_run_date
CAPACITY = 2


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for stale in ("escalations.jsonl", "audit.jsonl"):
        (OUT_DIR / stale).unlink(missing_ok=True)

    ctx = RunContext(
        run_date=RUN_DATE,
        attorney_capacity=CAPACITY,
        store=JsonFileCaseStore(
            intake_path=SEED,
            escalations_path=OUT_DIR / "escalations.jsonl",
        ),
        audit=JsonlAuditSink(OUT_DIR / "audit.jsonl"),
    )

    # Prefer a LIVE sweep so the console shows the rationales the model
    # actually wrote for the attorney. Without credentials (or if Bedrock
    # is throttling) fall back to the deterministic path, which produces
    # the same decisions with clearly-labelled template rationales: the
    # console then says so rather than passing templates off as prose.
    mode = "live"
    try:
        from agent.runner import run_live

        report = run_live(ctx, attorney_response="approve")
        if report.model_error:
            raise RuntimeError(report.model_error)
    except Exception as exc:
        print(f"live sweep unavailable ({str(exc)[:120]}); using the deterministic path")
        mode = "deterministic"
        ctx = RunContext(
            run_date=RUN_DATE,
            attorney_capacity=CAPACITY,
            store=JsonFileCaseStore(
                intake_path=SEED,
                escalations_path=OUT_DIR / "escalations-det.jsonl",
            ),
            audit=JsonlAuditSink(OUT_DIR / "audit-det.jsonl"),
        )
        report = run_deterministic(ctx, attorney_response="approve")

    cases = []
    for decision in ctx.decisions:
        deadline = ctx.deadlines.get(decision.case_id)
        record = ctx.records.get(decision.case_id)
        cases.append(
            {
                "case_id": decision.case_id,
                "level": decision.level.value,
                "floor_level": decision.floor_level.value,
                "rank": decision.rank,
                "days_remaining": decision.days_remaining,
                "interrupt_now": decision.interrupt_now,
                "held_reason": decision.held_reason,
                "raised_by": list(decision.raised_by),
                "factors": list(decision.factors),
                "flags": [
                    {
                        "code": f.code.value,
                        "reason": f.reason,
                        "day": f.day.isoformat() if f.day else None,
                    }
                    for f in (deadline.flags if deadline else ())
                ],
                "effective_deadline": (
                    deadline.effective_deadline.isoformat()
                    if deadline and deadline.effective_deadline
                    else None
                ),
                "computed_deadline": (
                    deadline.computed_deadline.isoformat()
                    if deadline and deadline.computed_deadline
                    else None
                ),
                "deadline_basis": deadline.deadline_basis.value if deadline else "none",
                "citation": deadline.citation if deadline else "",
                "court_reopens_on": (
                    deadline.court_reopens_on.isoformat()
                    if deadline and deadline.court_reopens_on
                    else None
                ),
                # The computation itself, day by day: this is what the packet
                # prints as a court record.
                "trace": [
                    {"day": step.day.isoformat(), "label": step.label}
                    for step in (deadline.trace if deadline else ())
                ],
                "service_date": record.service_date if record else None,
                "service_method": record.service_method if record else None,
                "answer_filed": record.answer_filed if record else False,
                "tenant_display_name": record.tenant_display_name if record else "",
                "property_address": record.property_address if record else "",
                "notes": record.notes if record else "",
                "label": record.label if record else "EXAMPLE DATA",
                "rationale": (
                    ctx.rationales[decision.case_id].rationale
                    if decision.case_id in ctx.rationales
                    else None
                ),
                "packet_memo": ctx.packet_memos.get(decision.case_id),
            }
        )

    audit = ctx.audit.read_all()  # type: ignore[attr-defined]
    payload = {
        "generated_by": f"scripts/export_queue.py ({mode} sweep)",
        "mode": mode,
        "run_date": RUN_DATE.isoformat(),
        "attorney_capacity": CAPACITY,
        "label": "EXAMPLE DATA: every case record in this file is synthetic",
        "report": {k: v for k, v in asdict(report).items()},
        "succeeded": report.succeeded,
        "cases": cases,
        "audit": [{"seq": e["seq"], "kind": e["kind"], "case_id": e["case_id"]} for e in audit],
        "counts": {
            "total": len(cases),
            "interrupt": sum(1 for c in cases if c["level"] == "interrupt"),
            "surface_today": sum(1 for c in cases if c["level"] == "surface_today"),
            "monitor": sum(1 for c in cases if c["level"] == "monitor"),
            "hold": sum(1 for c in cases if c["level"] == "hold"),
            "flagged": sum(1 for c in cases if c["flags"]),
            "audit_events": len(audit),
        },
    }

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps(payload, indent=1) + "\n")
    counts = payload["counts"]
    print(
        f"wrote {TARGET.relative_to(ROOT)}: {counts['total']} cases, "
        f"{counts['interrupt']} interrupt, {counts['flagged']} flagged, "
        f"{counts['audit_events']} audit events, succeeded={report.succeeded}"
    )


if __name__ == "__main__":
    main()
