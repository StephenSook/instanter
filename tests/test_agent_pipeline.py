"""Pipeline tests: store parsing, tool contracts, and the model-free
end-to-end run over the real synthetic seed."""

import json
from datetime import date
from pathlib import Path

import pytest

from agent.audit import JsonlAuditSink
from agent.hooks import bind_approval
from agent.run_context import RunContext
from agent.runner import run_deterministic
from agent.store import IntakeParseError, IntakeRecord, JsonFileCaseStore, to_case_input
from agent.tools import build_tools
from engine.rules import ServiceMethod

RUN_DATE = date(2026, 9, 9)
SEED = Path(__file__).parent.parent / "seed" / "synthetic_intake.json"


def make_record(case_id: str = "26ED00901", **overrides: object) -> IntakeRecord:
    base: dict[str, object] = {
        "case_id": case_id,
        "jurisdiction_id": "GA-FULTON",
        "service_date": "2026-09-02",
        "service_method": "personal",
    }
    base.update(overrides)
    return IntakeRecord(**base)  # type: ignore[arg-type]


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


# --- Store parsing boundary ---------------------------------------------------


def test_to_case_input_round_trip() -> None:
    case = to_case_input(make_record(service_method="tack_and_mail"))
    assert case.service_method is ServiceMethod.TACK_AND_MAIL
    assert case.service_date == date(2026, 9, 2)


def test_unknown_service_method_refuses_at_the_boundary() -> None:
    with pytest.raises(IntakeParseError, match="service_method"):
        to_case_input(make_record(service_method="taped_to_door"))


def test_malformed_date_refuses_at_the_boundary() -> None:
    with pytest.raises(IntakeParseError, match="ISO date"):
        to_case_input(make_record(service_date="09/02/2026"))


def test_duplicate_case_ids_refuse(tmp_path: Path) -> None:
    payload = {
        "records": [
            {
                "case_id": "X",
                "jurisdiction_id": "GA-FULTON",
                "service_date": None,
                "service_method": "unknown",
            },
            {
                "case_id": "X",
                "jurisdiction_id": "GA-FULTON",
                "service_date": None,
                "service_method": "unknown",
            },
        ]
    }
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps(payload))
    store = JsonFileCaseStore(intake_path=intake, escalations_path=tmp_path / "e.jsonl")
    result = store.load_intake()
    # Identity is ambiguous for every row carrying the id: none is swept,
    # the refusal fails the run, and the rest of an intake still processes.
    assert result.records == ()
    assert [m.row_key for m in result.malformed] == ["X"]
    assert "more than once" in result.malformed[0].reason


# --- Tool contracts -----------------------------------------------------------


def test_ranked_queue_populates_decisions_and_audits(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, [make_record(), make_record("26ED00902", service_date="2026-09-01")])
    tools = build_tools(ctx)
    payload = json.loads(tools["get_ranked_queue"]())
    assert payload["attorney_capacity"] == 2
    assert len(payload["queue"]) == 2
    assert len(ctx.decisions) == 2
    kinds = [e["kind"] for e in ctx.audit.read_all()]  # type: ignore[attr-defined]
    assert kinds.count("deadline_computed") == 2
    assert "queue_ranked" in kinds


def test_observation_submission_validates_and_rejects_advice(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, [make_record(notes="papers taped to door")])
    tools = build_tools(ctx)
    good = tools["submit_case_observations"](
        case_id="26ED00901",
        summary="Notes report papers taped to the door.",
        mentions_service_by_posting=True,
        needs_human_confirmation=True,
        confidence=0.8,
    )
    assert "recorded" in good
    bad = tools["submit_case_observations"](
        case_id="26ED00901",
        summary="You should file an answer immediately.",
        needs_human_confirmation=False,
        confidence=0.9,
    )
    assert "VALIDATION FAILED" in bad
    assert "advice language" in bad


