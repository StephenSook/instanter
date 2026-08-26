"""Custom spans must record on the receipt and must not swallow interrupts."""

from datetime import date
from pathlib import Path

import pytest

from agent.audit import JsonlAuditSink
from agent.run_context import RunContext
from agent.spans import instanter_span
from agent.store import JsonFileCaseStore


def _ctx(tmp_path: Path) -> RunContext:
    store = JsonFileCaseStore(
        intake_path=Path(__file__).parent.parent / "seed" / "synthetic_intake.json",
        escalations_path=tmp_path / "esc.jsonl",
    )
    return RunContext(
        run_date=date(2026, 9, 9),
        attorney_capacity=2,
        store=store,
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
    )


def test_span_records_even_when_the_body_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with (
        pytest.raises(RuntimeError, match="interrupt"),
        instanter_span(ctx, "instanter.attorney_interrupt", waiting=2),
    ):
        raise RuntimeError("interrupt")
    assert [row["name"] for row in ctx.span_log] == ["instanter.attorney_interrupt"]
    assert isinstance(ctx.audit, JsonlAuditSink)
    rows = ctx.audit.read_all()
    assert any(
        row["kind"] == "span" and row["payload"]["name"] == "instanter.attorney_interrupt"
        for row in rows
    )


def test_span_records_a_successful_body(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with instanter_span(ctx, "instanter.compute_deadline", case_id="26ED00101"):
        pass
    assert ctx.span_log[0]["name"] == "instanter.compute_deadline"
    assert ctx.span_log[0]["case_id"] == "26ED00101"
