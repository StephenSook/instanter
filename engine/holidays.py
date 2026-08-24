"""Holiday and court-closure calendars for the deadline engine.

Two calendars are deliberately separate, because they diverge and the
divergences are load-bearing:

* The Georgia LEGAL-HOLIDAY set drives the statutory terminal-day roll in
  O.C.G.A. 44-7-51(b) via O.C.G.A. 1-4-1. Under 1-4-1(a) (as amended by
  HB 1335, effective April 4, 2022) it is the federal public and legal
  holidays as designated on January 1, 2022, plus the days the Governor
  proclaims each year. The Governor's annual memo is the operative source
  for observed dates.
* The Fulton County courthouse CLOSURE calendar is the clerk's own list
  (fultonsuperiorcourtga.gov/court-holidays). The courthouse can be open on
  a state legal holiday and closed on a day that is not one. The statute
  keys to legal holidays, not courthouse closures, so a closure that is not
  a holiday must be flagged for a human, never silently resolved.

For 2026 the two calendars diverge on exactly three dates:
  * 2026-04-03  state legal holiday (Good Friday), courthouse OPEN
  * 2026-10-12  state legal holiday (Columbus Day), courthouse OPEN
  * 2026-12-31  courthouse CLOSED, NOT a legal holiday (the statute does
                not roll a day-7 deadline off this date)

Coverage is explicit: any computation touching a date outside the encoded
coverage window must flag a calendar gap rather than guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# Georgia legal holidays, 2026 (georgia.gov official list; federal-designated
# days per 1-4-1(a)(1) plus Governor-proclaimed days per 1-4-1(a)(2)).
GEORGIA_LEGAL_HOLIDAYS_2026: frozenset[date] = frozenset(
    {
        date(2026, 1, 1),  # New Year's Day (Thu)
        date(2026, 1, 19),  # Martin Luther King Jr. Day (Mon)
        date(2026, 2, 16),  # Washington's Birthday (Mon; federal-designated)
        date(2026, 4, 3),  # State Holiday, Good Friday (Fri; Governor-proclaimed)
        date(2026, 5, 25),  # Memorial Day (Mon)
        date(2026, 6, 19),  # Juneteenth (Fri)
        date(2026, 7, 3),  # Independence Day observed (Fri; Jul 4 falls on Sat)
        date(2026, 9, 7),  # Labor Day (Mon)
        date(2026, 10, 12),  # Columbus Day (Mon; federal-designated)
        date(2026, 11, 11),  # Veterans Day (Wed)
        date(2026, 11, 26),  # Thanksgiving (Thu)
        date(2026, 11, 27),  # State Holiday (Fri; Governor-proclaimed)
        date(2026, 12, 24),  # State observance day per georgia.gov 2026 list (Thu)
        date(2026, 12, 25),  # Christmas (Fri)
    }
)

# Fulton County courthouse closures, 2026 window
# (fultonsuperiorcourtga.gov/court-holidays; includes the New Year closure
# that opens the 2027 window).
FULTON_COURT_CLOSURES_2026: frozenset[date] = frozenset(
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
        date(2026, 12, 31),  # closed, NOT a legal holiday: the dangerous one
        date(2027, 1, 1),
    }
)

# The legal-holiday table is authoritative for 2026 only; the closure table
# additionally covers the 2027-01-01 closure. A terminal-day computation that
# needs holiday knowledge outside this window must flag, not guess.
HOLIDAY_COVERAGE_START: date = date(2026, 1, 1)
HOLIDAY_COVERAGE_END: date = date(2026, 12, 31)
CLOSURE_COVERAGE_END: date = date(2027, 1, 1)


@dataclass(frozen=True)
class HolidayCalendar:
    """A jurisdiction's legal-holiday set and courthouse-closure set.

    Construction validates every element, because calendar data IS the law
    here and a single malformed entry produces a wrong deadline presented as
    verified: a ``datetime`` in the set compares unequal to the ``date`` being
    checked, so the holiday silently stops existing and the terminal roll
    never fires.
    """

    legal_holidays: frozenset[date]
    court_closures: frozenset[date]
    holiday_coverage_start: date
    holiday_coverage_end: date
    closure_coverage_end: date

    def __post_init__(self) -> None:
        for name, collection in (
            ("legal_holidays", self.legal_holidays),
            ("court_closures", self.court_closures),
        ):
            if not isinstance(collection, frozenset):
                raise TypeError(f"{name} must be a frozenset of dates, got {type(collection)!r}")
            for d in collection:
                # type() is date, not isinstance: datetime subclasses date but
                # never equals one, which would silently erase the holiday.
                if type(d) is not date:
                    raise TypeError(f"{name} entries must be datetime.date, got {d!r}")
        for name, d in (
            ("holiday_coverage_start", self.holiday_coverage_start),
            ("holiday_coverage_end", self.holiday_coverage_end),
            ("closure_coverage_end", self.closure_coverage_end),
        ):
            if type(d) is not date:
                raise TypeError(f"{name} must be a datetime.date, got {d!r}")
        if self.holiday_coverage_start > self.holiday_coverage_end:
            raise ValueError("holiday coverage bounds are inverted")
        if self.closure_coverage_end < self.holiday_coverage_start:
            raise ValueError("closure_coverage_end precedes holiday_coverage_start")
        for d in self.legal_holidays:
            if not (self.holiday_coverage_start <= d <= self.holiday_coverage_end):
                raise ValueError(f"legal holiday {d.isoformat()} lies outside holiday coverage")
        for d in self.court_closures:
            if not (self.holiday_coverage_start <= d <= self.closure_coverage_end):
                raise ValueError(f"court closure {d.isoformat()} lies outside closure coverage")

    def is_legal_holiday(self, day: date) -> bool:
        return day in self.legal_holidays

    def is_court_closure(self, day: date) -> bool:
        return day in self.court_closures

    def holiday_knowledge_covers(self, day: date) -> bool:
        return self.holiday_coverage_start <= day <= self.holiday_coverage_end

    def divergences(self) -> tuple[frozenset[date], frozenset[date]]:
        """(holidays where the courthouse is open, closures that are not holidays).

        Closure-side divergence is reported only inside the holiday coverage
        window, because outside it we cannot know whether a closure is also a
        holiday.
        """
        holiday_court_open = frozenset(
            d for d in self.legal_holidays if d not in self.court_closures
        )
        closure_not_holiday = frozenset(
            d
            for d in self.court_closures
            if d not in self.legal_holidays and self.holiday_knowledge_covers(d)
        )
        return holiday_court_open, closure_not_holiday


GEORGIA_2026_CALENDAR = HolidayCalendar(
    legal_holidays=GEORGIA_LEGAL_HOLIDAYS_2026,
    court_closures=FULTON_COURT_CLOSURES_2026,
    holiday_coverage_start=HOLIDAY_COVERAGE_START,
    holiday_coverage_end=HOLIDAY_COVERAGE_END,
    closure_coverage_end=CLOSURE_COVERAGE_END,
)