def test_rationale_requires_interrupt_case_and_exact_echo(tmp_path: Path) -> None:
    ctx = make_ctx(
        tmp_path,
        [
            make_record(service_date="2026-09-01"),
            make_record("26ED00902", service_date="2026-09-07"),
        ],
        capacity=1,
    )
    tools = build_tools(ctx)
    tools["get_ranked_queue"]()
    interrupt_id = ctx.interrupt_candidates[0].case_id
    monitor_case = "26ED00902" if interrupt_id != "26ED00902" else "26ED00901"

    refused = tools["submit_escalation_rationale"](
        case_id=monitor_case,
        disposition="monitor",
        explanation="Not urgent this run.",
        confidence=0.9,
    )
    assert "NOT AN INTERRUPT CASE" in refused

    mismatched = tools["submit_escalation_rationale"](
        case_id=interrupt_id,
        disposition="monitor",
        explanation="Deadline already passed; default writ exposure is live.",
        confidence=0.9,
    )
    assert "DISPOSITION MISMATCH" in mismatched


def test_commit_requires_every_rationale(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, [make_record(service_date="2026-09-01")], capacity=1)
    tools = build_tools(ctx)
    tools["get_ranked_queue"]()
    assert "NOT APPROVED" in tools["commit_escalations"]()
    ctx.attorney_action = "approved"
    bind_approval(ctx, tuple(d.case_id for d in ctx.interrupt_candidates))
    result = tools["commit_escalations"]()
    assert "MISSING RATIONALES" in result


def test_packet_memo_only_for_committed_and_no_advice(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, [make_record(service_date="2026-09-01")], capacity=1)
    tools = build_tools(ctx)
    tools["get_ranked_queue"]()
    interrupt_id = ctx.interrupt_candidates[0].case_id
    tools["submit_escalation_rationale"](
        case_id=interrupt_id,
        disposition="interrupt",
        explanation="Deadline passed with no answer on file; writ exposure is live.",
        confidence=0.9,
    )
    assert "NOT COMMITTED" in tools["write_packet_memo"](case_id=interrupt_id, notes="")
    ctx.attorney_action = "approved"
    bind_approval(ctx, tuple(d.case_id for d in ctx.interrupt_candidates))
    tools["commit_escalations"]()
    poisoned = tools["write_packet_memo"](
        case_id=interrupt_id, notes="The tenant should raise the defense of tender."
    )
    assert "VALIDATION FAILED" in poisoned
    assert interrupt_id not in ctx.packet_memos
    # Round-10: notes are structurally incapable of carrying a date, day
    # count, or rank; a contradictory figure beside the fact sheet defeats
    # the deterministic-facts guarantee.
    numeric = tools["write_packet_memo"](
        case_id=interrupt_id,
        notes="Confirm the 30-day deadline on 2027-01-01 and rank 99.",
    )
    assert "VALIDATION FAILED" in numeric
    assert interrupt_id not in ctx.packet_memos
    assert "recorded" in tools["write_packet_memo"](
        case_id=interrupt_id, notes="Confirm the mailing date with the tenant."
    )
    # Round-9: the fact sheet is deterministic; every number and date comes
    # from engine state, and the drafter's contribution is labeled notes.
    memo = ctx.packet_memos[interrupt_id]
    deadline = ctx.deadlines[interrupt_id]
    assert f"Effective deadline {deadline.effective_deadline}." in memo
    assert "Reviewer notes: Confirm the mailing date with the tenant." in memo
    duplicate = tools["write_packet_memo"](case_id=interrupt_id, notes="A second memo.")
    assert "ALREADY RECORDED" in duplicate


# --- Model-free end-to-end over the real seed --------------------------------


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


def test_deterministic_run_over_real_seed_commits_to_capacity(tmp_path: Path) -> None:
    ctx = seed_ctx(tmp_path)
    report = run_deterministic(ctx)
    assert report.total_cases == 48
    assert len(report.committed) == 2  # capacity gates three L1 candidates to two
    assert report.attorney_action == "approved"
    escalations = ctx.store.list_escalations(run_id=ctx.run_id)
    assert [e.rank for e in escalations] == [1, 2]
    kinds = {e["kind"] for e in ctx.audit.read_all()}  # type: ignore[attr-defined]
    assert {
        "run_started",
        "deadline_computed",
        "queue_ranked",
        "rationale_recorded",
        "escalation_committed",
    } <= kinds


