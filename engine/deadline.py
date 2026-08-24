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
    TACK_AND_MAIL_SERVICE_MISMATCH = "tack_and_mail_service_mismatch"
    AMENDED_AFFIDAVIT = "amended_affidavit"
    SUMMONS_DATE_CONFLICT = "summons_date_conflict"
    COURT_CLOSED_NOT_LEGAL_HOLIDAY = "court_closed_not_legal_holiday"
    STATE_HOLIDAY_COURT_OPEN = "state_holiday_court_open"
    CALENDAR_COVERAGE_GAP = "calendar_coverage_gap"
    JURISDICTION_UNSUPPORTED = "jurisdiction_unsupported"


class DeadlineBasis(Enum):
    """What the effective deadline rests on. Callers must never treat a
    SUMMONS_ONLY_UNVERIFIED date as a finalized statutory computation."""

    COMPUTED = "computed"  # statutory computation only; no summons date supplied
    SUMMONS_CONFIRMS = "summons_confirms"  # computation and summons agree
    SUMMONS_CONTROLS = "summons_controls"  # they conflict; summons controls for the tenant
    SUMMONS_ONLY_UNVERIFIED = "summons_only_unverified"  # computation refused; summons unverified
    NONE = "none"  # no deadline could be established at all


@dataclass(frozen=True)
class Flag:
    code: FlagCode
    reason: str
    # The specific calendar date a calendar-anomaly flag is about, so two
    # anomalies of the same kind on different dates are never conflated
    # (deduplication must be keyed on code AND date, not code alone).
    day: date | None = None


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

    def __post_init__(self) -> None:
        # Intake rows arrive from serialization; a raw string like
        # "tack_and_mail" would otherwise fall through every method-specific
        # safeguard and compute a clean-looking deadline with no flags.
        if not isinstance(self.service_method, ServiceMethod):
            raise TypeError(f"service_method must be a ServiceMethod, got {self.service_method!r}")
        for name, value in (
            ("service_date", self.service_date),
            ("posting_date", self.posting_date),
            ("mailing_date", self.mailing_date),
            ("summons_stated_deadline", self.summons_stated_deadline),
        ):
            # Exact date type: a datetime subclasses date but compares unequal,
            # which would silently defeat calendar membership checks.
            if value is not None and type(value) is not date:
                raise TypeError(f"{name} must be a datetime.date or None, got {value!r}")
        if type(self.amended_affidavit) is not bool:
            raise TypeError(f"amended_affidavit must be a bool, got {self.amended_affidavit!r}")


@dataclass(frozen=True)
class DeadlineResult:
    case_id: str
    computed_deadline: date | None
    effective_deadline: date | None
    deadline_at: datetime | None
    tender_deadline: date | None
    court_reopens_on: date | None
    flags: tuple[Flag, ...]
    deadline_basis: DeadlineBasis = DeadlineBasis.NONE
    trace: tuple[TraceStep, ...] = field(default=())
    citation: str = ""

    @property
    def needs_human_review(self) -> bool:
        return len(self.flags) > 0

    @property
    def computation_refused(self) -> bool:
        """True when the statutory computation could not be completed."""
        return self.computed_deadline is None

    @property
    def refused(self) -> bool:
        return self.computed_deadline is None and self.effective_deadline is None


def _is_weekend(day: date) -> bool:
    return day.weekday() >= 5


def _next_day(day: date) -> date | None:
    """Checked date advancement: None instead of OverflowError at date.max."""
    try:
        return day + timedelta(days=1)
    except OverflowError:
        return None


def _next_court_open_day(rule: JurisdictionRule, start: date) -> date | None:
    """First day after ``start`` that is a weekday and not a courthouse closure.

    Advisory only; used to tell the attorney when the clerk's office reopens.
    Returns None as soon as the closure table's coverage runs out: a date we
    have no closure data for must never be reported as an open day.
    """
    day: date | None = start
    for _ in range(30):
        day = _next_day(day) if day is not None else None
        if day is None or day > rule.calendar.closure_coverage_end:
            return None
        if not _is_weekend(day) and not rule.calendar.is_court_closure(day):
            return day
    return None


