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
from agent.hooks import bind_approval
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
    ctx.attorney_action = "approved"
    bind_approval(ctx, tuple(d.case_id for d in ctx.interrupt_candidates))

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
    ctx.attorney_action = "approved"
    bind_approval(ctx, tuple(d.case_id for d in ctx.interrupt_candidates))

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

    tools["commit_escalations"]()  # no approval recorded -> refused
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
    bind_approval(ctx, tuple(d.case_id for d in ctx.interrupt_candidates))
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
    assert reasons == {"not_approved", "already_committed"}


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
    # Nobody was presented anything, so no approval is claimed: the floor
    # commits as PENDING attorney review, never as approved.
    assert report.attorney_action == "pending"
    assert not report.succeeded  # and the run is not allowed to read green
    kinds = audit_kinds(ctx)
    assert "model_error" in kinds
    assert "deterministic_backstop" in kinds
    assert "pending_review_commit" in kinds
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


def test_refused_case_fails_the_run(tmp_path: Path) -> None:
    """A refused (unparseable) case is a case the sweep could not protect:
    it must reach the report and fail the run, never exit green."""
    payload = {
        "records": [
            {
                "case_id": "GOOD-1",
                "jurisdiction_id": "GA-FULTON",
                "service_date": "2026-08-30",
                "service_method": "personal",
            },
            {
                "case_id": "BAD-1",
                "jurisdiction_id": "GA-FULTON",
                "service_date": 20260901,
                "service_method": "personal",
            },
        ]
    }
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps(payload))
    store = JsonFileCaseStore(intake_path=intake, escalations_path=tmp_path / "e.jsonl")
    ctx = RunContext(
        run_date=RUN_DATE,
        attorney_capacity=2,
        store=store,
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
    )
    report = run_deterministic(ctx)
    assert report.refused == ("BAD-1",)
    assert not report.succeeded
    assert report.committed == ("GOOD-1",)  # the good case was still protected


class AmbiguousFailureStore(JsonFileCaseStore):
    """Appends the row durably, THEN raises: the ambiguous-failure shape
    (e.g. fsync error after the write became visible)."""

    def __init__(self, intake_path: Path, escalations_path: Path) -> None:
        super().__init__(intake_path, escalations_path)
        self.fail_next = 0

    def record_escalation(self, escalation: EscalationRecord) -> None:
        super().record_escalation(escalation)  # the row IS durable
        if self.fail_next > 0:
            self.fail_next -= 1
            raise OSError("fsync failed after the row became visible")


