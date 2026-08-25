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


# --- UPL floor hardening (the adversarial-review payloads) --------------------


ADVICE_PAYLOADS = [
    "File an answer today and claim defective service.",  # the review's exact probe
    "You must respond by Friday to avoid default.",
    "You need to appear at the hearing.",
    "Pay the rent into the court registry before the hearing",
    "Appeal the judgment immediately",
    "The facts here argue tender as a complete defense",
    "FILE AN ANSWER TODAY",  # casing
    "Ｆile an answer today",  # noqa: RUF001 (deliberate fullwidth lookalike)
    "The tenant must contest the amount",
    "The tenant needs to respond this week",
    # Round-7 reproducers: polite prefix, leading bullet, Cyrillic homoglyph
    "Please file an answer today",
    "- File an answer today",
    "Yоu should file an answer today",  # noqa: RUF001 (Cyrillic small o homoglyph)
    "Kindly pay the rent into the registry",
    "Тhe tenant should respond",  # noqa: RUF001 (Cyrillic capital Te homoglyph)
    # Round-8 reproducers: format char, combining mark, leet, spaced letters,
    # control char
    "F\u200dile an answer today",  # zero-width joiner inside the verb
    "Fi\u0301le an answer today",  # combining acute over the i
    "F1le an answer today",  # digit-for-letter substitution
    "F i l e an answer today",  # spaced-out letters
    "The deadline\x00 passed with no answer.",  # raw control character
    # Round-9 reproducers: composed evasions (spacing + leet, punctuation)
    "F 1 l e an answer today",
    "Y 0 u should file an answer today",
    "F.i.l.e an answer today",
    # Round-10 reproducers: multi-character separators
    "Please f. i. l. e an answer today.",
    "F.-1-l-e an answer today.",
]


@pytest.mark.parametrize("payload", ADVICE_PAYLOADS)
def test_hardened_floor_rejects_imperative_advice(payload: str) -> None:
    with pytest.raises(ValidationError):
        EscalationRationale(
            case_id="X",
            disposition="interrupt",
            contributing_factors=["deadline overdue"],
            rationale=payload,
            confidence=0.9,
        )


FACTUAL_PAYLOADS = [
    "Deadline passed with no answer on file; writ exposure is live.",
    "The effective deadline is 2026-09-09, 0 day(s) away; rank 1 of 2.",
    "Notes report possibly defective service; staff must confirm today.",
    "An answer was filed on the docket; no default exposure remains.",
    "Tenant plans to pay everything owed this week per the notes.",
    "Service was by tack and mail; the mailing date is unverified.",
    "[MODEL DISABLED: templated rationale] This case ranks 1 this run.",
    # Non-legal imperatives and legal terms in factual positions stay valid
    "Please review the intake facts with the tenant.",
    "Pay-or-quit notice dates are recorded in the intake notes.",
]


@pytest.mark.parametrize("payload", FACTUAL_PAYLOADS)
def test_hardened_floor_passes_factual_statements(payload: str) -> None:
    rationale = EscalationRationale(
        case_id="X",
        disposition="interrupt",
        contributing_factors=["deadline overdue"],
        rationale=payload,
        confidence=0.9,
    )
    assert rationale.rationale == payload


# --- Cross-field uncertainty invariants ---------------------------------------


def test_ambiguities_without_confirmation_is_rejected() -> None:
    with pytest.raises(ValidationError, match="needs_human_confirmation"):
        ExtractedObservations(
            summary="The hearing date in the notes is uncertain.",
            ambiguities=["Which hearing date is correct?"],
            needs_human_confirmation=False,
            confidence=0.9,
        )


def test_low_confidence_without_confirmation_is_rejected() -> None:
    with pytest.raises(ValidationError, match="low-confidence"):
        ExtractedObservations(
            summary="Notes are hard to interpret.",
            needs_human_confirmation=False,
            confidence=0.1,
        )


def test_uncertainty_with_confirmation_is_accepted() -> None:
    obs = ExtractedObservations(
        summary="The hearing date in the notes is uncertain.",
        ambiguities=["Which hearing date is correct?"],
        needs_human_confirmation=True,
        confidence=0.4,
    )
    assert obs.needs_human_confirmation


# --- Round-12: model-authored quantities in words, ambiguity discipline -------


