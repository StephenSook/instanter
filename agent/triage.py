"""Deterministic discriminator-first triage ladder.

Modeled on validated emergency-department triage instruments (ESI/CTAS/MTS):
acuity and resource axes are kept separate, discriminators are independently
decisive, and contention rations attorney attention by re-ranking, never by
scoring. The design's own falsifier, from the research record: if this
module ever reduces to "days remaining <= N, escalate", the whole concept
reads as deadline arithmetic in an agent costume.

Structural commitments (each one is tested):
* No weighted sum exists anywhere; dispositions are discrete levels.
* Hard acuity gates set a FLOOR from deadline proximity and answer status.
* Each risk discriminator (tack-and-mail service, a calendar move that
  invalidates the tenant's own understanding of their deadline) raises the
  disposition one level ON ITS OWN.
* Contention never lowers a case's severity; it re-ranks: the top C by
  severity interrupt now, the remainder are explicitly HELD with the
  ranking stated, so the bar to interrupt rises with load.
* Undertriage (missing a real deadline) is the catastrophic error; the
  gates are liberal about eligibility and the capacity gate alone controls
  overtriage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from engine.deadline import DeadlineResult, FlagCode


class UrgencyLevel(Enum):
    """Four-level disposition ladder. Order matters: index = severity."""

    L0_HOLD = "hold"  # explicitly non-actionable (answer filed); never hidden
    L3_MONITOR = "monitor"
    L2_SURFACE_TODAY = "surface_today"
    L1_INTERRUPT = "interrupt"


_SEVERITY_ORDER = [
    UrgencyLevel.L0_HOLD,
    UrgencyLevel.L3_MONITOR,
    UrgencyLevel.L2_SURFACE_TODAY,
    UrgencyLevel.L1_INTERRUPT,
]

# Risk discriminators: flags that must be able to raise a disposition on
# their own, independent of days remaining. Tack-and-mail carries default
# exposure the tenant may never have seen; a calendar divergence means the
# tenant's own mental model of their deadline is likely wrong.
_SERVICE_RISK_FLAGS = frozenset(
    {
        FlagCode.TACK_AND_MAIL_REVIEW,
        FlagCode.TACK_AND_MAIL_DATE_SPLIT,
        FlagCode.TACK_AND_MAIL_SERVICE_MISMATCH,
    }
)
_CALENDAR_RISK_FLAGS = frozenset(
    {
        FlagCode.COURT_CLOSED_NOT_LEGAL_HOLIDAY,
        FlagCode.STATE_HOLIDAY_COURT_OPEN,
        FlagCode.SUMMONS_DATE_CONFLICT,
    }
)
# Flags that force human resolution because no reliable clock exists at all.
_NO_CLOCK_FLAGS = frozenset(
    {
        FlagCode.SERVICE_DATE_MISSING,
        FlagCode.CALENDAR_COVERAGE_GAP,
        FlagCode.JURISDICTION_UNSUPPORTED,
    }
)

# Hard acuity gates (calendar days from the run date to the effective
# deadline). Inclusive upper bounds.
_L1_PROXIMITY_DAYS = 1
_L2_PROXIMITY_DAYS = 3


@dataclass(frozen=True)
class TriageCase:
    """One case as the ladder sees it: engine output plus actionability."""

    case_id: str
    deadline: DeadlineResult
    answer_filed: bool = False
    # Model-estimated attorney effort in minutes; None until estimated.
    # Operational context only: it NEVER changes a disposition level, only
    # capacity accounting (a resource-axis input, per ESI).
    effort_minutes: int | None = None
    # Note-derived signals (validated model observations). Bounded inputs to
    # a fail-closed policy: any of them can FLOOR a case at L2 surface-today
    # (a human must look at it today), and none of them can create an L1
    # interrupt or lower anything. Interrupts stay derived exclusively from
    # verified intake fields; model perception summons human attention, it
    # never fires the interrupt itself.
    notes_present: bool = False
    observation_missing: bool = False  # notes exist but were never analyzed
    observed_service_by_posting: bool | None = None
    observed_answer_already_filed: bool | None = None
    observed_hearing_or_deadline_change: bool | None = None
    observed_possible_defective_service: bool | None = None
    observation_needs_confirmation: bool = False
    # Independent uncertainty floors (defense in depth beyond the model's
    # own cross-field validator): open questions or a low-confidence
    # extraction each floor at L2 on their own, whatever the model claimed
    # about needing confirmation.
    observation_has_ambiguities: bool = False
    observation_low_confidence: bool = False


@dataclass(frozen=True)
class TriageDecision:
    case_id: str
    level: UrgencyLevel
    floor_level: UrgencyLevel
    raised_by: tuple[str, ...]  # discriminator names, facts not numbers
    days_remaining: int | None
    rank: int  # 1 = most severe in this run
    interrupt_now: bool
    held_reason: str | None  # set when capacity demoted an L1 candidate
    factors: tuple[str, ...] = field(default=())  # rationale facts, in order


def _raise_level(level: UrgencyLevel, steps: int = 1) -> UrgencyLevel:
    index = min(_SEVERITY_ORDER.index(level) + steps, len(_SEVERITY_ORDER) - 1)
    # L0 never rises through discriminators: a filed answer is not urgent.
    return _SEVERITY_ORDER[index]


def _note_floor(
    level: UrgencyLevel,
    raised_by: list[str],
    factors: list[str],
    reason: str,
    factor: str,
) -> UrgencyLevel:
    """Floor a case at L2 surface-today on a note-derived signal. Records
    the reason only when the level actually moved; the factor always."""
    if _SEVERITY_ORDER.index(level) < _SEVERITY_ORDER.index(UrgencyLevel.L2_SURFACE_TODAY):
        level = UrgencyLevel.L2_SURFACE_TODAY
        raised_by.append(reason)
    factors.append(factor)
    return level


def _severity_key(decision_input: tuple[UrgencyLevel, int | None, int, str]) -> tuple[int, ...]:
    """Sort key: higher severity first, then fewer days, then more
    discriminators. Deterministic tie-break on case id (stable, auditable)."""
    level, days, discriminator_count, case_id = decision_input
    days_component = days if days is not None else -1  # unknown clock sorts first
    return (
        -_SEVERITY_ORDER.index(level),
        days_component,
        -discriminator_count,
        # Stable lexical tie-break; not a score, an ordering guarantee.
        *(ord(c) for c in case_id),
    )


def triage_queue(
    cases: list[TriageCase],
    run_date: date,
    attorney_capacity: int,
) -> list[TriageDecision]:
    """Rank the whole queue and ration interrupts by attorney capacity.

    Returns decisions in severity order (rank 1 first). Exactly
    ``min(attorney_capacity, eligible L1 cases)`` decisions carry
    ``interrupt_now=True``; every demoted L1 candidate carries an explicit
    ``held_reason`` naming the capacity and its rank, because escalating
    everything equals escalating nothing, and silent demotion would hide
    the rationing.
    """
    if attorney_capacity < 0:
        raise ValueError(f"attorney_capacity must be >= 0, got {attorney_capacity}")

    staged: list[
        tuple[TriageCase, UrgencyLevel, UrgencyLevel, list[str], int | None, list[str]]
    ] = []

    for case in cases:
        factors: list[str] = []
        raised_by: list[str] = []
        flag_codes = {f.code for f in case.deadline.flags}

        # Explicit hold: a filed answer removes the default-writ exposure.
        if case.answer_filed:
            factors.append("answer already filed; no default exposure on this docket entry")
            staged.append((case, UrgencyLevel.L0_HOLD, UrgencyLevel.L0_HOLD, [], None, factors))
            continue

        days_remaining: int | None = None
        if case.deadline.effective_deadline is not None:
            days_remaining = (case.deadline.effective_deadline - run_date).days

        # Acuity floor from the deterministic clock.
        if days_remaining is None:
            # No reliable clock at all: a human must establish the facts
            # today; monitoring nothing is not a disposition.
            floor = UrgencyLevel.L2_SURFACE_TODAY
            factors.append(
                "no reliable deadline could be established; attorney must resolve intake"
            )
        elif days_remaining <= _L1_PROXIMITY_DAYS:
            floor = UrgencyLevel.L1_INTERRUPT
            factors.append(
                f"effective deadline {case.deadline.effective_deadline} is "
                f"{days_remaining} day(s) away"
            )
        elif days_remaining <= _L2_PROXIMITY_DAYS:
            floor = UrgencyLevel.L2_SURFACE_TODAY
            factors.append(
                f"effective deadline {case.deadline.effective_deadline} is "
                f"{days_remaining} day(s) away"
            )
        else:
            floor = UrgencyLevel.L3_MONITOR
            factors.append(
                f"effective deadline {case.deadline.effective_deadline} is "
                f"{days_remaining} day(s) away"
            )

        level = floor

        # Risk discriminators: each independently decisive, each named.
        if flag_codes & _SERVICE_RISK_FLAGS:
            level = _raise_level(level)
            raised_by.append("service_method_risk")
            factors.append(
                "tack-and-mail service risk: default possession exposure even "
                "without actual notice (O.C.G.A. 44-7-51(c) family flags present)"
            )
        if flag_codes & _CALENDAR_RISK_FLAGS:
            level = _raise_level(level)
            raised_by.append("calendar_move_risk")
            factors.append(
                "a calendar divergence or summons conflict likely invalidates "
                "the tenant's own understanding of the deadline"
            )
        if flag_codes & _NO_CLOCK_FLAGS and days_remaining is not None:
            # Clock exists (summons-only, say) but its basis needs a human.
            level = _raise_level(level)
            raised_by.append("unverified_clock")
            factors.append("the deadline basis is unverified; attorney confirmation required")

        # Note-derived signal policy (fail closed, bounded): each signal can
        # floor the case at L2 surface-today so a human looks at it TODAY,
        # and nothing here can mint an L1 interrupt or lower a level.
        if case.observation_missing:
            level = _note_floor(
                level,
                raised_by,
                factors,
                "unanalyzed_notes",
                "intake notes exist but were never analyzed; a human must read "
                "them today (unread notes may hide urgency)",
            )
        if case.observed_possible_defective_service:
            level = _note_floor(
                level,
                raised_by,
                factors,
                "defective_service_mention",
                "notes report possibly defective service; staff must confirm today",
            )
        if case.observed_hearing_or_deadline_change:
            level = _note_floor(
                level,
                raised_by,
                factors,
                "deadline_change_mention",
                "notes report a hearing or deadline change; the tenant's "
                "understanding of the clock may be wrong",
            )
        if case.observed_service_by_posting and not (flag_codes & _SERVICE_RISK_FLAGS):
            # The intake fields did NOT record tack-and-mail but the notes
            # describe it: the recorded service method may be wrong.
            level = _note_floor(
                level,
                raised_by,
                factors,
                "posting_service_mention",
                "notes describe posted service the intake fields do not "
                "record; the service method needs staff confirmation",
            )
        if case.observation_needs_confirmation:
            level = _note_floor(
                level,
                raised_by,
                factors,
                "needs_human_confirmation",
                "the notes analysis flagged uncertain or conflicting facts for human confirmation",
            )
        if case.observation_has_ambiguities:
            level = _note_floor(
                level,
                raised_by,
                factors,
                "open_questions",
                "the notes analysis recorded open questions a staff member must answer",
            )
        if case.observation_low_confidence:
            level = _note_floor(
                level,
                raised_by,
                factors,
                "low_confidence_extraction",
                "the notes extraction is low confidence; a human must read the notes directly",
            )
        if case.observed_answer_already_filed and not case.answer_filed:
            # Fail-closed direction: a mentioned-but-unconfirmed filed answer
            # never LOWERS urgency; it is recorded for the reviewing human.
            factors.append(
                "notes mention an answer may already be filed; staff should "
                "confirm the docket before further action"
            )

        staged.append((case, level, floor, raised_by, days_remaining, factors))

    # Contention: rank by severity, then ration interrupts to capacity.
    staged.sort(key=lambda item: _severity_key((item[1], item[4], len(item[3]), item[0].case_id)))

    decisions: list[TriageDecision] = []
    interrupts_granted = 0
    for rank, (case, level, floor, raised_by, days_remaining, factors) in enumerate(
        staged, start=1
    ):
        interrupt_now = False
        held_reason: str | None = None
        final_level = level
        if level is UrgencyLevel.L1_INTERRUPT:
            if interrupts_granted < attorney_capacity:
                interrupt_now = True
                interrupts_granted += 1
            else:
                # Re-rank, never re-score: the case keeps its severity facts
                # but is explicitly held, with the rationing stated.
                final_level = UrgencyLevel.L2_SURFACE_TODAY
                held_reason = (
                    f"held: attorney capacity is {attorney_capacity} this run and "
                    f"{interrupts_granted} more-severe case(s) hold the slots; "
                    f"ranked {rank} overall"
                )
        decisions.append(
            TriageDecision(
                case_id=case.case_id,
                level=final_level,
                floor_level=floor,
                raised_by=tuple(raised_by),
                days_remaining=days_remaining,
                rank=rank,
                interrupt_now=interrupt_now,
                held_reason=held_reason,
                factors=tuple(factors),
            )
        )
    return decisions
