"""Structured-output contracts for the model's two legitimate jobs.

Perceive: read free-text intake notes into typed observations a human can
verify (and the deterministic ladder can consume). Communicate: explain an
escalation in operative facts. Neither output may contain legal advice, and
the boundary is enforced HERE, in validation, not in prompt hope: a
generation that drifts into advice fails validation and is retried or
routed to a human, never delivered.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

# Advice-language boundary. Deliberately small and literal: the Strands
# OutputEvaluator scores rationale quality more broadly offline; this
# validator is the hard runtime floor. Every phrase is lowercase; matching
# is case-insensitive substring.
_ADVICE_PHRASES: tuple[str, ...] = (
    "you should",
    "we recommend",
    "i recommend",
    "best defense",
    "strongest defense",
    "raise the defense",
    "assert the defense",
    "file a counterclaim",
    "you have a strong case",
    "legal advice",
    "advise the tenant",
    "the tenant should",
)


def _reject_advice_language(text: str, field_name: str) -> str:
    lowered = text.lower()
    for phrase in _ADVICE_PHRASES:
        if phrase in lowered:
            raise ValueError(
                f"{field_name} contains advice language ({phrase!r}); this "
                "system states facts for a licensed attorney and never advises"
            )
    return text


class ExtractedObservations(BaseModel):
    """Typed observations pulled from free-text intake notes.

    Observations are inputs to the deterministic ladder and statements for
    a human reviewer; they are never legal conclusions. Anything the model
    is unsure about belongs in ``ambiguities`` with
    ``needs_human_confirmation=True``, never guessed.
    """

    summary: str = Field(min_length=1, max_length=600)
    mentions_service_by_posting: bool | None = Field(
        default=None,
        description="Notes suggest tack-and-mail/posted service. None = not mentioned.",
    )
    mentions_answer_already_filed: bool | None = None
    mentions_hearing_or_deadline_change: bool | None = None
    mentions_possible_defective_service: bool | None = None
    ambiguities: list[str] = Field(
        default_factory=list,
        description="Material open questions a staff member must confirm.",
        max_length=10,
    )
    needs_human_confirmation: bool = Field(
        description="True whenever any extracted signal is uncertain or conflicting."
    )
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("summary")
    @classmethod
    def _summary_is_not_advice(cls, value: str) -> str:
        return _reject_advice_language(value, "summary")

    @field_validator("ambiguities")
    @classmethod
    def _ambiguities_are_not_advice(cls, value: list[str]) -> list[str]:
        for item in value:
            _reject_advice_language(item, "ambiguities")
        return value


class EffortEstimate(BaseModel):
    """Operational estimate of attorney minutes this case likely needs.

    Resource-axis context only: the deterministic ladder never lets this
    change a disposition level (bounded, and the threshold sits downstream).
    """

    minutes: int = Field(ge=5, le=240)
    basis: str = Field(min_length=1, max_length=300)

    @field_validator("basis")
    @classmethod
    def _basis_is_not_advice(cls, value: str) -> str:
        return _reject_advice_language(value, "basis")


class EscalationRationale(BaseModel):
    """The communicate-layer output: why THIS case, and not the others.

    The disposition itself is decided by the deterministic ladder; the
    model explains it. ``disposition`` must echo the ladder's decision
    (verified by the caller against the TriageDecision), and
    ``contributing_factors`` must restate the deterministic factors, so the
    explanation is faithful rather than decorative.
    """

    case_id: str = Field(min_length=1, max_length=64)
    disposition: str = Field(description="Echo of the ladder's level value, e.g. 'interrupt'.")
    contributing_factors: list[str] = Field(min_length=1, max_length=8)
    rationale: str = Field(min_length=1, max_length=900)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("rationale")
    @classmethod
    def _rationale_is_not_advice(cls, value: str) -> str:
        return _reject_advice_language(value, "rationale")

    @field_validator("contributing_factors")
    @classmethod
    def _factors_are_not_advice(cls, value: list[str]) -> list[str]:
        for item in value:
            _reject_advice_language(item, "contributing_factors")
        return value

    @field_validator("rationale")
    @classmethod
    def _rationale_carries_facts_not_scores(cls, value: str) -> str:
        # A bare urgency number in place of facts is the research's fourth
        # collapse condition. Cheap structural check: forbid "urgency" being
        # presented as a numeric score.
        lowered = value.lower()
        if "urgency score" in lowered or "urgency: 0." in lowered:
            raise ValueError("rationale must state operative facts, never a bare urgency score")
        return value
