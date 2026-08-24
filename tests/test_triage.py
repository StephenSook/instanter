"""Anti-collapse suite for the deterministic triage ladder.

The research record names five collapse conditions under which the ladder
degenerates into "arithmetic in a costume". Each is pinned here:
 1. no weighted sum (structural: dispositions are discrete, no numeric
    urgency field exists on a decision);
 2. each risk discriminator is independently decisive;
 3. contention re-ranks against capacity, never re-scores;
 4. rationale factors are stated facts, not bare numbers;
 5. an explicit hold (L0) and an explicit held-for-capacity reason exist,
    so rationing is stated, never hidden.
"""

from dataclasses import fields
from datetime import date

import pytest

from agent.triage import TriageCase, TriageDecision, UrgencyLevel, triage_queue
from engine.deadline import CaseInput, compute_deadline
from engine.rules import GEORGIA_RULE, ServiceMethod

RUN_DATE = date(2026, 8, 12)  # a Wednesday


def make_case(
    case_id: str,
    service: date | None,
    method: ServiceMethod = ServiceMethod.PERSONAL,
    answer_filed: bool = False,
    **kwargs: object,
) -> TriageCase:
    deadline = compute_deadline(
        CaseInput(
            case_id=case_id,
            jurisdiction_id="GA-FULTON",
            service_date=service,
            service_method=method,
            **kwargs,  # type: ignore[arg-type]
        ),
        GEORGIA_RULE,
    )
    return TriageCase(case_id=case_id, deadline=deadline, answer_filed=answer_filed)


def decide(cases: list[TriageCase], capacity: int = 5) -> dict[str, TriageDecision]:
    return {d.case_id: d for d in triage_queue(cases, RUN_DATE, capacity)}


# --- Acuity floors -----------------------------------------------------------


@pytest.mark.parametrize(
    ("service", "expected_level"),
    [
        (date(2026, 8, 4), UrgencyLevel.L1_INTERRUPT),  # deadline Aug 11, overdue
        (date(2026, 8, 5), UrgencyLevel.L1_INTERRUPT),  # deadline Aug 12, today
        (date(2026, 8, 6), UrgencyLevel.L1_INTERRUPT),  # deadline Aug 13, 1 day
        (date(2026, 8, 7), UrgencyLevel.L2_SURFACE_TODAY),  # deadline Aug 14, 2 days
        (date(2026, 8, 10), UrgencyLevel.L3_MONITOR),  # deadline Aug 17, 5 days
    ],
)
def test_acuity_floor(service: date, expected_level: UrgencyLevel) -> None:
    decisions = decide([make_case("C1", service)])
    assert decisions["C1"].level is expected_level
    assert decisions["C1"].floor_level is expected_level


def test_answer_filed_is_an_explicit_hold_even_on_deadline_day() -> None:
    decisions = decide([make_case("C1", date(2026, 8, 5), answer_filed=True)])
    assert decisions["C1"].level is UrgencyLevel.L0_HOLD
    assert "answer already filed" in decisions["C1"].factors[0]


def test_no_reliable_clock_surfaces_today() -> None:
    decisions = decide([make_case("C1", None)])
    assert decisions["C1"].level is UrgencyLevel.L2_SURFACE_TODAY
    assert decisions["C1"].days_remaining is None


# --- Collapse condition 2: discriminators independently decisive -------------


def test_tack_and_mail_raises_on_its_own_far_from_the_deadline() -> None:
    # Floor is L3 (5 days out); service risk alone must lift it to L2.
    decisions = decide([make_case("C1", date(2026, 8, 10), ServiceMethod.TACK_AND_MAIL)])
    assert decisions["C1"].floor_level is UrgencyLevel.L3_MONITOR
    assert decisions["C1"].level is UrgencyLevel.L2_SURFACE_TODAY
    assert decisions["C1"].raised_by == ("service_method_risk",)


