"""Calendar-table tests: the 2026 holiday sets and their three divergences.

These pin the primary-sourced calendar data (georgia.gov 2026 legal-holiday
list; fultonsuperiorcourtga.gov court-holidays page) and the verification-pass
finding that the two calendars diverge on exactly three dates. February 16 and
December 24 appear on BOTH calendars and are deliberately asserted as
non-divergent, because an earlier research pass wrongly counted them.
"""

from datetime import date, datetime

from engine.holidays import (
    FULTON_COURT_CLOSURES_2026,
    GEORGIA_2026_CALENDAR,
    GEORGIA_LEGAL_HOLIDAYS_2026,
    HolidayCalendar,
)


def test_legal_holiday_exact_set_2026() -> None:
    # Exact-set pin (Grok cross-review): a count test alone cannot catch a
    # same-count date shift on an unpinned holiday. Every date is asserted.
    assert GEORGIA_LEGAL_HOLIDAYS_2026 == frozenset(
        {
            date(2026, 1, 1),
            date(2026, 1, 19),
            date(2026, 2, 16),
            date(2026, 4, 3),
            date(2026, 5, 25),
            date(2026, 6, 19),
            date(2026, 7, 3),
            date(2026, 9, 7),
            date(2026, 10, 12),
            date(2026, 11, 11),
            date(2026, 11, 26),
            date(2026, 11, 27),
            date(2026, 12, 24),
            date(2026, 12, 25),
        }
    )


def test_court_closure_exact_set_2026() -> None:
    assert FULTON_COURT_CLOSURES_2026 == frozenset(
        {
            date(2026, 1, 1),
            date(2026, 1, 19),
            date(2026, 2, 16),
            date(2026, 5, 25),
            date(2026, 6, 19),
            date(2026, 7, 3),
            date(2026, 9, 7),
            date(2026, 11, 11),
            date(2026, 11, 26),
            date(2026, 11, 27),
            date(2026, 12, 24),
            date(2026, 12, 25),
            date(2026, 12, 31),
            date(2027, 1, 1),
        }
    )


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


def test_calendar_construction_rejects_datetime_elements() -> None:
    # datetime subclasses date but never equals one: a datetime in the set
    # would silently erase the holiday and ship a wrong deadline as verified.
    import pytest

    with pytest.raises(TypeError, match="legal_holidays"):
        HolidayCalendar(
            # No type: ignore needed: datetime IS a date to the type system,
            # which is exactly why the runtime check must exist.
            legal_holidays=frozenset({datetime(2026, 1, 19, 12)}),
            court_closures=frozenset(),
            holiday_coverage_start=date(2026, 1, 1),
            holiday_coverage_end=date(2026, 12, 31),
            closure_coverage_end=date(2026, 12, 31),
        )


def test_calendar_construction_rejects_inverted_or_inconsistent_bounds() -> None:
    import pytest

    with pytest.raises(ValueError, match="inverted"):
        HolidayCalendar(
            legal_holidays=frozenset(),
            court_closures=frozenset(),
            holiday_coverage_start=date(2026, 12, 31),
            holiday_coverage_end=date(2026, 1, 1),
            closure_coverage_end=date(2026, 12, 31),
        )
    with pytest.raises(ValueError, match="outside holiday coverage"):
        HolidayCalendar(
            legal_holidays=frozenset({date(2027, 6, 1)}),
            court_closures=frozenset(),
            holiday_coverage_start=date(2026, 1, 1),
            holiday_coverage_end=date(2026, 12, 31),
            closure_coverage_end=date(2027, 1, 1),
        )
    # Closure coverage ending before holiday coverage would make unknown
    # closure dates read as open courthouses (round-4 finding).
    with pytest.raises(ValueError, match="closure_coverage_end"):
        HolidayCalendar(
            legal_holidays=frozenset({date(2026, 8, 17)}),
            court_closures=frozenset(),
            holiday_coverage_start=date(2026, 1, 1),
            holiday_coverage_end=date(2026, 12, 31),
            closure_coverage_end=date(2026, 6, 3),
        )
