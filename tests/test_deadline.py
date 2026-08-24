"""Statutory corpus for the deterministic deadline engine.

Every case pins an exact expected date (never a derived one), per the rule
that a property test alone cannot detect an inverted ladder. The corpus
covers: plain weekday windows, weekend rolls, every 2026 holiday chain the
statute produces, both court-calendar divergence directions, the December 31
trap, tack-and-mail flags, the summons-conflict rule, refusal paths, the
calendar coverage guard, and jurisdiction-table extensibility.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from engine.deadline import (
    CaseInput,
    DeadlineBasis,
    DeadlineResult,
    FlagCode,
    compute_deadline,
)
from engine.rules import (
    GEORGIA_RULE,
    CountingBasis,
    JurisdictionRule,
    ServiceMethod,
    TerminalRoll,
)


def make_case(
    service: date | None,
    method: ServiceMethod = ServiceMethod.PERSONAL,
    **kwargs: object,
) -> CaseInput:
    return CaseInput(
        case_id="26ED000001",
        jurisdiction_id="GA-FULTON",
        service_date=service,
        service_method=method,
        **kwargs,  # type: ignore[arg-type]
    )


def compute(case: CaseInput) -> DeadlineResult:
    return compute_deadline(case, GEORGIA_RULE)


def flag_codes(result: DeadlineResult) -> set[FlagCode]:
    return {f.code for f in result.flags}


# --- Plain windows: day 7 lands on an open weekday, no roll -----------------


@pytest.mark.parametrize(
    ("service", "expected"),
    [
        (date(2026, 8, 10), date(2026, 8, 17)),  # Mon -> Mon
        (date(2026, 8, 4), date(2026, 8, 11)),  # Tue -> Tue
        (date(2026, 8, 5), date(2026, 8, 12)),  # Wed -> Wed
        (date(2026, 8, 6), date(2026, 8, 13)),  # Thu -> Thu
        (date(2026, 8, 7), date(2026, 8, 14)),  # Fri -> Fri
    ],
)
def test_plain_seven_day_window(service: date, expected: date) -> None:
    result = compute(make_case(service))
    assert result.computed_deadline == expected
    assert result.effective_deadline == expected
    assert result.flags == ()
    assert (expected - service).days == 7  # intermediates counted, day 0 excluded


# --- Weekend terminal rolls --------------------------------------------------


@pytest.mark.parametrize(
    ("service", "expected"),
    [
        (date(2026, 8, 8), date(2026, 8, 17)),  # day 7 Sat Aug 15 -> Mon Aug 17
        (date(2026, 8, 9), date(2026, 8, 17)),  # day 7 Sun Aug 16 -> Mon Aug 17
    ],
)
def test_weekend_roll(service: date, expected: date) -> None:
    result = compute(make_case(service))
    assert result.computed_deadline == expected
    assert result.flags == ()


# --- Holiday chains ----------------------------------------------------------


def test_mlk_day_roll() -> None:
    # Day 7 = Mon Jan 19 (MLK Day, courthouse also closed) -> Tue Jan 20.
    result = compute(make_case(date(2026, 1, 12)))
    assert result.computed_deadline == date(2026, 1, 20)
    assert result.flags == ()


def test_independence_day_observed_chain() -> None:
    # Day 7 = Fri Jul 3 (observed holiday) -> Sat -> Sun -> Mon Jul 6.
    result = compute(make_case(date(2026, 6, 26)))
    assert result.computed_deadline == date(2026, 7, 6)
    assert result.flags == ()


def test_thanksgiving_double_holiday_chain() -> None:
    # Day 7 = Thu Nov 26 -> Fri Nov 27 (state holiday) -> weekend -> Mon Nov 30.
    result = compute(make_case(date(2026, 11, 19)))
    assert result.computed_deadline == date(2026, 11, 30)
    assert result.flags == ()


def test_christmas_double_holiday_chain() -> None:
    # Day 7 = Thu Dec 24 (state observance) -> Fri Dec 25 -> weekend -> Mon Dec 28.
    result = compute(make_case(date(2026, 12, 17)))
    assert result.computed_deadline == date(2026, 12, 28)
    assert result.flags == ()


# --- Divergence direction 1: legal holiday, courthouse open ------------------


def test_good_friday_rolls_with_court_open_flag() -> None:
    # Day 7 = Fri Apr 3: Georgia legal holiday, Fulton courthouse OPEN.
    # Statute rolls; the calendar gap is surfaced, not resolved.
    result = compute(make_case(date(2026, 3, 27)))
    assert result.computed_deadline == date(2026, 4, 6)
    assert flag_codes(result) == {FlagCode.STATE_HOLIDAY_COURT_OPEN}


def test_columbus_day_rolls_with_court_open_flag() -> None:
    # Day 7 = Mon Oct 12: legal holiday, courthouse OPEN -> Tue Oct 13 + flag.
    result = compute(make_case(date(2026, 10, 5)))
    assert result.computed_deadline == date(2026, 10, 13)
    assert flag_codes(result) == {FlagCode.STATE_HOLIDAY_COURT_OPEN}


# --- Divergence direction 2: the December 31 trap ----------------------------


def test_dec_31_trap_does_not_roll_and_flags() -> None:
    # Day 7 = Thu Dec 31: courthouse CLOSED but NOT a legal holiday.
    # The statute does not roll it; the engine must flag, never resolve.
    result = compute(make_case(date(2026, 12, 24)))
    assert result.computed_deadline == date(2026, 12, 31)
    assert FlagCode.COURT_CLOSED_NOT_LEGAL_HOLIDAY in flag_codes(result)
    # Closure coverage ends 2027-01-01, so the reopening date is unknowable
    # from encoded data and must be None, never a guess.
    assert result.court_reopens_on is None
    assert result.needs_human_review


# --- Calendar coverage guard -------------------------------------------------


@pytest.mark.parametrize(
    "service",
    [
        date(2026, 12, 25),  # day 7 = 2027-01-01, outside holiday coverage
        date(2026, 12, 28),  # day 7 = 2027-01-04, outside holiday coverage
    ],
)
def test_coverage_gap_refuses_instead_of_guessing(service: date) -> None:
    result = compute(make_case(service))
    assert result.computed_deadline is None
    assert FlagCode.CALENDAR_COVERAGE_GAP in flag_codes(result)


# --- Service-method discriminators -------------------------------------------


def test_tack_and_mail_always_flags_for_review() -> None:
    result = compute(make_case(date(2026, 8, 10), ServiceMethod.TACK_AND_MAIL))
    assert result.computed_deadline == date(2026, 8, 17)
    assert FlagCode.TACK_AND_MAIL_REVIEW in flag_codes(result)


def test_tack_and_mail_date_split_flags_both() -> None:
    result = compute(
        make_case(
            date(2026, 8, 10),
            ServiceMethod.TACK_AND_MAIL,
            posting_date=date(2026, 8, 10),
            mailing_date=date(2026, 8, 11),
        )
    )
    assert FlagCode.TACK_AND_MAIL_REVIEW in flag_codes(result)
    assert FlagCode.TACK_AND_MAIL_DATE_SPLIT in flag_codes(result)


def test_unknown_service_method_flags() -> None:
    result = compute(make_case(date(2026, 8, 10), ServiceMethod.UNKNOWN))
    assert FlagCode.UNKNOWN_SERVICE_METHOD in flag_codes(result)


def test_amended_affidavit_flags() -> None:
    result = compute(make_case(date(2026, 8, 10), amended_affidavit=True))
    assert FlagCode.AMENDED_AFFIDAVIT in flag_codes(result)


# --- Summons-stated date controls --------------------------------------------


def test_summons_conflict_surfaces_and_summons_controls() -> None:
    result = compute(make_case(date(2026, 8, 10), summons_stated_deadline=date(2026, 8, 18)))
    assert result.computed_deadline == date(2026, 8, 17)
    assert result.effective_deadline == date(2026, 8, 18)
    assert FlagCode.SUMMONS_DATE_CONFLICT in flag_codes(result)


def test_summons_agreement_is_clean() -> None:
    result = compute(make_case(date(2026, 8, 10), summons_stated_deadline=date(2026, 8, 17)))
    assert result.effective_deadline == date(2026, 8, 17)
    assert result.flags == ()


# --- Refusal paths -----------------------------------------------------------


def test_missing_service_date_refuses() -> None:
    result = compute(make_case(None))
    assert result.computed_deadline is None
    assert FlagCode.SERVICE_DATE_MISSING in flag_codes(result)


def test_missing_service_date_with_summons_keeps_summons_effective() -> None:
    result = compute(make_case(None, summons_stated_deadline=date(2026, 8, 18)))
    assert result.computed_deadline is None
    assert result.effective_deadline == date(2026, 8, 18)


def test_unsupported_jurisdiction_refuses() -> None:
    case = CaseInput(
        case_id="X",
        jurisdiction_id="TX-HARRIS",
        service_date=date(2026, 8, 10),
        service_method=ServiceMethod.PERSONAL,
    )
    result = compute_deadline(case, GEORGIA_RULE)
    assert result.refused
    assert FlagCode.JURISDICTION_UNSUPPORTED in flag_codes(result)


# --- Ancillary outputs -------------------------------------------------------


def test_deadline_datetime_is_5pm_eastern() -> None:
    result = compute(make_case(date(2026, 8, 10)))
    assert result.deadline_at == datetime(2026, 8, 17, 17, 0, tzinfo=ZoneInfo("America/New_York"))


def test_tender_window_computed_in_parallel() -> None:
    result = compute(make_case(date(2026, 8, 10)))
    assert result.tender_deadline == date(2026, 8, 17)


def test_trace_starts_at_service_and_ends_at_deadline() -> None:
    result = compute(make_case(date(2026, 6, 26)))
    assert result.trace[0].day == date(2026, 6, 26)
    assert "day 0" in result.trace[0].label
    assert result.trace[-1].day == date(2026, 7, 6)


# --- Fail-closed regressions (adversarial-review findings, 2026-08-24) -------


def test_coverage_gap_with_summons_is_labeled_unverified() -> None:
    # A refused computation must never launder a summons date into a
    # finalized-looking deadline: the basis says exactly what it rests on.
    result = compute(make_case(date(2026, 12, 25), summons_stated_deadline=date(2027, 1, 4)))
    assert result.computed_deadline is None
    assert result.computation_refused
    assert result.effective_deadline == date(2027, 1, 4)  # summons controls for the tenant
    assert result.deadline_basis is DeadlineBasis.SUMMONS_ONLY_UNVERIFIED
    assert FlagCode.CALENDAR_COVERAGE_GAP in flag_codes(result)


@pytest.mark.parametrize(
    ("case_kwargs", "expected_basis"),
    [
        ({}, DeadlineBasis.COMPUTED),
        ({"summons_stated_deadline": date(2026, 8, 17)}, DeadlineBasis.SUMMONS_CONFIRMS),
        ({"summons_stated_deadline": date(2026, 8, 18)}, DeadlineBasis.SUMMONS_CONTROLS),
    ],
)
def test_deadline_basis_reflects_provenance(
    case_kwargs: dict[str, object], expected_basis: DeadlineBasis
) -> None:
    result = compute(make_case(date(2026, 8, 10), **case_kwargs))  # type: ignore[arg-type]
    assert result.deadline_basis is expected_basis


def test_missing_service_with_summons_is_unverified_basis() -> None:
    result = compute(make_case(None, summons_stated_deadline=date(2026, 8, 18)))
    assert result.deadline_basis is DeadlineBasis.SUMMONS_ONLY_UNVERIFIED
    assert result.computation_refused


def test_malformed_rule_rows_fail_closed_at_construction() -> None:
    # A serialized string in an enum field must raise, never skip the roll.
    base = {
        "jurisdiction_id": "XX-BAD",
        "citation_string": "n/a",
        "window_length_days": 7,
        "counting_basis": CountingBasis.DAY_OF_SERVICE_EXCLUDED,
        "intermediate_days_counted": True,
        "terminal_roll": TerminalRoll.NEXT_NON_WEEKEND_NON_HOLIDAY,
        "calendar": GEORGIA_RULE.calendar,
        "tack_and_mail_money_judgment_note": "n/a",
        "tender_window_days": None,
        "deadline_time_of_day": "17:00",
        "deadline_timezone": "America/New_York",
        "notes": "",
    }
    with pytest.raises(TypeError):
        JurisdictionRule(**{**base, "terminal_roll": "next_non_weekend_non_holiday"})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        JurisdictionRule(**{**base, "counting_basis": "day_of_service_excluded"})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="window_length_days"):
        JurisdictionRule(**{**base, "window_length_days": 0})  # type: ignore[arg-type]


# --- Jurisdiction-table extensibility ----------------------------------------


def test_second_jurisdiction_row_changes_window_without_code_change() -> None:
    fake_rule = JurisdictionRule(
        jurisdiction_id="XX-TEST",
        citation_string="Test Stat. 1-2-3",
        window_length_days=10,
        counting_basis=CountingBasis.DAY_OF_SERVICE_EXCLUDED,
        intermediate_days_counted=True,
        terminal_roll=TerminalRoll.NEXT_NON_WEEKEND_NON_HOLIDAY,
        calendar=GEORGIA_RULE.calendar,
        tack_and_mail_money_judgment_note="n/a",
        tender_window_days=None,
        deadline_time_of_day="17:00",
        deadline_timezone="America/New_York",
        notes="synthetic row proving extensibility",
    )
    case = CaseInput(
        case_id="X",
        jurisdiction_id="XX-TEST",
        service_date=date(2026, 8, 10),
        service_method=ServiceMethod.PERSONAL,
    )
    result = compute_deadline(case, fake_rule)
    assert result.computed_deadline == date(2026, 8, 20)  # Mon + 10 -> Thu, no roll
    assert result.tender_deadline is None


# --- Property invariants (pinned corpus above catches inversions) ------------


@given(
    st.dates(min_value=date(2026, 1, 2), max_value=date(2026, 11, 30)),
)
def test_invariants_hold_across_2026(service: date) -> None:
    result = compute(make_case(service))
    computed = result.computed_deadline
    assert computed is not None
    assert (computed - service).days >= 7
    assert (computed - service).days <= 12
    assert computed.weekday() < 5
    assert not GEORGIA_RULE.calendar.is_legal_holiday(computed)