def test_ambiguities_reject_digits_and_number_words() -> None:
    for poisoned in (
        ["Could the deadline instead be 2099-12-31 with 999 days remaining?"],
        ["Could the deadline instead be December thirty first?"],
        ["Is the rank nine hundred ninety nine?"],
    ):
        with pytest.raises(ValidationError):
            ExtractedObservations(
                summary="Notes are unclear about the deadline.",
                ambiguities=poisoned,
                needs_human_confirmation=True,
                confidence=0.8,
            )


def test_ambiguities_reject_oversized_entries() -> None:
    with pytest.raises(ValidationError, match="160"):
        ExtractedObservations(
            summary="Notes are unclear.",
            ambiguities=["Confirm whether " + "the tenant record matches " * 20 + "?"],
            needs_human_confirmation=True,
            confidence=0.8,
        )


def test_ambiguities_accept_digitless_open_questions() -> None:
    obs = ExtractedObservations(
        summary="Notes mention conflicting hearing dates.",
        ambiguities=["Which hearing date is correct?"],
        needs_human_confirmation=True,
        confidence=0.8,
    )
    assert obs.ambiguities


def test_numeric_floor_scans_canonical_shadows() -> None:
    """Round-13 reproducer: 'nïne days' beat the literal wordlist because
    numeric matching skipped the diacritic and separator shadows the
    advice floor already scans."""
    from agent.models import reject_model_numerics

    for poisoned in (
        "The deadline is nïne days away.",
        "The deadline is n i n e days away.",
        "The deadline is n.i.n.e days away.",
        "Due in Dеcember.",  # noqa: RUF001 (Cyrillic small ie homoglyph)
    ):
        with pytest.raises(ValueError):
            reject_model_numerics(poisoned, "explanation")
    assert reject_model_numerics("The deadline has passed.", "explanation")


def test_numeric_floor_rejects_partial_separator_insertion() -> None:
    """Round-14 reproducer: 'n-ine days' kept multi-character fragments the
    single-letter collapse never joined."""
    from agent.models import reject_model_numerics

    for poisoned in ("n-ine days remain", "tw-enty days", "sep-tember hearing"):
        with pytest.raises(ValueError):
            reject_model_numerics(poisoned, "explanation")
    assert reject_model_numerics("The deadline has passed.", "explanation")


def test_advice_floor_rejects_partial_separator_imperatives() -> None:
    for poisoned in ("F-ile an answer today", "P-ay the rent into the registry"):
        with pytest.raises(ValidationError):
            EscalationRationale(
                case_id="X",
                disposition="interrupt",
                contributing_factors=["deadline overdue"],
                rationale=poisoned,
                confidence=0.9,
            )


def test_numeric_floor_rejects_nonascii_numerals_and_compounds() -> None:
    """Round-15 reproducers (hunter): isdigit misses vulgar fractions and
    Roman numeral characters, the digit gate ran pre-NFKC only, and joined
    compounds and month abbreviations were absent from the wordlist."""
    from agent.models import reject_model_numerics

    for poisoned in (
        "the deadline is in ½ a day",
        "deadline in Ⅻ days",
        "in twentyfive days the writ issues",
        "by early Dec the matter closes",
        "the fortieth day approaches",
        "sixtytwo days remain",
    ):
        with pytest.raises(ValueError):
            reject_model_numerics(poisoned, "notes")
    # 'tenant' must never trip the compound segmentation (ten + ant).
    assert reject_model_numerics("The tenant reports the notice arrived.", "notes")


def test_numeric_floor_rejects_quantity_and_relative_date_words() -> None:
    """Round-16 reproducers: collective quantities and relative or named
    dates fabricate figures exactly as digits do."""
    from agent.models import reject_model_numerics

    for poisoned in (
        "the tenant has a dozen days to respond",
        "the hearing is a fortnight away",
        "the deadline is tomorrow",
        "half the window has elapsed",
        "answer due by May third" if False else "due by May",
    ):
        with pytest.raises(ValueError):
            reject_model_numerics(poisoned, "notes")


def test_modal_may_is_legitimate_hedge_language() -> None:
    """Round-16: rejecting lowercase modal 'may' starved the models of
    their most natural hedge; only the capitalized month form rejects."""
    from agent.models import reject_model_numerics

    assert reject_model_numerics("the tenant may have moved", "notes")
    assert reject_model_numerics("service may be defective", "notes")
    with pytest.raises(ValueError):
        reject_model_numerics("The answer is due by May.", "notes")


