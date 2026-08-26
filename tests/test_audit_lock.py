"""Object Lock sink writes one Compliance-mode object per audit row."""

from datetime import date
from pathlib import Path

import boto3
import pytest

from agent.audit import AuditEvent, JsonlAuditSink, LockedAuditSink
from agent.run_context import RunContext
from agent.runner import run_deterministic
from agent.store import JsonFileCaseStore


def test_locked_sink_puts_compliance_object(tmp_path: Path) -> None:
    puts: list[dict[str, object]] = []

    def put_object(**kwargs: object) -> None:
        puts.append(kwargs)

    sink = LockedAuditSink(tmp_path / "audit.jsonl", bucket="lock-bucket", put_object=put_object)
    sink.append(AuditEvent(kind="run_started", case_id=None, payload={"n": 1}, run_id="run-lock"))
    assert len(puts) == 1
    row = puts[0]
    assert row["Bucket"] == "lock-bucket"
    assert row["ObjectLockMode"] == "COMPLIANCE"
    assert "ObjectLockRetainUntilDate" in row
    assert str(row["Key"]).startswith("audit/run-lock/")


def test_door_lock_record_is_compliance(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    door = Path(__file__).parent.parent / "infra" / "door"
    if str(door) not in sys.path:
        sys.path.insert(0, str(door))
    import lock as door_lock

    puts: list[dict[str, object]] = []

    class _S3:
        def put_object(self, **kwargs: object) -> None:
            puts.append(kwargs)

    monkeypatch.setattr(door_lock, "AUDIT_LOCK_BUCKET", "lock-bucket")
    monkeypatch.setattr(boto3, "client", lambda *_a, **_k: _S3())
    door_lock.lock_record("sweep_interrupted", "run-1", {"status": "awaiting_attorney"})
    assert len(puts) == 1
    assert puts[0]["ObjectLockMode"] == "COMPLIANCE"
    assert str(puts[0]["Key"]).startswith("door/run-1/")


def test_deterministic_run_records_custom_spans(tmp_path: Path) -> None:
    store = JsonFileCaseStore(
        intake_path=Path(__file__).parent.parent / "seed" / "synthetic_intake.json",
        escalations_path=tmp_path / "esc.jsonl",
    )
    ctx = RunContext(
        run_date=date(2026, 9, 9),
        attorney_capacity=2,
        store=store,
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
    )
    run_deterministic(ctx, attorney_response="approve")
    names = [s["name"] for s in ctx.span_log]
    assert "instanter.compute_deadline" in names
    assert "instanter.triage_queue" in names
