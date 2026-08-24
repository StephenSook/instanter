"""Deterministic answer-deadline computation.

This module is the safety-critical core of Instanter. It is plain Python on
purpose: a statutory deadline is not a judgment call, so no model touches it
(AWS Prescriptive Guidance: "Use deterministic execution logic unless AI is
needed"). Every computation returns a full day-by-day trace for the audit log
and the attorney, and every ambiguity becomes an explicit flag for a human,
never a silent resolution.

Boundary notes that shape the API:
* An unknown or disputed service date REFUSES to produce a deadline.
* A summons-stated last-answer date CONTROLS for the tenant when it conflicts
  with the computed date (O.C.G.A. 44-7-51(b) requires the date on the
  summons); the discrepancy is surfaced, not resolved.
* A terminal day landing on a courthouse closure that is not a legal holiday
  (December 31, 2026) does NOT roll, because the statute keys to legal
  holidays; it is flagged as the dangerous case it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from engine.rules import CountingBasis, JurisdictionRule, ServiceMethod, TerminalRoll


class FlagCode(Enum):
    SERVICE_DATE_MISSING = "service_date_missing"
    UNKNOWN_SERVICE_METHOD = "unknown_service_method"
    TACK_AND_MAIL_REVIEW = "tack_and_mail_review"
    TACK_AND_MAIL_DATE_SPLIT = "tack_and_mail_date_split"
    AMENDED_AFFIDAVIT = "amended_affidavit"
    SUMMONS_DATE_CONFLICT = "summons_date_conflict"
    COURT_CLOSED_NOT_LEGAL_HOLIDAY = "court_closed_not_legal_holiday"
    STATE_HOLIDAY_COURT_OPEN = "state_holiday_court_open"
    CALENDAR_COVERAGE_GAP = "calendar_coverage_gap"
    JURISDICTION_UNSUPPORTED = "jurisdiction_unsupported"


@dataclass(frozen=True)
class Flag:
    code: FlagCode
    reason: str


@dataclass(frozen=True)
class TraceStep:
    day: date
    label: str


@dataclass(frozen=True)
class CaseInput:
    """Deadline-driving intake facts, as entered by clinic staff.

    ``service_date`` is the operative date of actual service the staff member
    read off the served papers. ``posting_date`` and ``mailing_date`` are the
    tack-and-mail component dates when the staff member captured both.
    """

    case_id: str
    jurisdiction_id: str
    service_date: date | None
    service_method: ServiceMethod
    posting_date: date | None = None
    mailing_date: date | None = None
    summons_stated_deadline: date | None = None
    amended_affidavit: bool = False


@dataclass(frozen=True)
class DeadlineResult:
    case_id: str
    computed_deadline: date | None
    effective_deadline: date | None
    deadline_at: datetime | None
    tender_deadline: date | None
    court_reopens_on: date | None
    flags: tuple[Flag, ...]
    trace: tuple[TraceStep, ...] = field(default=())
    citation: str = ""

    @property
    def needs_human_review(self) -> bool:
        return len(self.flags) > 0

    @property
    def refused(self) -> bool:
        return self.computed_deadline is None and self.effective_deadline is None


def _is_weekend(day: date) -> bool:
    return day.weekday() >= 5


def _next_court_open_day(rule: JurisdictionRule, start: date) -> date | None:
    """First day after ``start`` that is a weekday and not a courthouse closure.

    Advisory only; used to tell the attorney when the clerk's office reopens.
    Returns None if the closure table's coverage runs out first.
    """
    day = start
    for _ in range(30):
        day = day + timedelta(days=1)
        if day > rule.calendar.closure_coverage_end and rule.calendar.is_court_closure(day):
            return None
        if not _is_weekend(day) and not rule.calendar.is_court_closure(day):
            return day
    return None


def _roll_terminal_day(
    rule: JurisdictionRule, candidate: date, trace: list[TraceStep], flags: list[Flag]
) -> date | None:
    """Roll the terminal day per the rule; returns None on a coverage gap."""
    day = candidate
    for _ in range(15):  # longest real chains are 4-5 days; 15 is a hard stop
        if not rule.calendar.holiday_knowledge_covers(day):
            flags.append(
                Flag(
                    FlagCode.CALENDAR_COVERAGE_GAP,
                    f"Terminal day {day.isoformat()} is outside the encoded "
                    "legal-holiday calendar; refusing to finalize a deadline "
                    "without primary-source holiday data for that year.",
                )
            )
            trace.append(TraceStep(day, "outside holiday-calendar coverage; computation stopped"))
            return None
        if _is_weekend(day):
            trace.append(TraceStep(day, f"{day.strftime('%A')}; roll forward"))
            day = day + timedelta(days=1)
            continue
        if rule.calendar.is_legal_holiday(day):
            if not rule.calendar.is_court_closure(day):
                flags.append(
                    Flag(
                        FlagCode.STATE_HOLIDAY_COURT_OPEN,
                        f"{day.isoformat()} is a Georgia legal holiday, but the "
                        "Fulton courthouse is open that day. The statute rolls "
                        "the deadline; the summons keys to 'Court holiday'. "
                        "Summons-stated date controls for the tenant.",
                    )
                )
            trace.append(TraceStep(day, "Georgia legal holiday; roll forward"))
            day = day + timedelta(days=1)
            continue
        return day
    raise RuntimeError("terminal-day roll exceeded 15 days; calendar data is malformed")


def compute_deadline(case: CaseInput, rule: JurisdictionRule) -> DeadlineResult:
    """Compute the statutory last day to answer for one case.

    Counting (Georgia row): the day of actual service is day zero
    (O.C.G.A. 1-3-1(d)(3): "the first day shall not be counted"); the window
    runs in calendar days including intervening weekends and holidays (a
    seven-day window is not "less than seven days", so the sub-seven-day
    intermediate-exclusion clause does not apply); only the terminal day rolls
    forward past Saturdays, Sundays, and Georgia legal holidays
    (O.C.G.A. 44-7-51(b)).
    """
    flags: list[Flag] = []
    trace: list[TraceStep] = []

    if case.jurisdiction_id != rule.jurisdiction_id:
        return DeadlineResult(
            case_id=case.case_id,
            computed_deadline=None,
            effective_deadline=None,
            deadline_at=None,
            tender_deadline=None,
            court_reopens_on=None,
            flags=(
                Flag(
                    FlagCode.JURISDICTION_UNSUPPORTED,
                    f"No rule row for jurisdiction {case.jurisdiction_id!r}; "
                    "the engine does not guess out-of-state deadlines.",
                ),
            ),
            citation=rule.citation_string,
        )

    if case.service_date is None:
        return DeadlineResult(
            case_id=case.case_id,
            computed_deadline=None,
            effective_deadline=case.summons_stated_deadline,
            deadline_at=None,
            tender_deadline=None,
            court_reopens_on=None,
            flags=(
                Flag(
                    FlagCode.SERVICE_DATE_MISSING,
                    "Service date unknown or disputed; refusing to compute a "
                    "binding deadline. If the summons states a last day to "
                    "answer, that date controls for the tenant.",
                ),
            ),
            citation=rule.citation_string,
        )

    # Service-method flags (risk discriminators, not date terms).
    if case.service_method is ServiceMethod.TACK_AND_MAIL:
        flags.append(Flag(FlagCode.TACK_AND_MAIL_REVIEW, rule.tack_and_mail_money_judgment_note))
        if (
            case.posting_date is not None
            and case.mailing_date is not None
            and case.posting_date != case.mailing_date
        ):
            flags.append(
                Flag(
                    FlagCode.TACK_AND_MAIL_DATE_SPLIT,
                    f"Posting date {case.posting_date.isoformat()} and mailing "
                    f"date {case.mailing_date.isoformat()} differ; the entered "
                    "service date is used for computation, and an attorney "
                    "must confirm which date starts the clock.",
                )
            )
    elif case.service_method is ServiceMethod.UNKNOWN:
        flags.append(
            Flag(
                FlagCode.UNKNOWN_SERVICE_METHOD,
                "Service method not captured at intake; method-specific risk "
                "(tack-and-mail default exposure) cannot be assessed.",
            )
        )

    if case.amended_affidavit:
        flags.append(
            Flag(
                FlagCode.AMENDED_AFFIDAVIT,
                "Second or amended dispossessory affidavit on file; the "
                "operative service event may have reset. Attorney must "
                "confirm which service date controls.",
            )
        )

    # Day count.
    if rule.counting_basis is not CountingBasis.DAY_OF_SERVICE_EXCLUDED:
        raise NotImplementedError("only day-of-service-excluded counting is implemented")
    trace.append(TraceStep(case.service_date, "day of actual service; not counted (day 0)"))
    candidate = case.service_date + timedelta(days=rule.window_length_days)
    trace.append(
        TraceStep(candidate, f"day {rule.window_length_days} (calendar days; intermediates count)")
    )

    computed: date | None
    if rule.terminal_roll is TerminalRoll.NEXT_NON_WEEKEND_NON_HOLIDAY:
        computed = _roll_terminal_day(rule, candidate, trace, flags)
    else:
        computed = candidate

    court_reopens: date | None = None
    if computed is not None:
        trace.append(TraceStep(computed, "last day to answer (statutory computation)"))
        if rule.calendar.is_court_closure(computed) and not rule.calendar.is_legal_holiday(
            computed
        ):
            court_reopens = _next_court_open_day(rule, computed)
            reopen_text = (
                f" The clerk's office next opens {court_reopens.isoformat()}."
                if court_reopens
                else ""
            )
            flags.append(
                Flag(
                    FlagCode.COURT_CLOSED_NOT_LEGAL_HOLIDAY,
                    f"Computed last day {computed.isoformat()} is a courthouse "
                    "closure that is NOT a Georgia legal holiday, so the "
                    "statute does not roll it forward, and absent a judicial "
                    "emergency order there is no automatic extension. An "
                    f"attorney must resolve this date.{reopen_text}",
                )
            )

    # Summons-stated date controls for the tenant when present.
    effective = computed
    if case.summons_stated_deadline is not None:
        effective = case.summons_stated_deadline
        if computed is not None and case.summons_stated_deadline != computed:
            flags.append(
                Flag(
                    FlagCode.SUMMONS_DATE_CONFLICT,
                    f"Summons states {case.summons_stated_deadline.isoformat()} "
                    f"but the statutory computation gives {computed.isoformat()}. "
                    "The summons-stated date controls for the tenant "
                    "(O.C.G.A. 44-7-51(b)); the discrepancy needs attorney review.",
                )
            )

    deadline_at: datetime | None = None
    if effective is not None:
        hour, minute = (int(p) for p in rule.deadline_time_of_day.split(":"))
        deadline_at = datetime.combine(
            effective, time(hour, minute), tzinfo=ZoneInfo(rule.deadline_timezone)
        )

    # O.C.G.A. 44-7-52 tender window, computed in parallel as advisory only.
    tender: date | None = None
    if rule.tender_window_days is not None:
        tender_candidate = case.service_date + timedelta(days=rule.tender_window_days)
        tender_flags: list[Flag] = []
        tender_trace: list[TraceStep] = []
        if rule.terminal_roll is TerminalRoll.NEXT_NON_WEEKEND_NON_HOLIDAY:
            tender = _roll_terminal_day(rule, tender_candidate, tender_trace, tender_flags)
        else:
            tender = tender_candidate

    return DeadlineResult(
        case_id=case.case_id,
        computed_deadline=computed,
        effective_deadline=effective,
        deadline_at=deadline_at,
        tender_deadline=tender,
        court_reopens_on=court_reopens,
        flags=tuple(flags),
        trace=tuple(trace),
        citation=rule.citation_string,
    )