def test_month_may_rejected_in_every_casing_modal_stays_legal() -> None:
    """Round-17 reproducer: the month ban was casing-exact, so 'DUE BY
    MAY' and 'due by may' delivered fabricated date words while the
    sentence-initial modal 'May service have been defective' was a false
    positive. Month recognition is by date context on casefolded shadows."""
    from agent.models import reject_model_numerics

    for poisoned in (
        "THE HEARING IS IN MAY.",
        "the filing is due by may.",
        "rent was withheld since may",
        "the notice arrived in mAy",
        "expected in mid-May",
    ):
        with pytest.raises(ValueError):
            reject_model_numerics(poisoned, "notes")
    assert reject_model_numerics("the tenant may have moved", "notes")
    assert reject_model_numerics("May service have been defective is unclear", "notes")
    assert reject_model_numerics("service may be defective", "notes")
    # Round-17 LOW: system-vocabulary words no longer starve the writers.
    assert reject_model_numerics("the ladder does not use a score; it ranks", "notes")
    assert reject_model_numerics("on the eve of the hearing the notes were read", "notes")


def test_may_context_covers_attributive_and_month_first_forms() -> None:
    """Round-18 reproducers: attributive and month-first May phrasings
    dodged the preposition-only pattern, while common modal hedge frames
    ('this may reflect', 'by may already be') were false positives."""
    from agent.models import reject_model_numerics

    for poisoned in (
        "the may hearing needs staff confirmation",
        "the may deadline is close",
        "come may the writ will issue",
        "may arrives before the hearing",
        "due by May",
        "the answer is due by christmas",
        "the deadline falls on thanksgiving",
    ):
        with pytest.raises(ValueError):
            reject_model_numerics(poisoned, "notes")
    for legal in (
        "the notes suggest this may reflect defective service",
        "the recorded method for this may need confirmation",
        "whether the answer described by may already be on file",
        "service may be defective",
        "the tenant plans to resolve arrears this week",
    ):
        assert reject_model_numerics(legal, "notes")


def test_may_context_round19_dodges_and_hedges() -> None:
    """Round-19: for/to/into/through prepositions, plural and article
    attributives, and possessives dodged the month pattern; relative-clause
    hedges ('complained of may invalidate') false-positived."""
    from agent.models import reject_model_numerics

    for poisoned in (
        "The hearing is set for May.",
        "The case was reset to May.",
        "carried over into May.",
        "The stay runs through May.",
        "The May hearings will resolve the docket.",
        "A May hearing is expected.",
        "May's docket includes this case.",
    ):
        with pytest.raises(ValueError):
            reject_model_numerics(poisoned, "notes")
    for legal in (
        "The defect complained of may invalidate the service.",
        "The conduct complained of may amount to improper notice.",
        "The irregularity complained of may constitute defective service.",
        "The problem complained of may well be defective service.",
        "The posting spoken of may or might not have occurred.",
        # Round-19 wordlist rebalance: street names, agency names, and
        # honest weekend prose are legal again.
        "confirm which unit on Memorial Drive the posted notice refers to",
        "confirm whether the tenant receives Veterans Affairs housing assistance",
        "the courthouse was closed over the weekend when the tenant tried to file",
        "the tenant asserts independence from the co-signer",
    ):
        assert reject_model_numerics(legal, "notes")


def test_may_month_uses_closed_class_lookahead() -> None:
    """Round-20: the modal false-positive family survives any finite verb
    list (English verbs are open class), so the rule is inverted: after a
    preposition, 'may' is the MONTH only when a sentence boundary or a
    closed-class token follows."""
    from agent.models import reject_model_numerics

    for month in (
        "the docket shows a May continuance",
        "staff noted a May reset of the hearing",
        "the tenant's May hearing was moved",
        "this May setting conflicts with the notice",
        "continued till May",
        "slid toward May",
        "lands on May",
        "in May the hearing was reset",
        "due by May.",
    ):
        with pytest.raises(ValueError):
            reject_model_numerics(month, "notes")
    for modal in (
        "the person the notice was handed to may reside elsewhere",
        "the relief the tenant is entitled to may include possession",
        "the docket the filing belongs to may list a hearing",
        "the amount the ledger points to may consist of late fees",
        "the defect complained of may invalidate the service",
        "the problem complained of may well be defective service",
        "the posting spoken of may or might not have occurred",
        "service may be defective",
        "this may reflect a recording error",
    ):
        assert reject_model_numerics(modal, "notes")