def _flag_effective_date_anomalies(
    rule: JurisdictionRule, effective: date, flags: list[Flag]
) -> date | None:
    """Calendar-divergence checks on the FINAL effective deadline.

    Runs after summons resolution on every path that produces an effective
    date, so a summons-stated December 31 (closed clerk) or April 3 (legal
    holiday, courthouse open) raises its specific hazard instead of hiding
    behind a generic conflict or missing-service flag. Returns the
    court-reopens advisory date when the clerk is closed on the effective day.

    Only checkable when the holiday calendar covers the date; closure
    coverage is guaranteed to reach at least that far by construction.
    """
    if not rule.calendar.holiday_knowledge_covers(effective):
        return None
    # Deduplicate by code AND date: rolling may already have flagged a
    # different holiday (April 3) while the controlling summons date lands
    # on another (October 12); both dates must stay visible.
    already_flagged = {(f.code, f.day) for f in flags}
    if rule.calendar.is_court_closure(effective) and not rule.calendar.is_legal_holiday(effective):
        court_reopens = _next_court_open_day(rule, effective)
        reopen_text = (
            f" The clerk's office next opens {court_reopens.isoformat()}." if court_reopens else ""
        )
        flags.append(
            Flag(
                FlagCode.COURT_CLOSED_NOT_LEGAL_HOLIDAY,
                f"Last day to answer {effective.isoformat()} is a courthouse "
                f"closure that is NOT a {rule.jurisdiction_label} legal "
                "holiday, so the statute does not roll it forward, and absent "
                "a judicial emergency order there is no automatic extension. "
                f"An attorney must resolve this date.{reopen_text}",
                day=effective,
            )
        )
        return court_reopens
    if (
        rule.calendar.is_legal_holiday(effective)
        and not rule.calendar.is_court_closure(effective)
        and (FlagCode.STATE_HOLIDAY_COURT_OPEN, effective) not in already_flagged
    ):
        flags.append(
            Flag(
                FlagCode.STATE_HOLIDAY_COURT_OPEN,
                f"Last day to answer {effective.isoformat()} is a "
                f"{rule.jurisdiction_label} legal holiday on which "
                f"{rule.court_label} is open. The statute treats it as a "
                "non-answer day and would roll past it; the calendars "
                "diverge here and an attorney must resolve which date "
                "controls.",
                day=effective,
            )
        )
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
        advanced: date | None
        if _is_weekend(day):
            trace.append(TraceStep(day, f"{day.strftime('%A')}; roll forward"))
            advanced = _next_day(day)
        elif rule.calendar.is_legal_holiday(day):
            if not rule.calendar.is_court_closure(day):
                flags.append(
                    Flag(
                        FlagCode.STATE_HOLIDAY_COURT_OPEN,
                        f"{day.isoformat()} is a {rule.jurisdiction_label} legal "
                        f"holiday, but {rule.court_label} is open that day. The "
                        "statute rolls the deadline past it"
                        # A claim about the summons's roll basis is a
                        # jurisdiction-specific fact; state it only when the
                        # rule row configures it.
                        + (f", while {rule.summons_roll_note}" if rule.summons_roll_note else "")
                        + "; the calendars diverge here and an attorney must "
                        "confirm the operative date.",
                        day=day,
                    )
                )
            trace.append(TraceStep(day, f"{rule.jurisdiction_label} legal holiday; roll forward"))
            advanced = _next_day(day)
        else:
            return day
        if advanced is None:
            # Rolling walked off the end of representable dates; refuse.
            flags.append(
                Flag(
                    FlagCode.CALENDAR_COVERAGE_GAP,
                    f"Terminal-day roll past {day.isoformat()} is not "
                    "arithmetically representable; refusing to finalize a "
                    "deadline.",
                )
            )
            trace.append(TraceStep(day, "date arithmetic limit reached; computation stopped"))
            return None
        day = advanced
    raise RuntimeError("terminal-day roll exceeded 15 days; calendar data is malformed")


