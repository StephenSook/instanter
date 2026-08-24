"""Contract tests for the structured-output models: the UPL boundary lives
in validation, so a generation that drifts into advice must FAIL, and the
failure routes to retry-or-human, never delivery."""

import pytest
from pydantic import ValidationError

from agent.models import EffortEstimate, EscalationRationale, ExtractedObservations


def valid_observations(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "summary": "Tenant reports papers were taped to the door last Tuesday.",
        "mentions_service_by_posting": True,
        "needs_human_confirmation": True,
        "confidence": 0.7,
        "ambiguities": ["Confirm whether a mailed copy also arrived."],
    }
    base.update(overrides)
    return base


def test_observations_accept_honest_uncertainty() -> None:
    obs = ExtractedObservations(**valid_observations())  # type: ignore[arg-type]
    assert obs.needs_human_confirmation
    assert obs.mentions_service_by_posting is True


@pytest.mark.parametrize(
    "poisoned_summary",
    [
        "You should file an answer immediately.",
        "We recommend raising the tender defense.",
        "The tenant should assert the defense of defective service.",
    ],
)
def test_observations_reject_advice_language(poisoned_summary: str) -> None:
    with pytest.raises(ValidationError, match="advice language"):
        ExtractedObservations(**valid_observations(summary=poisoned_summary))  # type: ignore[arg-type]


def test_observations_reject_advice_hidden_in_ambiguities() -> None:
    with pytest.raises(ValidationError, match="advice language"):
        ExtractedObservations(
            **valid_observations(ambiguities=["Ask if they want legal advice on defenses."])  # type: ignore[arg-type]
        )


def test_effort_estimate_bounds() -> None:
    assert (
        EffortEstimate(minutes=45, basis="tack-and-mail intake with conflicting dates").minutes
        == 45
    )
    with pytest.raises(ValidationError):
        EffortEstimate(minutes=4, basis="too small")
    with pytest.raises(ValidationError):
        EffortEstimate(minutes=500, basis="too large")


def valid_rationale(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "case_id": "26ED000101",
        "disposition": "interrupt",
        "contributing_factors": [
            "effective deadline 2026-08-13 is 1 day away",
            "tack-and-mail service risk present",
        ],
        "rationale": (
            "This case outranks the queue because the answer deadline is one "
            "day away and service was by posting, which permits a default "
            "possession judgment even without actual notice."
        ),
        "confidence": 0.85,
    }
    base.update(overrides)
    return base


def test_rationale_accepts_fact_based_explanation() -> None:
    r = EscalationRationale(**valid_rationale())  # type: ignore[arg-type]
    assert r.disposition == "interrupt"


def test_rationale_rejects_advice() -> None:
    with pytest.raises(ValidationError, match="advice language"):
        EscalationRationale(
            **valid_rationale(rationale="You should file the answer today and raise the defense.")  # type: ignore[arg-type]
        )


def test_rationale_rejects_bare_urgency_score() -> None:
    with pytest.raises(ValidationError, match="urgency score"):
        EscalationRationale(
            **valid_rationale(rationale="Urgency score 0.91 justifies escalation. Urgency: 0.91.")  # type: ignore[arg-type]
        )


def test_rationale_requires_at_least_one_factor() -> None:
    with pytest.raises(ValidationError):
        EscalationRationale(**valid_rationale(contributing_factors=[]))  # type: ignore[arg-type]
