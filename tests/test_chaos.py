"""Chaos tests: the failure paths an unattended agent must survive LOUDLY.

Deterministic and credential-free (CI runs these). Each test injects a
concrete failure and asserts the two properties every fault path must have:
the run degrades instead of dying where degradation is safe, and nothing
fails silently, ever (the audit trail and the tool output both say what
happened). Live-model chaos (tool timeout under the real graph) runs in the
evals harness, not here.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from agent.audit import JsonlAuditSink
from agent.run_context import RunContext
from agent.runner import run_deterministic
from agent.store import EscalationRecord, IntakeRecord, JsonFileCaseStore
from agent.tools import build_tools

RUN_DATE = date(2026, 9, 9)
SEED = Path(__file__).parent.parent / "seed" / "synthetic_intake.json"


def good_record(case_id: str, service_date: str = "2026-09-01") -> IntakeRecord:
    return IntakeRecord(
        case_id=case_id,
        jurisdiction_id="GA-FULTON",
        service_date=service_date,
        service_method="personal",
    )


def make_ctx(tmp_path: Path, records: list[IntakeRecord], capacity: int = 2) -> RunContext:
    store = JsonFileCaseStore(
        intake_path=tmp_path / "unused.json",
        escalations_path=tmp_path / "escalations.jsonl",
    )
    ctx = RunContext(
        run_date=RUN_DATE,
        attorney_capacity=capacity,
        store=store,
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
    )
    for record in records:
        ctx.records[record.case_id] = record
    return ctx


def audit_kinds(ctx: RunContext) -> list[str]:
    return [e["kind"] for e in ctx.audit.read_all()]  # type: ignore[attr-defined]


# --- One malformed row must not kill the sweep --------------------------------


def test_malformed_record_becomes_refusal_not_crash(tmp_path: Path) -> None:
    ctx = make_ctx(
        tmp_path,
        [
            good_record("GOOD-1"),
            IntakeRecord(
                case_id="BAD-1",
                jurisdiction_id="GA-FULTON",
                service_date="09/01/2026",  # not ISO: parse refusal
                service_method="personal",
            ),
            good_record("GOOD-2", service_date="2026-09-02"),
        ],
    )
    tools = build_tools(ctx)
    payload = json.loads(tools["get_ranked_queue"]())

    ranked_ids = {row["case_id"] for row in payload["queue"]}
    assert ranked_ids == {"GOOD-1", "GOOD-2"}
    assert payload["refused_cases"] == [
        {"case_id": "BAD-1", "reason": payload["refused_cases"][0]["reason"]}
    ]
    assert "not an ISO date" in payload["refused_cases"][0]["reason"]

    kinds = audit_kinds(ctx)
    assert kinds.count("case_refused") == 1
    assert kinds.count("deadline_computed") == 2


def test_wrongly_typed_date_becomes_refusal_not_crash(tmp_path: Path) -> None:
    """A JSON number in a date field raises TypeError (not ValueError) from
    fromisoformat, and IntakeRecord has no runtime type validation, so this
    must be owned by the refusal path, not detonate mid-sweep."""
    ctx = make_ctx(
        tmp_path,
        [
            good_record("GOOD-1"),
            IntakeRecord(
                case_id="BAD-3",
                jurisdiction_id="GA-FULTON",
                service_date=20260901,  # type: ignore[arg-type]
                service_method="personal",
            ),
            IntakeRecord(
                case_id="BAD-4",
                jurisdiction_id="GA-FULTON",
                service_date="2026-09-01",
                service_method="personal",
                amended_affidavit="true",  # type: ignore[arg-type]
            ),
        ],
    )
    tools = build_tools(ctx)
    payload = json.loads(tools["get_ranked_queue"]())
    assert {row["case_id"] for row in payload["queue"]} == {"GOOD-1"}
    assert {r["case_id"] for r in payload["refused_cases"]} == {"BAD-3", "BAD-4"}
    assert audit_kinds(ctx).count("case_refused") == 2


def test_unknown_service_method_becomes_refusal_not_crash(tmp_path: Path) -> None:
    ctx = make_ctx(
        tmp_path,
        [
            good_record("GOOD-1"),
            IntakeRecord(
                case_id="BAD-2",
                jurisdiction_id="GA-FULTON",
                service_date="2026-09-01",
                service_method="taped_to_door",
            ),
        ],
    )
    tools = build_tools(ctx)
    payload = json.loads(tools["get_ranked_queue"]())
    assert {row["case_id"] for row in payload["queue"]} == {"GOOD-1"}
    assert payload["refused_cases"][0]["case_id"] == "BAD-2"
    assert "case_refused" in audit_kinds(ctx)


# --- A store failure mid-commit must be loud and exact ------------------------


class FailingAfterOneStore(JsonFileCaseStore):
    """Writes the first escalation, then dies: the partial-write case."""

    def __init__(self, intake_path: Path, escalations_path: Path) -> None:
        super().__init__(intake_path, escalations_path)
        self.writes = 0

    def record_escalation(self, escalation: EscalationRecord) -> None:
        self.writes += 1
        if self.writes > 1:
            raise OSError("disk full while appending escalation")
        super().record_escalation(escalation)


def test_store_failure_mid_commit_is_loud_and_exact(tmp_path: Path) -> None:
    store = FailingAfterOneStore(
        intake_path=tmp_path / "unused.json",
        escalations_path=tmp_path / "escalations.jsonl",
    )
    ctx = RunContext(
        run_date=RUN_DATE,
        attorney_capacity=2,
        store=store,
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
    )
    # Two overdue cases: both interrupt-now at capacity 2.
    ctx.records["A-1"] = good_record("A-1", service_date="2026-08-30")
    ctx.records["B-2"] = good_record("B-2", service_date="2026-08-31")

    tools = build_tools(ctx)
    tools["get_ranked_queue"]()
    for decision in ctx.interrupt_candidates:
        tools["submit_escalation_rationale"](
            case_id=decision.case_id,
            disposition=decision.level.value,
            contributing_factors=list(decision.factors)[:8],
            rationale="Deadline has passed with no answer on file.",
            confidence=0.9,
        )

    result = tools["commit_escalations"]()

    assert "STORE WRITE FAILED" in result
    assert "NOT committed" in result
    # Exactly the one successful write is reflected, no more, no less.
    assert len(ctx.committed_case_ids) == 1
    stored = store.list_escalations(run_id=ctx.run_id)
    assert [e.case_id for e in stored] == list(ctx.committed_case_ids)

    events = ctx.audit.read_all()  # type: ignore[attr-defined]
    failures = [e for e in events if e["kind"] == "store_write_failed"]
    assert len(failures) == 1
    payload = failures[0]["payload"]
    assert payload["written"] == list(ctx.committed_case_ids)
    assert len(payload["not_written"]) == 1
    # The failed run never claims a completed commit.
    assert "escalation_committed" not in audit_kinds(ctx)


def test_deterministic_run_surfaces_store_failure(tmp_path: Path) -> None:
    store = FailingAfterOneStore(
        intake_path=SEED,
        escalations_path=tmp_path / "escalations.jsonl",
    )
    ctx = RunContext(
        run_date=RUN_DATE,
        attorney_capacity=2,
        store=store,
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
    )
    report = run_deterministic(ctx)
    # 3 L1 candidates gated to 2; only 1 write succeeded before the fault.
    assert len(report.committed) == 1
    assert "store_write_failed" in audit_kinds(ctx)
    assert "escalation_committed" not in audit_kinds(ctx)
    # The attorney approved 2; 1 landed. The run must report the missing
    # case as a failure and refuse to call itself a success.
    assert len(report.failures) == 1
    assert report.failures[0] not in report.committed
    assert not report.succeeded


def test_commit_retry_after_partial_failure_never_duplicates(tmp_path: Path) -> None:
    """A retry after a partial store failure writes only the missing cases;
    the already-durable escalation is never appended twice."""
    store = FailingAfterOneStore(
        intake_path=tmp_path / "unused.json",
        escalations_path=tmp_path / "escalations.jsonl",
    )
    ctx = RunContext(
        run_date=RUN_DATE,
        attorney_capacity=2,
        store=store,
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
    )
    ctx.records["A-1"] = good_record("A-1", service_date="2026-08-30")
    ctx.records["B-2"] = good_record("B-2", service_date="2026-08-31")
    tools = build_tools(ctx)
    tools["get_ranked_queue"]()
    for decision in ctx.interrupt_candidates:
        tools["submit_escalation_rationale"](
            case_id=decision.case_id,
            disposition=decision.level.value,
            contributing_factors=list(decision.factors)[:8],
            rationale="Deadline has passed with no answer on file.",
            confidence=0.9,
        )

    first = tools["commit_escalations"]()
    assert "STORE WRITE FAILED" in first
    assert len(ctx.committed_case_ids) == 1

    # The fault clears (FailingAfterOneStore only fails its second write;
    # reset the counter to simulate recovery), and the model retries.
    store.writes = 0
    second = tools["commit_escalations"]()
    assert "Committed 1 escalation(s)" in second
    stored = store.list_escalations(run_id=ctx.run_id)
    assert sorted(e.case_id for e in stored) == sorted(ctx.committed_case_ids)
    assert len(stored) == 2  # exactly one row per case, no duplicates

    third = tools["commit_escalations"]()
    assert "ALREADY COMMITTED" in third
    assert len(store.list_escalations(run_id=ctx.run_id)) == 2


def seed_ctx(tmp_path: Path, capacity: int = 2) -> RunContext:
    store = JsonFileCaseStore(
        intake_path=SEED,
        escalations_path=tmp_path / "escalations.jsonl",
    )
    return RunContext(
        run_date=RUN_DATE,
        attorney_capacity=capacity,
        store=store,
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
    )


def test_run_context_is_single_use(tmp_path: Path) -> None:
    ctx = seed_ctx(tmp_path)
    run_deterministic(ctx)
    with pytest.raises(RuntimeError, match="single-use"):
        run_deterministic(ctx)


# --- The deterministic floor: undertriage must be impossible ------------------


def test_floor_completes_sweep_when_model_layer_did_nothing(tmp_path: Path) -> None:
    """The live-chaos finding, CI-shaped: the graph can end with no ranked
    queue and no attorney decision (a node died, or the writer declined to
    act). The floor must then rank, escalate with template rationales, and
    execute the attorney decision on its own."""
    from agent.runner import _deterministic_floor

    ctx = make_ctx(
        tmp_path,
        [good_record("A-1", service_date="2026-08-30"), good_record("B-2")],
        capacity=1,
    )
    tools = build_tools(ctx)
    assert ctx.decisions == [] and ctx.attorney_action == ""

    _deterministic_floor(ctx, tools, "approve")

    assert ctx.attorney_action == "approved"
    assert len(ctx.committed_case_ids) == 1
    kinds = audit_kinds(ctx)
    assert "queue_ranked" in kinds
    assert "rationale_recorded" in kinds
    assert "escalation_committed" in kinds


def test_floor_keeps_model_rationales_and_templates_only_missing(tmp_path: Path) -> None:
    from agent.runner import _deterministic_floor

    ctx = make_ctx(
        tmp_path,
        [good_record("A-1", service_date="2026-08-30"), good_record("B-2", "2026-08-31")],
        capacity=2,
    )
    tools = build_tools(ctx)
    tools["get_ranked_queue"]()
    first = ctx.interrupt_candidates[0]
    tools["submit_escalation_rationale"](
        case_id=first.case_id,
        disposition=first.level.value,
        contributing_factors=list(first.factors)[:8],
        rationale="Deadline has passed with no answer on file.",
        confidence=0.9,
    )

    _deterministic_floor(ctx, tools, "approve")

    assert "[MODEL DISABLED" not in ctx.rationales[first.case_id].rationale
    second = ctx.interrupt_candidates[1]
    assert "[MODEL DISABLED" in ctx.rationales[second.case_id].rationale
    assert len(ctx.committed_case_ids) == 2


def test_floor_makes_no_attorney_decision_without_candidates(tmp_path: Path) -> None:
    from agent.runner import _deterministic_floor

    # Served today: deadline is a week out, nothing interrupt-now.
    ctx = make_ctx(tmp_path, [good_record("A-1", service_date="2026-09-09")])
    tools = build_tools(ctx)
    _deterministic_floor(ctx, tools, "approve")
    assert ctx.attorney_action == ""
    assert ctx.committed_case_ids == ()
    assert "queue_ranked" in audit_kinds(ctx)


# --- Attorney response parsing must be strict and fail closed -----------------


def test_attorney_response_parsing_is_strict_and_fail_closed() -> None:
    from agent.hooks import parse_attorney_response

    approvals = ["approve", "Approve", "APPROVED!!", "approve all", " approved. "]
    for probe in approvals:
        action, _ = parse_attorney_response(probe)
        assert action == "approved", probe

    deferrals = [
        "",  # empty defers
        "approving is denied",  # prefix-match trap
        "Approve only 26ED00101, defer the rest",  # conditional never flattens
        "approve please",  # qualifier defers
        "defer: in hearings",
        "aprove",  # typo defers
        "yes",  # ambiguity defers
    ]
    for probe in deferrals:
        action, _ = parse_attorney_response(probe)
        assert action == "deferred", probe


def test_refusals_are_audited(tmp_path: Path) -> None:
    """Every safety-boundary refusal leaves an audit event: disposition
    mismatch, memo advice rejection, and commit refusals."""
    ctx = make_ctx(tmp_path, [good_record("A-1", service_date="2026-08-30")], capacity=1)
    tools = build_tools(ctx)
    tools["get_ranked_queue"]()
    interrupt_id = ctx.interrupt_candidates[0].case_id

    tools["commit_escalations"]()  # missing rationale -> refused
    tools["submit_escalation_rationale"](
        case_id=interrupt_id,
        disposition="monitor",  # ladder said interrupt -> mismatch
        contributing_factors=["overdue"],
        rationale="Deadline already passed.",
        confidence=0.9,
    )
    tools["submit_escalation_rationale"](
        case_id=interrupt_id,
        disposition="interrupt",
        contributing_factors=["overdue"],
        rationale="Deadline already passed; no answer on file.",
        confidence=0.9,
    )
    ctx.attorney_action = "approved"
    tools["commit_escalations"]()
    tools["commit_escalations"]()  # second call -> already committed
    tools["write_packet_memo"](
        case_id=interrupt_id, memo="The tenant should raise the defense of tender."
    )

    kinds = audit_kinds(ctx)
    assert kinds.count("commit_refused") == 2
    assert "rationale_rejected" in kinds
    assert "memo_rejected" in kinds
    events = ctx.audit.read_all()  # type: ignore[attr-defined]
    reasons = {e["payload"].get("reason") for e in events if e["kind"] == "commit_refused"}
    assert reasons == {"missing_rationales", "already_committed"}


# --- A raising graph must not skip the floor ----------------------------------


def test_graph_exception_still_runs_floor_and_reports_model_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strands re-raises node failures and node timeouts (fail-fast). The
    runner's failure boundary must audit the death and still complete the
    deterministic sweep, then refuse to call the run a success."""
    import agent.graph as graph_module
    from agent.runner import run_live

    class ExplodingGraph:
        def __call__(self, _prompt: object) -> object:
            raise RuntimeError("simulated Bedrock outage")

    monkeypatch.setattr(
        graph_module, "build_triage_graph", lambda ctx, plugins=None: ExplodingGraph()
    )

    store = JsonFileCaseStore(
        intake_path=SEED,
        escalations_path=tmp_path / "escalations.jsonl",
    )
    ctx = RunContext(
        run_date=RUN_DATE,
        attorney_capacity=2,
        store=store,
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
    )
    report = run_live(ctx, attorney_response="approve")

    assert report.backstop_used
    assert "simulated Bedrock outage" in report.model_error
    assert len(report.committed) == 2  # the sweep still delivered
    assert not report.succeeded  # but the run is not allowed to read green
    kinds = audit_kinds(ctx)
    assert "model_error" in kinds
    assert "deterministic_backstop" in kinds
    assert "escalation_committed" in kinds


