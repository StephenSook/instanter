"""Instanter deterministic deadline engine.

Pure Python, zero AWS dependencies, fully unit-tested. No model ever touches
this package: statutory deadline math is deterministic by design.
"""

from engine.deadline import (
    CaseInput,
    DeadlineBasis,
    DeadlineResult,
    Flag,
    FlagCode,
    compute_deadline,
)
from engine.holidays import GEORGIA_2026_CALENDAR, HolidayCalendar
from engine.rules import GEORGIA_RULE, RULES, JurisdictionRule, ServiceMethod

__all__ = [
    "GEORGIA_2026_CALENDAR",
    "GEORGIA_RULE",
    "RULES",
    "CaseInput",
    "DeadlineBasis",
    "DeadlineResult",
    "Flag",
    "FlagCode",
    "HolidayCalendar",
    "JurisdictionRule",
    "ServiceMethod",
    "compute_deadline",
]
