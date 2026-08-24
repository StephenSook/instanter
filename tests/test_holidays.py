"""Calendar-table tests: the 2026 holiday sets and their three divergences.

These pin the primary-sourced calendar data (georgia.gov 2026 legal-holiday
list; fultonsuperiorcourtga.gov court-holidays page) and the verification-pass
finding that the two calendars diverge on exactly three dates. February 16 and
December 24 appear on BOTH calendars and are deliberately asserted as
non-divergent, because an earlier research pass wrongly counted them.
"""

from datetime import date

from engine.holidays import (
    FULTON_COURT_CLOSURES_2026,
    GEORGIA_2026_CALENDAR,
    GEORGIA_LEGAL_HOLIDAYS_2026,
)


def test_legal_holiday_count_2026() -> None:
    assert len(GEORGIA_LEGAL_HOLIDAYS_2026) == 14


def test_court_closure_count_2026_window() -> None:
    assert len(FULTON_COURT_CLOSURES_2026) == 14  # 13 in 2026 plus 2027-01-01


def test_divergences_are_exactly_three() -> None:
    holiday_court_open, closure_not_holiday = GEORGIA_2026_CALENDAR.divergences()
    assert holiday_court_open == frozenset({date(2026, 4, 3), date(2026, 10, 12)})
    assert closure_not_holiday == frozenset({date(2026, 12, 31)})


def test_feb_16_and_dec_24_are_on_both_calendars() -> None:
    for d in (date(2026, 2, 16), date(2026, 12, 24)):
        assert GEORGIA_2026_CALENDAR.is_legal_holiday(d)
        assert GEORGIA_2026_CALENDAR.is_court_closure(d)


def test_dec_31_is_closure_but_not_holiday() -> None:
    d = date(2026, 12, 31)
    assert GEORGIA_2026_CALENDAR.is_court_closure(d)
    assert not GEORGIA_2026_CALENDAR.is_legal_holiday(d)


def test_new_years_2027_is_encoded_as_closure_and_is_the_coverage_boundary() -> None:
    assert GEORGIA_2026_CALENDAR.is_court_closure(date(2027, 1, 1))
    assert GEORGIA_2026_CALENDAR.closure_coverage_end == date(2027, 1, 1)


def test_july_observance_is_friday_july_3() -> None:
    assert GEORGIA_2026_CALENDAR.is_legal_holiday(date(2026, 7, 3))
    assert not GEORGIA_2026_CALENDAR.is_legal_holiday(date(2026, 7, 4))


def test_coverage_guard_rejects_2027() -> None:
    assert GEORGIA_2026_CALENDAR.holiday_knowledge_covers(date(2026, 12, 31))
    assert not GEORGIA_2026_CALENDAR.holiday_knowledge_covers(date(2027, 1, 4))