# --- Corrupt intake must fail loudly before anything runs ---------------------


def test_audit_seq_continues_across_sink_instances(tmp_path: Path) -> None:
    from agent.audit import AuditEvent

    path = tmp_path / "audit.jsonl"
    first = JsonlAuditSink(path)
    first.append(AuditEvent(kind="run_started", case_id=None, payload={}, run_id="r1"))
    first.append(AuditEvent(kind="run_finished", case_id=None, payload={}, run_id="r1"))
    second = JsonlAuditSink(path)
    second.append(AuditEvent(kind="run_started", case_id=None, payload={}, run_id="r2"))
    seqs = [e["seq"] for e in second.read_all()]
    assert seqs == [1, 2, 3]  # no reuse across runs appended to one file


def test_corrupt_audit_line_is_loud_and_names_the_line(tmp_path: Path) -> None:
    from agent.audit import AuditEvent

    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path)
    sink.append(AuditEvent(kind="run_started", case_id=None, payload={}, run_id="r1"))
    with path.open("a") as handle:
        handle.write('{"seq": 2, "torn')  # simulated torn tail
    with pytest.raises(ValueError, match="line 2"):
        JsonlAuditSink(path).read_all()


def test_corrupt_escalation_line_is_loud_and_names_the_line(tmp_path: Path) -> None:
    store = JsonFileCaseStore(
        intake_path=tmp_path / "unused.json",
        escalations_path=tmp_path / "escalations.jsonl",
    )
    store.record_escalation(
        EscalationRecord(
            case_id="A-1",
            disposition="interrupt",
            rank=1,
            factors=("overdue",),
            rationale="Deadline passed.",
            confidence=1.0,
            run_id="r1",
        )
    )
    with (tmp_path / "escalations.jsonl").open("a") as handle:
        handle.write("{torn")
    with pytest.raises(ValueError, match="line 2"):
        store.list_escalations()


def test_corrupt_intake_file_raises_loudly(tmp_path: Path) -> None:
    intake = tmp_path / "intake.json"
    intake.write_text('{"records": [{"case_id": "X", truncated')
    store = JsonFileCaseStore(
        intake_path=intake,
        escalations_path=tmp_path / "escalations.jsonl",
    )
    ctx = RunContext(
        run_date=RUN_DATE,
        attorney_capacity=2,
        store=store,
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
    )
    with pytest.raises(json.JSONDecodeError):
        run_deterministic(ctx)
    # Nothing pretended to start: no run_started event, no escalations.
    assert audit_kinds(ctx) == []
    assert store.list_escalations() == []