def test_deterministic_run_deferred_commits_nothing(tmp_path: Path) -> None:
    ctx = seed_ctx(tmp_path)
    report = run_deterministic(ctx, attorney_response="defer: capacity emergency")
    assert report.committed == ()
    assert report.attorney_action == "deferred"
    assert ctx.store.list_escalations(run_id=ctx.run_id) == []


# --- Round-11 reproducers -----------------------------------------------------


def test_rationale_numbers_come_from_engine_state(tmp_path: Path) -> None:
    """Round-11 reproducer: a writer probe fabricated 'deadline 2099-12-31,
    999 days remaining, rank 9' and it persisted. Numeric rationale facts
    are now deterministic; the model's explanation must carry no digits."""
    ctx = make_ctx(tmp_path, [make_record(service_date="2026-09-01")], capacity=1)
    tools = build_tools(ctx)
    tools["get_ranked_queue"]()
    interrupt_id = ctx.interrupt_candidates[0].case_id

    fabricated = tools["submit_escalation_rationale"](
        case_id=interrupt_id,
        disposition="interrupt",
        explanation="The effective deadline is 2099-12-31, with 999 days remaining; queue rank 9.",
        confidence=0.9,
    )
    assert "VALIDATION FAILED" in fabricated
    assert "no digits" in fabricated
    assert interrupt_id not in ctx.rationales

    accepted = tools["submit_escalation_rationale"](
        case_id=interrupt_id,
        disposition="interrupt",
        explanation="Deadline has passed with no answer on file; writ exposure is live.",
        confidence=0.9,
    )
    assert "recorded" in accepted
    stored = ctx.rationales[interrupt_id].rationale
    deadline = ctx.deadlines[interrupt_id]
    assert f"Effective deadline {deadline.effective_deadline}." in stored
    assert "Writer explanation:" in stored
    assert "2099" not in stored


def test_recorded_ambiguities_reach_the_packet_memo(tmp_path: Path) -> None:
    """Round-11 reproducer: recorded intake ambiguities never reached the
    committed memos while the run scored 17/17. They are packet facts."""
    ctx = make_ctx(
        tmp_path, [make_record(service_date="2026-09-01", notes="conflicting dates")], capacity=1
    )
    tools = build_tools(ctx)
    tools["submit_case_observations"](
        case_id="26ED00901",
        summary="Notes mention two different hearing dates.",
        needs_human_confirmation=True,
        confidence=0.8,
        ambiguities=["Which hearing date is correct?"],
    )
    tools["get_ranked_queue"]()
    interrupt_id = ctx.interrupt_candidates[0].case_id
    tools["submit_escalation_rationale"](
        case_id=interrupt_id,
        disposition="interrupt",
        explanation="Deadline has passed with no answer on file.",
        confidence=0.9,
    )
    ctx.attorney_action = "approved"
    bind_approval(ctx, (interrupt_id,))
    tools["commit_escalations"]()
    tools["write_packet_memo"](case_id=interrupt_id, notes="")
    assert "Which hearing date is correct?" in ctx.packet_memos[interrupt_id]


def test_rationale_rejects_numbers_written_as_words(tmp_path: Path) -> None:
    """Round-12 reproducer: 'nine hundred ninety nine days remaining'
    passed the digit-only ban and stored a fabricated figure beside the
    deterministic facts."""
    ctx = make_ctx(tmp_path, [make_record(service_date="2026-09-01")], capacity=1)
    tools = build_tools(ctx)
    tools["get_ranked_queue"]()
    interrupt_id = ctx.interrupt_candidates[0].case_id
    worded = tools["submit_escalation_rationale"](
        case_id=interrupt_id,
        disposition="interrupt",
        explanation=(
            "The effective deadline is December thirty first two thousand "
            "ninety nine, with nine hundred ninety nine days remaining and "
            "queue rank nine."
        ),
        confidence=0.9,
    )
    assert "VALIDATION FAILED" in worded
    assert interrupt_id not in ctx.rationales

    empty_ok = tools["submit_escalation_rationale"](
        case_id=interrupt_id,
        disposition="interrupt",
        explanation="",
        confidence=0.9,
    )
    assert "recorded" in empty_ok
    assert "Writer explanation:" not in ctx.rationales[interrupt_id].rationale