def _flag_intake_risks(case: CaseInput, rule: JurisdictionRule, flags: list[Flag]) -> None:
    """Service-method and affidavit risk flags (discriminators, not dates).

    Runs on EVERY supported path, including the missing-service refusal: a
    tack-and-mail case with no service date still carries the default and
    money-judgment exposure the attorney must see. Only the service-date
    mismatch check requires a known service date.
    """
    if case.service_method is ServiceMethod.TACK_AND_MAIL:
        flags.append(Flag(FlagCode.TACK_AND_MAIL_REVIEW, rule.tack_and_mail_money_judgment_note))
        if (
            case.posting_date is not None
            and case.mailing_date is not None
            and case.posting_date != case.mailing_date
        ):
            # Outcome-neutral wording: this helper runs before the terminal
            # roll, which can still refuse (calendar coverage gap), so the
            # reason must never claim a finalized computation. It states the
            # starting point when a service date exists and states the
            # refusal when none does.
            consequence = (
                "the entered service date is the starting point for computation"
                if case.service_date is not None
                else "no deadline was computed because the service date is unconfirmed"
            )
            flags.append(
                Flag(
                    FlagCode.TACK_AND_MAIL_DATE_SPLIT,
                    f"Posting date {case.posting_date.isoformat()} and mailing "
                    f"date {case.mailing_date.isoformat()} differ; "
                    f"{consequence}, and an attorney "
                    "must confirm which date starts the clock.",
                )
            )
        else:
            # Component dates that agree with each other (or a single known
            # component) can still contradict the entered service date; a
            # mapping or intake error would otherwise move the deadline with
            # no warning at all.
            component = case.posting_date if case.posting_date is not None else case.mailing_date
            if (
                component is not None
                and case.service_date is not None
                and component != case.service_date
            ):
                flags.append(
                    Flag(
                        FlagCode.TACK_AND_MAIL_SERVICE_MISMATCH,
                        f"Tack-and-mail component date {component.isoformat()} "
                        "does not match the entered service date "
                        f"{case.service_date.isoformat()}. The entered service "
                        "date is the starting point for computation, and an "
                        "attorney must confirm which date starts the clock.",
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
                    f"Case jurisdiction {case.jurisdiction_id!r} does not "
                    f"match the supplied rule ({rule.jurisdiction_id!r}); "
                    "refusing to compute under a mismatched rule. Whether a "
                    "rule row exists for this jurisdiction is decided by the "
                    "dispatcher, not here.",
                ),
            ),
            # No legal citation on a rule mismatch: the supplied rule's
            # statutes are the wrong jurisdiction's authority and must not
            # contaminate an audit record or a generic result renderer.
            citation="",
        )

    if case.service_date is None:
        missing_flags: list[Flag] = [
            Flag(
                FlagCode.SERVICE_DATE_MISSING,
                "Service date unknown or disputed; refusing to compute a "
                "binding deadline. If the summons states a last day to "
                "answer, that date controls for the tenant.",
            )
        ]
        # Method and affidavit risks are independent of the refusal: a
        # tack-and-mail case with no service date still carries its
        # default-exposure warning.
        _flag_intake_risks(case, rule, missing_flags)
        # The effective-date hazard checks still apply to a summons-only
        # date: a summons stating a closed-clerk day must raise that
        # specific danger even when the computation is refused.
        missing_reopens: date | None = None
        if case.summons_stated_deadline is not None:
            missing_reopens = _flag_effective_date_anomalies(
                rule, case.summons_stated_deadline, missing_flags
            )
        return DeadlineResult(
            case_id=case.case_id,
            computed_deadline=None,
            effective_deadline=case.summons_stated_deadline,
            deadline_at=None,
            tender_deadline=None,
            court_reopens_on=missing_reopens,
            flags=tuple(missing_flags),
            deadline_basis=(
                DeadlineBasis.SUMMONS_ONLY_UNVERIFIED
                if case.summons_stated_deadline is not None
                else DeadlineBasis.NONE
            ),
            citation=rule.citation_string,
        )

    # Service-method and affidavit risk flags (shared with the refusal path).
    _flag_intake_risks(case, rule, flags)

    # Day count.
    if rule.counting_basis is not CountingBasis.DAY_OF_SERVICE_EXCLUDED:
        raise NotImplementedError("only day-of-service-excluded counting is implemented")
    trace.append(TraceStep(case.service_date, "day of actual service; not counted (day 0)"))
    # A date near date.max is a valid date to the type system but its window
    # arithmetic overflows; a malformed intake must refuse, never crash.
    try:
        candidate = case.service_date + timedelta(days=rule.window_length_days)
    except OverflowError:
        flags.append(
            Flag(
                FlagCode.CALENDAR_COVERAGE_GAP,
                f"Service date {case.service_date.isoformat()} is not "
                "arithmetically representable with the statutory window; "
                "refusing to compute. An attorney must confirm the intake "
                "record.",
            )
        )
        # The effective-date hazard checks still apply to a summons-only
        # date on this refusal path, exactly as on the missing-service path.
        overflow_reopens: date | None = None
        if case.summons_stated_deadline is not None:
            overflow_reopens = _flag_effective_date_anomalies(
                rule, case.summons_stated_deadline, flags
            )
        return DeadlineResult(
            case_id=case.case_id,
            computed_deadline=None,
            effective_deadline=case.summons_stated_deadline,
            deadline_at=None,
            tender_deadline=None,
            court_reopens_on=overflow_reopens,
            flags=tuple(flags),
            deadline_basis=(
                DeadlineBasis.SUMMONS_ONLY_UNVERIFIED
                if case.summons_stated_deadline is not None
                else DeadlineBasis.NONE
            ),
            trace=tuple(trace),
            citation=rule.citation_string,
        )
    trace.append(
        TraceStep(candidate, f"day {rule.window_length_days} (calendar days; intermediates count)")
    )

    # Exhaustive dispatch: an unknown terminal-roll value must never fall
    # open into "no roll", which would silently produce a weekend deadline.
    computed: date | None
    if rule.terminal_roll is TerminalRoll.NEXT_NON_WEEKEND_NON_HOLIDAY:
        computed = _roll_terminal_day(rule, candidate, trace, flags)
    elif rule.terminal_roll is TerminalRoll.NONE:
        computed = candidate
    else:
        raise TypeError(
            f"unsupported terminal_roll value {rule.terminal_roll!r}; "
            "refusing to compute a deadline under an unvalidated rule"
        )

    if computed is not None:
        trace.append(TraceStep(computed, "last day to answer (statutory computation)"))

    # Summons-stated date controls for the tenant when present. When the
    # computation was refused (coverage gap), the summons date is preserved
    # but explicitly labeled UNVERIFIED: it never launders a refusal into a
    # finalized-looking deadline.
    effective = computed
    basis = DeadlineBasis.COMPUTED if computed is not None else DeadlineBasis.NONE
    if case.summons_stated_deadline is not None:
        effective = case.summons_stated_deadline
        if computed is None:
            basis = DeadlineBasis.SUMMONS_ONLY_UNVERIFIED
        elif case.summons_stated_deadline == computed:
            basis = DeadlineBasis.SUMMONS_CONFIRMS
        else:
            basis = DeadlineBasis.SUMMONS_CONTROLS
            # Cite only the rule's own configured authority; a generic row
            # must never emit another jurisdiction's statute.
            authority = f" ({rule.summons_authority})" if rule.summons_authority else ""
            flags.append(
                Flag(
                    FlagCode.SUMMONS_DATE_CONFLICT,
                    f"Summons states {case.summons_stated_deadline.isoformat()} "
                    f"but the statutory computation gives {computed.isoformat()}. "
                    "The summons-stated date controls for the tenant"
                    f"{authority}; the discrepancy needs attorney review.",
                )
            )

    # Calendar-divergence checks run on the EFFECTIVE deadline, after summons
    # resolution, in both directions: a summons stating December 31 raises
    # the closed-clerk hazard, and one stating April 3 or October 12 raises
    # the holiday-but-court-open divergence, instead of either hiding behind
    # the generic conflict flag.
    court_reopens: date | None = None
    if effective is not None:
        court_reopens = _flag_effective_date_anomalies(rule, effective, flags)

    # Invariant: a precise timestamp exists ONLY when the statutory
    # computation completed. An unverified summons-only date keeps its
    # calendar date in effective_deadline but never gets a clock time a
    # scheduler could mistake for a finalized deadline; this matches the
    # missing-service-date path, so identical provenance states project
    # identically regardless of which refusal produced them.
    deadline_at: datetime | None = None
    if effective is not None and computed is not None:
        hour, minute = (int(p) for p in rule.deadline_time_of_day.split(":"))
        deadline_at = datetime.combine(
            effective, time(hour, minute), tzinfo=ZoneInfo(rule.deadline_timezone)
        )

    # O.C.G.A. 44-7-52 tender window, computed in parallel as advisory only.
    tender: date | None = None
    if rule.tender_window_days is not None:
        try:
            tender_candidate = case.service_date + timedelta(days=rule.tender_window_days)
        except OverflowError:
            tender_candidate = None  # advisory only; the main path already refused or flagged
        if tender_candidate is not None:
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
        deadline_basis=basis,
        trace=tuple(trace),
        citation=rule.citation_string,
    )
