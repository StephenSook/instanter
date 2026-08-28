"""Photograph a summons, extract printed fields, then compute the deadline.

The model may only transcribe what is on the page. It never computes a
deadline. ``compute_deadline`` does that. A guessed date is a refusal.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import date
from typing import Any

from engine.deadline import CaseInput, compute_deadline
from engine.rules import GEORGIA_RULE, ServiceMethod

MAX_IMAGE_BYTES = 2_000_000

NOVA_PRO = "us.amazon.nova-pro-v1:0"
EXTRACT_PROMPT = (
    "This image is a Georgia dispossessory summons, labelled EXAMPLE DATA. "
    "Extract ONLY what is printed. Reply with JSON and nothing else: "
    '{"service_date":"YYYY-MM-DD"|null,"service_method":"personal"|"tack_and_mail"'
    '|"unknown","summons_stated_deadline":"YYYY-MM-DD"|null,"case_id":string|null,'
    '"refused":bool,"reason":string,"refusal_code":"example_watermark"|'
    '"not_a_summons"|"unreadable"|null}. '
    "The page is labelled EXAMPLE DATA on purpose; that is not a reason to refuse, "
    'but if you refuse ONLY because of that label, set refusal_code="example_watermark". '
    "If the page is not a dispossessory summons at all, refused=true with "
    'refusal_code="not_a_summons". If a date is unreadable, refused=true with '
    'refusal_code="unreadable". '
    "Do not compute a deadline. Do not guess a missing date."
)
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
# The ONLY refusal a transcribed date may override: the spurious one about our
# own EXAMPLE DATA watermark (commit 71b9f28), identified by the STRUCTURED
# refusal_code, never by matching free-form reason text. Two earlier cuts
# proved text matching unsound: "sample" matched a SAMPLE RENT LEDGER's own
# title, and even an exact "example data" pattern matched a reason that
# merely mentioned the label while refusing for a real cause. A refusal
# without the code fails closed.
_OVERRIDABLE_REFUSAL_CODE = "example_watermark"


def extract_summons_fields(image: bytes, media: str, converse: Any) -> dict[str, Any]:
    fmt = "jpeg"
    if media in ("image/png", "png"):
        fmt = "png"
    elif media in ("image/webp", "webp"):
        fmt = "webp"
    elif media in ("image/gif", "gif"):
        fmt = "gif"
    response = converse(
        modelId=NOVA_PRO,
        messages=[
            {
                "role": "user",
                "content": [
                    {"image": {"format": fmt, "source": {"bytes": image}}},
                    {"text": EXTRACT_PROMPT},
                ],
            }
        ],
        inferenceConfig={"maxTokens": 400, "temperature": 0},
    )
    text = ""
    for block in response.get("output", {}).get("message", {}).get("content", []):
        if "text" in block:
            text += block["text"]
    match = _JSON_BLOCK.search(text)
    if not match:
        return {"refused": True, "reason": "the model did not return JSON"}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"refused": True, "reason": "the model returned invalid JSON"}
    if not isinstance(parsed, dict):
        return {"refused": True, "reason": "the model returned a non-object"}
    return parsed


def deadline_from_extract(extracted: dict[str, Any]) -> dict[str, Any]:
    raw_date = extracted.get("service_date")
    if not raw_date:
        if extracted.get("refused"):
            return {
                "error": "summons_unreadable",
                "detail": str(extracted.get("reason") or "the summons could not be read"),
                "extracted": extracted,
            }
        return {
            "error": "service_date_missing",
            "detail": (
                "No service date was printed on the summons, so the engine will not guess one."
            ),
            "extracted": extracted,
        }
    if extracted.get("refused"):
        reason = str(extracted.get("reason") or "")
        if str(extracted.get("refusal_code") or "") != _OVERRIDABLE_REFUSAL_CODE:
            # A refusal WITH a transcribed date used to compute anyway, which
            # let any dated document (a rent demand, a ledger) be presented as
            # a summons deadline even when the model correctly rejected it.
            return {
                "error": "model_refused",
                "detail": (
                    "The model refused to read this as a summons: "
                    f"{reason or 'no reason given'}. No deadline is computed "
                    "from a page the model rejected."
                ),
                "extracted": extracted,
            }
    try:
        service_date = date.fromisoformat(str(raw_date))
    except ValueError:
        return {
            "error": "invalid_service_date",
            "detail": f"{raw_date!r} is not an ISO date.",
            "extracted": extracted,
        }
    raw_method = str(extracted.get("service_method") or "unknown")
    try:
        method = ServiceMethod(raw_method)
    except ValueError:
        return {
            "error": "invalid_service_method",
            "detail": f"{raw_method!r} is not a ServiceMethod the engine knows.",
            "extracted": extracted,
        }
    stated = extracted.get("summons_stated_deadline")
    stated_date = None
    if stated:
        try:
            stated_date = date.fromisoformat(str(stated))
        except ValueError:
            # NOT a silent None. Under O.C.G.A. 44-7-51(b) a summons-stated
            # date CONTROLS for the tenant, so dropping an unparseable one
            # would change the answer, not the metadata: the engine would
            # compute a later day with no conflict flag and the earlier date
            # that binds the tenant would appear nowhere. Same posture as an
            # unparseable service date three checks up.
            return {
                "error": "invalid_stated_deadline",
                "detail": (
                    f"{stated!r} is not an ISO date. The summons-stated deadline "
                    "can control under O.C.G.A. 44-7-51(b), so it is never "
                    "silently dropped."
                ),
                "extracted": extracted,
            }
    case = CaseInput(
        case_id=str(extracted.get("case_id") or "ocr-summons"),
        jurisdiction_id=GEORGIA_RULE.jurisdiction_id,
        service_date=service_date,
        service_method=method,
        summons_stated_deadline=stated_date,
    )
    result = compute_deadline(case, GEORGIA_RULE)
    payload: dict[str, Any] = {
        "extracted": {
            "service_date": service_date.isoformat(),
            "service_method": method.value,
            "summons_stated_deadline": stated_date.isoformat() if stated_date else None,
            "case_id": case.case_id,
        },
        "computed_deadline": (
            result.computed_deadline.isoformat() if result.computed_deadline else None
        ),
        "effective_deadline": (
            result.effective_deadline.isoformat() if result.effective_deadline else None
        ),
        "deadline_basis": result.deadline_basis.value,
        "citation": result.citation,
        "flags": [
            {
                "code": flag.code.value,
                "reason": flag.reason,
                "day": flag.day.isoformat() if flag.day else None,
            }
            for flag in result.flags
        ],
        "trace": [{"day": step.day.isoformat(), "label": step.label} for step in result.trace],
        "label": "EXAMPLE DATA: extracted from an image you supplied, then computed by the engine.",
    }
    if extracted.get("refused"):
        # A transcribed date overrides a spurious refusal (commit 71b9f28,
        # after Nova refused the EXAMPLE DATA watermark), but the objection
        # itself is evidence the caller must see: it may mean the photographed
        # page was not a summons at all.
        payload["model_refusal_reason"] = str(extracted.get("reason") or "the model objected")[:300]
    return payload


def handle_ocr(body: dict[str, Any], converse: Any) -> dict[str, Any]:
    raw_b64 = str(body.get("image_b64") or "")
    media = str(body.get("media_type") or "image/png")
    if not raw_b64:
        return {
            "error": "image_required",
            "detail": "Pass image_b64. The engine will not invent a summons.",
        }
    try:
        image = base64.b64decode(raw_b64, validate=False)
    except Exception:
        return {"error": "invalid_image", "detail": "image_b64 was not valid base64."}
    if not image:
        return {"error": "invalid_image", "detail": "image_b64 decoded empty."}
    if len(image) > MAX_IMAGE_BYTES:
        return {
            "error": "image_too_large",
            "detail": f"Max {MAX_IMAGE_BYTES} bytes after decode.",
        }
    try:
        extracted = extract_summons_fields(image, media, converse)
    except Exception as exc:
        return {
            "error": "ocr_upstream",
            "detail": str(exc)[:400],
        }
    return deadline_from_extract(extracted)
