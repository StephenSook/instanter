"""OCR transcribes a summons; the engine computes the deadline."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

DOOR = Path(__file__).parent.parent / "infra" / "door"
if str(DOOR) not in sys.path:
    sys.path.insert(0, str(DOOR))

from ocr import deadline_from_extract  # noqa: E402

from engine.deadline import CaseInput, compute_deadline  # noqa: E402
from engine.rules import GEORGIA_RULE, ServiceMethod  # noqa: E402


def test_ocr_deadline_is_the_engine_not_the_model() -> None:
    extracted = {
        "service_date": "2026-08-08",
        "service_method": "personal",
        "summons_stated_deadline": None,
        "case_id": "EXAMPLE-1",
        "refused": False,
    }
    out = deadline_from_extract(extracted)
    engine = compute_deadline(
        CaseInput(
            case_id="EXAMPLE-1",
            jurisdiction_id="GA-FULTON",
            service_date=date(2026, 8, 8),
            service_method=ServiceMethod.PERSONAL,
        ),
        GEORGIA_RULE,
    )
    assert engine.computed_deadline is not None
    assert out["computed_deadline"] == engine.computed_deadline.isoformat() == "2026-08-17"
    assert out["extracted"]["service_date"] == "2026-08-08"
    assert "deadline" not in out["extracted"]


def test_ocr_refuses_when_the_model_guesses_no_date() -> None:
    out = deadline_from_extract({"refused": True, "reason": "not a summons"})
    assert out["error"] == "summons_unreadable"
    assert "computed_deadline" not in out


def test_ocr_refuses_a_missing_service_date() -> None:
    out = deadline_from_extract(
        {"service_date": None, "service_method": "personal", "refused": False}
    )
    assert out["error"] == "service_date_missing"


def test_ocr_example_watermark_does_not_block_a_transcribed_date() -> None:
    out = deadline_from_extract(
        {
            "service_date": "2026-08-08",
            "service_method": "personal",
            "refused": True,
            "reason": "EXAMPLE DATA",
            "refusal_code": "example_watermark",
        }
    )
    assert out["computed_deadline"] == "2026-08-17"
    assert "error" not in out


def test_ocr_a_semantic_refusal_refuses_even_when_a_date_was_transcribed() -> None:
    """A rent demand, a ledger, or any dated page the model rejects must not
    become a confident summons deadline. Only the watermark refusal (the
    spurious one about our own EXAMPLE DATA label) may be overridden."""
    out = deadline_from_extract(
        {
            "service_date": "2026-08-08",
            "service_method": "personal",
            "refused": True,
            "reason": "this looks like a rent demand notice",
        }
    )
    assert out["error"] == "model_refused"
    assert "computed_deadline" not in out
    assert "rent demand" in out["detail"]


def test_ocr_a_watermark_refusal_computes_and_carries_the_reason() -> None:
    out = deadline_from_extract(
        {
            "service_date": "2026-08-08",
            "service_method": "personal",
            "refused": True,
            "reason": "the page carries an EXAMPLE DATA watermark",
            "refusal_code": "example_watermark",
        }
    )
    assert out["computed_deadline"] == "2026-08-17"
    assert "EXAMPLE DATA" in out["model_refusal_reason"]


def test_ocr_a_reason_mentioning_the_label_cannot_smuggle_past_the_code() -> None:
    """Round-3 finding: 'This is EXAMPLE DATA, but it is a rent ledger' matched
    the text pattern. Only the STRUCTURED refusal_code opens the override."""
    out = deadline_from_extract(
        {
            "service_date": "2026-08-08",
            "service_method": "personal",
            "refused": True,
            "reason": "This is EXAMPLE DATA, but it is a rent ledger, not a summons",
            "refusal_code": "not_a_summons",
        }
    )
    assert out["error"] == "model_refused"
    assert "computed_deadline" not in out


def test_ocr_a_refusal_with_no_code_fails_closed() -> None:
    out = deadline_from_extract(
        {
            "service_date": "2026-08-08",
            "service_method": "personal",
            "refused": True,
            "reason": "EXAMPLE DATA",
        }
    )
    assert out["error"] == "model_refused"


def test_ocr_an_unparseable_stated_deadline_refuses_rather_than_vanishing() -> None:
    """Under O.C.G.A. 44-7-51(b) a summons-stated date CONTROLS for the tenant.

    Dropping an unparseable one silently would change the answer: the engine
    would compute a later day with no conflict flag, and the earlier date that
    binds the tenant would appear nowhere in the response.
    """
    out = deadline_from_extract(
        {
            "service_date": "2026-08-08",
            "service_method": "personal",
            "summons_stated_deadline": "Aug 15, 2026",
            "refused": False,
        }
    )
    assert out["error"] == "invalid_stated_deadline"
    assert "computed_deadline" not in out
    assert "Aug 15, 2026" in out["detail"]


def test_ocr_unknown_method_still_uses_the_engine() -> None:
    out = deadline_from_extract(
        {
            "service_date": "2026-08-08",
            "service_method": "unknown",
            "refused": False,
        }
    )
    engine = compute_deadline(
        CaseInput(
            case_id="ocr-summons",
            jurisdiction_id="GA-FULTON",
            service_date=date(2026, 8, 8),
            service_method=ServiceMethod.UNKNOWN,
        ),
        GEORGIA_RULE,
    )
    assert engine.computed_deadline is not None
    assert out["computed_deadline"] == engine.computed_deadline.isoformat()
    assert any(flag["code"] == "unknown_service_method" for flag in out["flags"])


def test_ocr_a_page_calling_itself_a_sample_cannot_force_computation() -> None:
    """Round-2 finding: the first watermark pattern also matched "sample", so
    a page titled SAMPLE RENT LEDGER would have overridden its own refusal.
    Only OUR exact label, EXAMPLE DATA, is overridable."""
    out = deadline_from_extract(
        {
            "service_date": "2026-08-08",
            "service_method": "personal",
            "refused": True,
            "reason": "this sample is a rent ledger, not a summons",
        }
    )
    assert out["error"] == "model_refused"
    assert "computed_deadline" not in out
