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
