"""Jurisdiction rule table for answer-deadline computation.

One row per jurisdiction. Adding a state is adding a row plus a holiday
calendar; the computation code does not change. The Georgia row is the only
one populated from primary sources today, and the engine refuses to compute
for a jurisdiction it does not have a row for.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from zoneinfo import ZoneInfo

from engine.holidays import GEORGIA_2026_CALENDAR, HolidayCalendar


class ServiceMethod(Enum):
    PERSONAL = "personal"
    NOTORIOUS = "notorious"  # left with a resident adult, treated like personal
    TACK_AND_MAIL = "tack_and_mail"
    UNKNOWN = "unknown"


class CountingBasis(Enum):
    DAY_OF_SERVICE_EXCLUDED = "day_of_service_excluded"
    DAY_OF_SERVICE_INCLUDED = "day_of_service_included"


class TerminalRoll(Enum):
    NEXT_NON_WEEKEND_NON_HOLIDAY = "next_non_weekend_non_holiday"
    NONE = "none"


@dataclass(frozen=True)
class JurisdictionRule:
    jurisdiction_id: str
    citation_string: str
    window_length_days: int
    counting_basis: CountingBasis
    intermediate_days_counted: bool  # calendar days count in the window
    terminal_roll: TerminalRoll
    calendar: HolidayCalendar

    # Service-method notes that must surface with a computed deadline.
    tack_and_mail_money_judgment_note: str
    tender_window_days: int | None  # O.C.G.A. 44-7-52 parallel advisory window
    deadline_time_of_day: str  # local clerk close, display-level
    deadline_timezone: str
    notes: str
    # Prose labels used in attorney-facing warnings, so a non-Georgia rule
    # row never emits Georgia- or Fulton-specific legal claims.
    jurisdiction_label: str = "this jurisdiction"
    court_label: str = "the courthouse"

    def __post_init__(self) -> None:
        # A rule row is legal configuration; a malformed row must fail closed
        # at construction, never fall open into a wrong deadline downstream
        # (a serialized string in an enum field would otherwise skip the
        # terminal roll and yield a weekend deadline with no flag).
        if not isinstance(self.counting_basis, CountingBasis):
            raise TypeError(f"counting_basis must be a CountingBasis, got {self.counting_basis!r}")
        if not isinstance(self.terminal_roll, TerminalRoll):
            raise TypeError(f"terminal_roll must be a TerminalRoll, got {self.terminal_roll!r}")
        # type() checks, not isinstance(): bool is a subclass of int, and a
        # row with window_length_days=True would otherwise compute a one-day
        # window without a whisper of protest.
        if type(self.window_length_days) is not int or self.window_length_days < 1:
            raise ValueError(
                f"window_length_days must be a positive int, got {self.window_length_days!r}"
            )
        if self.intermediate_days_counted is not True:
            # The engine implements calendar-day counting only. A row claiming
            # business-day intermediates would be silently computed as calendar
            # days, which is a wrong deadline wearing a valid config.
            raise NotImplementedError(
                "intermediate_days_counted=False is not implemented; "
                "refusing a rule row the engine would misinterpret"
            )
        if self.tender_window_days is not None and (
            type(self.tender_window_days) is not int or self.tender_window_days < 1
        ):
            raise ValueError(
                f"tender_window_days must be None or a positive int, "
                f"got {self.tender_window_days!r}"
            )
        if self.counting_basis is not CountingBasis.DAY_OF_SERVICE_EXCLUDED:
            # Only day-of-service-excluded counting is implemented; a row
            # claiming otherwise would be miscomputed, so it must refuse here,
            # not crash mid-computation.
            raise NotImplementedError(
                f"counting basis {self.counting_basis.value!r} is not implemented"
            )
        if not isinstance(self.calendar, HolidayCalendar):
            raise TypeError(f"calendar must be a HolidayCalendar, got {type(self.calendar)!r}")
        # deadline_time_of_day must be exactly HH:MM; a seconds-bearing value
        # would crash deep inside computation instead of failing here.
        parts = self.deadline_time_of_day.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError(
                f"deadline_time_of_day must be 'HH:MM', got {self.deadline_time_of_day!r}"
            )
        hour, minute = int(parts[0]), int(parts[1])
        if hour > 23 or minute > 59:
            raise ValueError(
                f"deadline_time_of_day must be a valid clock time, "
                f"got {self.deadline_time_of_day!r}"
            )
        # Instantiating the zone at construction surfaces an unknown timezone
        # immediately rather than at the first computed deadline.
        ZoneInfo(self.deadline_timezone)
        for label_name, label in (
            ("jurisdiction_label", self.jurisdiction_label),
            ("court_label", self.court_label),
        ):
            if type(label) is not str or not label.strip():
                raise ValueError(f"{label_name} must be a non-empty string, got {label!r}")


GEORGIA_RULE = JurisdictionRule(
    jurisdiction_id="GA-FULTON",
    citation_string="O.C.G.A. 44-7-51(b); O.C.G.A. 1-3-1(d)(3); O.C.G.A. 1-4-1",
    window_length_days=7,
    counting_basis=CountingBasis.DAY_OF_SERVICE_EXCLUDED,
    intermediate_days_counted=True,  # 7-day window is not "less than seven days",
    # so the sub-seven-day intermediate-exclusion clause of 1-3-1(d)(3) does NOT apply
    terminal_roll=TerminalRoll.NEXT_NON_WEEKEND_NON_HOLIDAY,
    calendar=GEORGIA_2026_CALENDAR,
    tack_and_mail_money_judgment_note=(
        "Service by tack and mail: default judgment for possession is possible, "
        "but no money judgment absent an answer or appearance (O.C.G.A. 44-7-51(c))."
    ),
    tender_window_days=7,
    deadline_time_of_day="17:00",
    deadline_timezone="America/New_York",
    notes=(
        "The last possible date to answer must be stated on the summons "
        "(O.C.G.A. 44-7-51(b)); a summons-stated date controls for the tenant. "
        "The Fulton summons rolls off a 'Court holiday' while the statute says "
        "'legal holiday'; the engine surfaces any date where those calendars diverge."
    ),
    jurisdiction_label="Georgia",
    court_label="the Fulton courthouse",
)

RULES: dict[str, JurisdictionRule] = {GEORGIA_RULE.jurisdiction_id: GEORGIA_RULE}