def test_calendar_conflict_raises_on_its_own() -> None:
    decisions = decide(
        [make_case("C1", date(2026, 8, 10), summons_stated_deadline=date(2026, 8, 18))]
    )
    assert decisions["C1"].floor_level is UrgencyLevel.L3_MONITOR
    assert decisions["C1"].level is UrgencyLevel.L2_SURFACE_TODAY
    assert "calendar_move_risk" in decisions["C1"].raised_by


def test_two_discriminators_stack_to_interrupt() -> None:
    decisions = decide(
        [
            make_case(
                "C1",
                date(2026, 8, 10),
                ServiceMethod.TACK_AND_MAIL,
                summons_stated_deadline=date(2026, 8, 18),
            )
        ]
    )
    assert decisions["C1"].level is UrgencyLevel.L1_INTERRUPT
    assert set(decisions["C1"].raised_by) == {"service_method_risk", "calendar_move_risk"}


# --- Collapse condition 3: capacity re-ranks, never re-scores ----------------


def _three_l1_cases() -> list[TriageCase]:
    return [
        make_case("OVERDUE", date(2026, 8, 4)),  # -1 day
        make_case("TODAY", date(2026, 8, 5)),  # 0 days
        make_case("TOMORROW", date(2026, 8, 6)),  # 1 day
    ]


def test_capacity_one_interrupts_only_the_most_severe() -> None:
    decisions = triage_queue(_three_l1_cases(), RUN_DATE, attorney_capacity=1)
    by_id = {d.case_id: d for d in decisions}
    assert by_id["OVERDUE"].interrupt_now
    assert by_id["OVERDUE"].rank == 1
    for cid in ("TODAY", "TOMORROW"):
        held = by_id[cid]
        assert not held.interrupt_now
        assert held.level is UrgencyLevel.L2_SURFACE_TODAY
        reason = held.held_reason
        assert reason is not None
        assert "capacity is 1" in reason


def test_raising_capacity_changes_the_cutoff_not_the_order() -> None:
    ranks_c1 = [d.case_id for d in triage_queue(_three_l1_cases(), RUN_DATE, 1)]
    ranks_c3 = [d.case_id for d in triage_queue(_three_l1_cases(), RUN_DATE, 3)]
    assert ranks_c1 == ranks_c3 == ["OVERDUE", "TODAY", "TOMORROW"]
    assert sum(d.interrupt_now for d in triage_queue(_three_l1_cases(), RUN_DATE, 3)) == 3


def test_zero_capacity_holds_everything_with_stated_reasons() -> None:
    decisions = triage_queue(_three_l1_cases(), RUN_DATE, attorney_capacity=0)
    assert all(not d.interrupt_now for d in decisions)
    assert all(d.held_reason is not None for d in decisions)


def test_negative_capacity_refuses() -> None:
    with pytest.raises(ValueError, match="attorney_capacity"):
        triage_queue([], RUN_DATE, attorney_capacity=-1)


# --- Collapse conditions 1 and 4: no scores, facts in the rationale ----------


def test_decision_carries_no_numeric_urgency_field() -> None:
    numeric_fields = {
        f.name
        for f in fields(TriageDecision)
        if f.type in ("float", "int") and f.name not in {"rank", "days_remaining"}
    }
    assert numeric_fields == set(), "a numeric urgency field is the first collapse symptom"


def test_factors_state_the_operative_facts() -> None:
    decisions = decide([make_case("C1", date(2026, 8, 6))])
    factors = decisions["C1"].factors
    assert factors, "every decision must explain itself"
    assert any("2026-08-13" in f for f in factors)  # the actual deadline date


def test_severity_ordering_is_deterministic_and_stable() -> None:
    cases = [
        make_case("B", date(2026, 8, 5)),
        make_case("A", date(2026, 8, 5)),  # identical severity; id breaks the tie
    ]
    order_one = [d.case_id for d in triage_queue(cases, RUN_DATE, 2)]
    order_two = [d.case_id for d in triage_queue(list(reversed(cases)), RUN_DATE, 2)]
    assert order_one == order_two == ["A", "B"]