def test_ambiguous_store_failure_retry_never_duplicates(tmp_path: Path) -> None:
    store = AmbiguousFailureStore(
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

    ctx.attorney_action = "approved"
    bind_approval(ctx, tuple(d.case_id for d in ctx.interrupt_candidates))
    store.fail_next = 1  # first write lands durably but reports failure
    first = tools["commit_escalations"]()
    assert "STORE WRITE FAILED" in first

    # Retry: reconciliation against durable state must find the landed row
    # and write ONLY the genuinely missing case.
    second = tools["commit_escalations"]()
    assert "Committed 1 escalation(s)" in second
    stored = store.list_escalations(run_id=ctx.run_id)
    assert len(stored) == 2  # exactly one row per case despite the lie
    assert sorted(e.case_id for e in stored) == ["A-1", "B-2"]
    assert len(ctx.committed_case_ids) == 2


def test_post_approval_failure_recovers_through_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approval is a decision, not a completed commit: a graph that dies
    AFTER the hook approved but before the writes finished must still end
    with every approved case durably committed."""
    import agent.graph as graph_module
    from agent.runner import run_live

    def exploding_after_approval(ctx: RunContext, plugins: object = None) -> object:
        class G:
            def __call__(self, _prompt: object) -> object:
                # The realistic death sequence: the writer ranked the queue,
                # wrote rationales, the hook recorded a SNAPSHOT-BOUND
                # approval... and then the executor died before the commit.
                from agent.runner import _template_rationale

                tools = build_tools(ctx)
                tools["get_ranked_queue"]()
                for d in ctx.interrupt_candidates:
                    ctx.rationales[d.case_id] = _template_rationale(d)
                ctx.attorney_action = "approved"
                bind_approval(ctx, tuple(d.case_id for d in ctx.interrupt_candidates))
                raise RuntimeError("executor died before the commit")

        return G()

    monkeypatch.setattr(graph_module, "build_triage_graph", exploding_after_approval)

    ctx = seed_ctx(tmp_path)
    report = run_live(ctx, attorney_response="approve")
    assert report.backstop_used
    assert len(report.committed) == 2  # recovery delivered the approved cases
    assert report.failures == ()  # parity restored by the recovery
    assert not report.succeeded  # the model death still fails the run
    stored = ctx.store.list_escalations(run_id=ctx.run_id)
    assert sorted(e.case_id for e in stored) == sorted(report.committed)
    # The attorney packet is complete: recovered cases carry template memos.
    assert report.missing_memos == ()
    assert all(cid in ctx.packet_memos for cid in report.committed)
    assert all("[MODEL DISABLED" in ctx.packet_memos[cid] for cid in report.committed)


def test_concurrent_same_run_writers_never_duplicate_an_escalation(tmp_path: Path) -> None:
    """The round-3 reproducer: two retry workers for the same run both
    observe a missing case and both write. Insert-if-absent under the
    store's lock must leave exactly one row."""
    import threading

    store = JsonFileCaseStore(
        intake_path=tmp_path / "unused.json",
        escalations_path=tmp_path / "escalations.jsonl",
    )
    record = EscalationRecord(
        case_id="A-1",
        disposition="interrupt",
        rank=1,
        factors=("overdue",),
        rationale="Deadline passed.",
        confidence=1.0,
        run_id="r1",
    )
    barrier = threading.Barrier(2)

    def write() -> None:
        barrier.wait()
        store.record_escalation(record)

    threads = [threading.Thread(target=write) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.list_escalations(run_id="r1")) == 1


def test_unbound_approval_is_a_run_failure_and_recovery_stays_inside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An approval bound to no candidate snapshot must never let recovery
    commit a freshly ranked queue the attorney never saw; it fails the run."""
    import agent.graph as graph_module
    from agent.runner import run_live

    def approves_nothing(ctx: RunContext, plugins: object = None) -> object:
        class G:
            def __call__(self, _prompt: object) -> object:
                ctx.attorney_action = "approved"  # approval with NO snapshot
                raise RuntimeError("died with an unbound approval")

        return G()

    monkeypatch.setattr(graph_module, "build_triage_graph", approves_nothing)
    ctx = seed_ctx(tmp_path)
    report = run_live(ctx, attorney_response="approve")
    assert report.committed == ()  # nothing rides an unbound approval
    assert "approval-not-bound-to-candidates" in report.failures
    assert not report.succeeded


def test_commit_never_exceeds_the_approval_snapshot(tmp_path: Path) -> None:
    """Cases minted after the approval need a fresh interrupt; they can
    never ride an existing approval into the store."""
    ctx = make_ctx(
        tmp_path,
        [good_record("A-1", service_date="2026-08-30"), good_record("B-2", "2026-08-31")],
        capacity=2,
    )
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
    # The attorney approved ONLY A-1 (snapshot from an earlier queue state).
    ctx.attorney_action = "approved"
    bind_approval(ctx, ("A-1",))
    result = tools["commit_escalations"]()
    assert "Committed 1 escalation(s): A-1" in result
    stored = ctx.store.list_escalations(run_id=ctx.run_id)
    assert [e.case_id for e in stored] == ["A-1"]  # B-2 never rode the approval
    events = ctx.audit.read_all()  # type: ignore[attr-defined]
    refusals = [e for e in events if e["kind"] == "commit_refused"]
    assert any(e["payload"]["reason"] == "requires_new_approval" for e in refusals)


def test_stale_durable_content_is_a_conflict_not_a_success(tmp_path: Path) -> None:
    """Round-5 reproducer: a durable row with this run's key but DIFFERENT
    content (stale rank, old rationale) must never be silently counted as
    this run's commit; it is an audited conflict that fails the run."""
    from agent.runner import _report

    ctx = make_ctx(tmp_path, [good_record("A-1", service_date="2026-08-30")], capacity=1)
    tools = build_tools(ctx)
    tools["get_ranked_queue"]()
    decision = ctx.interrupt_candidates[0]
    tools["submit_escalation_rationale"](
        case_id=decision.case_id,
        disposition=decision.level.value,
        contributing_factors=list(decision.factors)[:8],
        rationale="Deadline has passed with no answer on file.",
        confidence=0.9,
    )
    # A stale writer already landed a same-key row with different content.
    ctx.store.record_escalation(
        EscalationRecord(
            case_id="A-1",
            disposition="interrupt",
            rank=99,
            factors=("stale",),
            rationale="An old rationale from a divergent writer.",
            confidence=0.5,
            run_id=ctx.run_id,
        )
    )
    ctx.attorney_action = "approved"
    bind_approval(ctx, ("A-1",))
    result = tools["commit_escalations"]()

    assert "STORE CONFLICT" in result
    assert ctx.committed_case_ids == ()  # the stale row is NOT this run's commit
    assert "store_conflict" in audit_kinds(ctx)
    report = _report(ctx, mode="test")
    assert "A-1" in report.failures
    assert not report.succeeded


def test_candidate_outside_the_approval_fails_the_run(tmp_path: Path) -> None:
    """Round-5 reproducer: a case minted after the approval cannot ride it,
    AND it cannot vanish behind a green run; it stays a failure until a
    fresh approval resolves it."""
    from agent.runner import _report

    ctx = make_ctx(
        tmp_path,
        [good_record("A-1", service_date="2026-08-30"), good_record("B-2", "2026-08-31")],
        capacity=2,
    )
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
    ctx.attorney_action = "approved"
    bind_approval(ctx, ("A-1",))  # the attorney only ever saw A-1
    tools["commit_escalations"]()

    assert ctx.committed_case_ids == ("A-1",)
    report = _report(ctx, mode="test")
    assert "B-2" in report.failures  # the un-approved urgent case is not lost
    assert not report.succeeded


def test_second_interrupt_gets_deferred_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supplied response is ONE human decision: a second interrupt in
    the same run must receive a fail-closed deferral, never a replay."""
    from strands.multiagent import Status

    import agent.graph as graph_module
    from agent.runner import run_live

    answers: list[str] = []

    class FakeInterrupt:
        id = "int-1"

    class FakeResult:
        def __init__(self, status: object, interrupts: list[object]) -> None:
            self.status = status
            self.interrupts = interrupts

    class TwoInterruptGraph:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, payload: object) -> object:
            self.calls += 1
            if isinstance(payload, list):
                for item in payload:
                    answers.append(item["interruptResponse"]["response"])
            if self.calls <= 2:
                return FakeResult(Status.INTERRUPTED, [FakeInterrupt()])
            return FakeResult(Status.COMPLETED, [])

    monkeypatch.setattr(
        graph_module, "build_triage_graph", lambda ctx, plugins=None: TwoInterruptGraph()
    )
    ctx = seed_ctx(tmp_path)
    run_live(ctx, attorney_response="approve")

    assert answers[0] == "approve"
    assert answers[1].startswith("defer: the single-use attorney response")


def test_overlapping_audit_sinks_never_share_a_sequence(tmp_path: Path) -> None:
    from agent.audit import AuditEvent

    path = tmp_path / "audit.jsonl"
    a = JsonlAuditSink(path)
    b = JsonlAuditSink(path)
    for i in range(3):  # interleaved appends from two live instances
        a.append(AuditEvent(kind="tick", case_id=None, payload={"i": i}, run_id="rA"))
        b.append(AuditEvent(kind="tick", case_id=None, payload={"i": i}, run_id="rB"))
    seqs = [e["seq"] for e in a.read_all()]
    assert seqs == [1, 2, 3, 4, 5, 6]


def test_duplicate_sequence_is_rejected_on_read(tmp_path: Path) -> None:
    from agent.audit import AuditEvent

    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path)
    sink.append(AuditEvent(kind="tick", case_id=None, payload={}, run_id="r1"))
    line = path.read_text().splitlines()[0]
    with path.open("a") as handle:  # forge a duplicate seq
        handle.write(line + "\n")
    with pytest.raises(ValueError, match="broken sequence"):
        JsonlAuditSink(path).read_all()


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
