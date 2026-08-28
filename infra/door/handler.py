"""The judge's door: a public, credential-free front door to Instanter.

ADR-0006 explains why this exists. `InvokeAgentRuntime` accepts only IAM SigV4
or an OAuth bearer token, so a browser cannot reach the agent at all. This
function holds the credential and invokes the runtime server-side.

Eleven endpoints, and only one of them is the point:

* ``GET  /api/health``          liveness, open, so an uptime check needs no secret.
* ``GET  /api/stats``           **the checkable number.** Recomputes every answer
                                deadline in the corpus with the real engine, on
                                request, and reports how many of them hand
                                counting gets wrong. No cache, no stored answer.
* ``GET  /api/what-if``         one statutory computation on a date the visitor
                                chose. Same ``compute_deadline`` the tests cover.
                                Pure arithmetic, never capped.
* ``GET  /api/queue``           the filing cabinet, recomputed on this request
                                from the engine plus the triage ladder. Not a
                                stored snapshot. Never capped.
* ``GET  /api/push/vapid``      Web Push public key. Subscribe from the console.
* ``POST /api/push/subscribe``  store a push subscription. Pings fire only when
                                a sweep actually stops for an attorney.
* ``POST /api/ocr``             photograph a summons; Nova Pro transcribes
                                printed fields; the engine computes the deadline.
* ``GET  /api/awaiting``        runs still owed a decision, as counts only: no
                                run ids, because /decision is unauthenticated.
* ``POST /api/run``             start a triage sweep on the deployed agent.
* ``GET  /api/run/{id}``        poll it. The door polls because a managed Python
                                Lambda cannot stream (ADR-0006).
* ``POST /api/run/{id}/decision`` answer the attorney interrupt, which resumes
                                the run on fresh compute (proven in spike 0001).

The statutory computation here is the SAME code the test suite covers and the
CLI runs. It is imported, not reimplemented, so a visitor who clicks /api/stats
is watching the product work rather than reading a number someone typed.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import boto3
import lock as door_lock
import ocr as door_ocr
import push as door_push
from boto3.dynamodb.conditions import Key

from agent.triage import TriageCase, triage_queue
from engine.deadline import CaseInput, compute_deadline
from engine.rules import GEORGIA_RULE, ServiceMethod

REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE_NAME = os.environ.get("RUN_TABLE", "")
RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
ORIGIN_SECRET = os.environ.get("ORIGIN_SECRET", "")
GIT_SHA = os.environ.get("GIT_SHA", "unknown")
# Sparse index over `status`. The daily counter rows carry no status attribute,
# so they are absent from it by construction rather than filtered out.
AWAITING_INDEX = os.environ.get("AWAITING_INDEX", "status-created_at-index")


def _seed_path() -> Path:
    """Resolve the corpus, in a stated order, and refuse if it is absent.

    Deployed, the seed sits beside this file because `build_door.py` copies it
    there. Running from the repo (the tests, a local check) it is two levels
    up. Both are legitimate; a SILENT fallback between them would not be, so
    the order is explicit and the failure names every path that was tried.
    """
    override = os.environ.get("SEED_PATH")
    candidates = (
        [Path(override)]
        if override
        else [
            Path(__file__).parent / "seed" / "synthetic_intake.json",
            Path(__file__).parent.parent.parent / "seed" / "synthetic_intake.json",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "the case corpus is missing; tried: " + ", ".join(str(c) for c in candidates)
    )


# A judge's click must not be able to start an unbounded number of model runs.
#
# ADR-0006 planned to use reserved concurrency for this. It cannot: this account
# has a TOTAL Lambda concurrency limit of 10 (the new-account default is not the
# familiar 1000), and reserving any of it would drop unreserved concurrency below
# the required minimum of 10, which fails the deploy outright. The account limit
# is therefore already the concurrency cap, imposed from above.
#
# So the cap that matters is on SPEND, not on parallelism. /api/stats is pure
# arithmetic and costs nothing however often it is called; /api/run invokes a
# model. This counter bounds only the endpoint that can spend money.
MAX_RUNS_PER_DAY = int(os.environ.get("MAX_RUNS_PER_DAY", "200"))
# The scheduled morning sweep gets its own budget rather than sharing the
# visitor cap. Those two caps defend against different things: the visitor cap
# stops a stranger draining model spend, and this one stops a misconfigured or
# retried schedule from firing the sweep in a loop. Sharing one counter would
# mean a busy day of visitors silently cancels the clinic's morning sweep,
# which is the one run that actually has to happen.
MAX_SCHEDULED_RUNS_PER_DAY = int(os.environ.get("MAX_SCHEDULED_RUNS_PER_DAY", "2"))
# Upper bound on rows /api/awaiting will walk. It exists to bound the work, and
# when it bites the response says `truncated: true` rather than presenting a
# partial page as the whole list.
MAX_AWAITING_SCAN = int(os.environ.get("MAX_AWAITING_SCAN", "500"))
# Deadlines are counted in the court's calendar, not in UTC. Fulton County
# State Court sits here, and a named zone follows daylight saving on its own.
COURT_TZ = ZoneInfo(os.environ.get("COURT_TZ", "America/New_York"))

_ddb = None


def table() -> Any:
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb", region_name=REGION)
    return _ddb.Table(TABLE_NAME)


def claim_daily_run_slot(origin: str = "visitor") -> tuple[bool, int]:
    """Atomically count today's runs of one origin and say whether to proceed.

    ADD is an atomic counter in DynamoDB, so two simultaneous clicks cannot
    both read the same value and both decide they were under the cap. The
    counter row expires on its own, so nothing has to clean it up.

    Each origin counts against its own row, so the scheduled sweep and the
    visitor traffic cannot exhaust one another.
    """
    cap = MAX_SCHEDULED_RUNS_PER_DAY if origin == "scheduled" else MAX_RUNS_PER_DAY
    today = time.strftime("%Y-%m-%d", time.gmtime())
    key = f"__daily__{today}" if origin == "visitor" else f"__daily__{origin}__{today}"
    result = table().update_item(
        Key={"run_id": key},
        UpdateExpression="ADD #n :one SET #e = :exp",
        ExpressionAttributeNames={"#n": "started", "#e": "expires_at"},
        ExpressionAttributeValues={":one": 1, ":exp": int(time.time()) + 3 * 24 * 3600},
        ReturnValues="UPDATED_NEW",
    )
    used = int(result["Attributes"]["started"])
    return used <= cap, used


def _json(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            # /api/stats must never be served from a cache, or it stops being
            # a recomputation and becomes a stored number.
            "cache-control": "no-store",
        },
        "body": json.dumps(body, default=str),
    }


# ---------------------------------------------------------------- statutory


def _as_date(value: Any) -> date | None:
    return date.fromisoformat(value) if value else None


def _case_input(record: dict[str, Any]) -> CaseInput:
    return CaseInput(
        case_id=record["case_id"],
        jurisdiction_id=record["jurisdiction_id"],
        service_date=_as_date(record.get("service_date")),
        service_method=ServiceMethod(record["service_method"]),
        posting_date=_as_date(record.get("posting_date")),
        mailing_date=_as_date(record.get("mailing_date")),
        summons_stated_deadline=_as_date(record.get("summons_stated_deadline")),
        amended_affidavit=record.get("amended_affidavit", False),
    )


def compute_stats() -> dict[str, Any]:
    """Recompute the whole corpus and report what hand counting would miss.

    Two DIFFERENT mechanisms produce a wrong hand-counted date, and they are
    reported separately because conflating them would overstate one of them:

    1. The terminal roll. Counting seven days from service can land on a
       Saturday or Sunday, and O.C.G.A. 1-3-1(d)(3) rolls the deadline to the
       next day the court is open.
    2. The summons. Where the summons states a date that differs from the
       computation, O.C.G.A. 44-7-51(b) makes the stated date the one that
       controls for the tenant, so being right about the arithmetic is still
       being wrong about the deadline.
    """
    seed = json.loads(_seed_path().read_text())
    records = seed["records"]
    window = GEORGIA_RULE.window_length_days

    started = time.perf_counter()
    results = [(r, compute_deadline(_case_input(r), GEORGIA_RULE)) for r in records]
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)

    computed = refused = flagged = 0
    roll_divergences: list[dict[str, Any]] = []
    summons_controls: list[dict[str, Any]] = []

    for record, result in results:
        # Count the flag BEFORE the refusal branch. A case the engine refused
        # to compute is exactly the case a clinic most needs flagged: both of
        # ours carry SERVICE_DATE_MISSING and UNKNOWN_SERVICE_METHOD. Counting
        # flags only on computed cases undercounted by those two and made this
        # panel disagree with the queue rendered directly beneath it.
        if result.flags:
            flagged += 1
        if result.computed_deadline is None:
            refused += 1
            continue
        computed += 1

        served = _as_date(record.get("service_date")) or _as_date(record.get("posting_date"))
        if served is not None:
            hand_counted = served + timedelta(days=window)
            if hand_counted != result.computed_deadline:
                roll_divergences.append(
                    {
                        "case_id": record["case_id"],
                        "served": str(served),
                        "hand_counted": str(hand_counted),
                        "hand_counted_weekday": hand_counted.strftime("%A"),
                        "statutory": str(result.computed_deadline),
                        "statutory_weekday": result.computed_deadline.strftime("%A"),
                        "days_off": (result.computed_deadline - hand_counted).days,
                    }
                )

        if result.effective_deadline != result.computed_deadline:
            summons_controls.append(
                {
                    "case_id": record["case_id"],
                    "computed": str(result.computed_deadline),
                    "controlling": str(result.effective_deadline),
                    "authority": GEORGIA_RULE.summons_authority,
                }
            )

    wrong_by_hand = len(roll_divergences) + len(summons_controls)
    return {
        "recomputed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": "Recomputed on this request. Nothing here is stored or cached.",
        "corpus": {
            "cases": len(records),
            "label": seed.get("label", ""),
            "run_date": seed.get("demo_run_date"),
        },
        "computation": {
            "deadlines_computed": computed,
            "refused_unverified": refused,
            "cases_carrying_a_flag": flagged,
            "elapsed_ms": elapsed_ms,
            "citation": GEORGIA_RULE.citation_string,
        },
        "headline": {
            "answer_deadlines_hand_counting_gets_wrong": wrong_by_hand,
            "of_deadlines_computed": computed,
            "why_it_matters": (
                "A missed answer deadline in a dispossessory case is a default "
                "judgment, which is an eviction."
            ),
        },
        "because_the_deadline_rolls": roll_divergences,
        "because_the_summons_controls": summons_controls,
    }


def compute_what_if(service_date: date, method: ServiceMethod) -> dict[str, Any]:
    """One statutory computation on a date the visitor chose.

    Imports ``compute_deadline``; it does not reimplement the count. The
    console is allowed to *display* this payload, never to invent a date,
    a day count, or a flag of its own.
    """
    case = CaseInput(
        case_id="what-if",
        jurisdiction_id=GEORGIA_RULE.jurisdiction_id,
        service_date=service_date,
        service_method=method,
    )
    result = compute_deadline(case, GEORGIA_RULE)
    return {
        "service_date": service_date.isoformat(),
        "service_method": method.value,
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
        "court_reopens_on": (
            result.court_reopens_on.isoformat() if result.court_reopens_on else None
        ),
        "label": (
            "EXAMPLE DATA: this is a statutory computation on a date you chose, "
            "not a live case file."
        ),
    }


def what_if_from_event(event: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """Parse the visitor's date out of the query string or JSON body."""
    params = dict(event.get("queryStringParameters") or {})
    raw_qs = event.get("rawQueryString") or ""
    if not params and raw_qs:
        for pair in raw_qs.split("&"):
            if "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            params[key] = value
    raw_date = str(params.get("service_date") or body.get("service_date") or "").strip()
    raw_method = str(
        params.get("service_method") or body.get("service_method") or "personal"
    ).strip()
    if not raw_date:
        return _json(
            400,
            {
                "error": "service_date_required",
                "detail": "Pass service_date=YYYY-MM-DD. The engine will not guess a date.",
            },
        )
    try:
        service_date = date.fromisoformat(raw_date)
    except ValueError:
        return _json(
            400,
            {
                "error": "invalid_service_date",
                "detail": f"{raw_date!r} is not an ISO date.",
            },
        )
    try:
        method = ServiceMethod(raw_method)
    except ValueError:
        return _json(
            400,
            {
                "error": "invalid_service_method",
                "detail": f"{raw_method!r} is not a ServiceMethod.",
            },
        )
    return _json(200, compute_what_if(service_date, method))


def compute_queue() -> dict[str, Any]:
    """Rank the corpus with the same engine and ladder the snapshot uses.

    This is the live cabinet. It does not invoke a model, so it is never
    capped. Writer rationales are omitted: they appear when someone Sweeps.
    The UI may display this payload. It may not invent a date or a rank.
    """
    seed = json.loads(_seed_path().read_text())
    records = seed["records"]
    run_date = date.fromisoformat(str(seed["demo_run_date"]))
    capacity = 2

    started = time.perf_counter()
    triage_cases: list[TriageCase] = []
    by_id: dict[str, tuple[dict[str, Any], Any]] = {}
    for record in records:
        result = compute_deadline(_case_input(record), GEORGIA_RULE)
        by_id[record["case_id"]] = (record, result)
        triage_cases.append(
            TriageCase(
                case_id=record["case_id"],
                deadline=result,
                answer_filed=bool(record.get("answer_filed")),
                notes_present=bool(str(record.get("notes") or "").strip()),
            )
        )
    decisions = triage_queue(triage_cases, run_date, capacity)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)

    cases: list[dict[str, Any]] = []
    for decision in decisions:
        record, result = by_id[decision.case_id]
        cases.append(
            {
                "case_id": decision.case_id,
                "level": decision.level.value,
                "floor_level": decision.floor_level.value,
                "rank": decision.rank,
                "days_remaining": decision.days_remaining,
                "interrupt_now": decision.interrupt_now,
                "held_reason": decision.held_reason,
                "raised_by": list(decision.raised_by),
                "factors": list(decision.factors),
                "flags": [
                    {
                        "code": flag.code.value,
                        "reason": flag.reason,
                        "day": flag.day.isoformat() if flag.day else None,
                    }
                    for flag in result.flags
                ],
                "effective_deadline": (
                    result.effective_deadline.isoformat() if result.effective_deadline else None
                ),
                "computed_deadline": (
                    result.computed_deadline.isoformat() if result.computed_deadline else None
                ),
                "deadline_basis": result.deadline_basis.value,
                "citation": result.citation,
                "court_reopens_on": (
                    result.court_reopens_on.isoformat() if result.court_reopens_on else None
                ),
                "trace": [
                    {"day": step.day.isoformat(), "label": step.label} for step in result.trace
                ],
                "service_date": record.get("service_date"),
                "service_method": record.get("service_method"),
                "answer_filed": bool(record.get("answer_filed")),
                "tenant_display_name": record.get("tenant_display_name") or "",
                "property_address": record.get("property_address") or "",
                "notes": record.get("notes") or "",
                "label": record.get("label") or "EXAMPLE DATA",
                "rationale": None,
                "packet_memo": None,
            }
        )

    interrupts = [c["case_id"] for c in cases if c["interrupt_now"]]
    return {
        "generated_by": "door /api/queue (engine + triage ladder, recomputed on this request)",
        "source": "live",
        "mode": "live-ladder",
        "run_date": run_date.isoformat(),
        "attorney_capacity": capacity,
        "elapsed_ms": elapsed_ms,
        "label": seed.get("label", "EXAMPLE DATA"),
        "succeeded": True,
        "report": {
            "run_id": "queue-live",
            "committed": [],
            "interrupts": interrupts,
            "refused": [],
            "failures": [],
            "attorney_action": "none",
            "backstop_used": False,
        },
        "cases": cases,
        "audit": [],
        "counts": {
            "total": len(cases),
            "interrupt": sum(1 for c in cases if c["level"] == "interrupt"),
            "surface_today": sum(1 for c in cases if c["level"] == "surface_today"),
            "monitor": sum(1 for c in cases if c["level"] == "monitor"),
            "hold": sum(1 for c in cases if c["level"] == "hold"),
            "flagged": sum(1 for c in cases if c["flags"]),
            "audit_events": 0,
        },
    }


def attach_steps(result: dict[str, Any]) -> dict[str, Any]:
    """A receipt the console can print without inventing a pipeline.

    If the agent already returned ``steps`` or ``audit``, those win. Otherwise
    the door names only the stages this payload itself proves happened
    (a total, an interrupt, a decision). The UI prints this list in order.
    """
    if result.get("steps") or result.get("audit"):
        return result
    steps: list[dict[str, Any]] = []
    seq = 1
    total = result.get("total_cases")
    if total is not None:
        steps.append({"seq": seq, "kind": "ingest", "detail": f"{total} cases read"})
        seq += 1
    steps.append({"seq": seq, "kind": "compute", "detail": "statutory deadlines"})
    seq += 1
    steps.append({"seq": seq, "kind": "rank", "detail": "queue ranked"})
    seq += 1
    for custom in result.get("spans") or []:
        name = custom.get("name") if isinstance(custom, dict) else None
        if name:
            steps.append({"seq": seq, "kind": "span", "detail": str(name)})
            seq += 1
    if result.get("interrupted"):
        waiting = result.get("awaiting") or []
        steps.append(
            {
                "seq": seq,
                "kind": "stop",
                "detail": f"attorney interrupt ({len(waiting)} case(s))",
            }
        )
    else:
        action = result.get("attorney_action") or "complete"
        steps.append({"seq": seq, "kind": "stop", "detail": str(action)})
    out = dict(result)
    out["steps"] = steps
    return out


# --------------------------------------------------------------------- runs


def _runtime_session_id() -> str:
    # runtimeSessionId has a 33 character MINIMUM (confirmed empirically in
    # spike 0001: 32 is rejected, 33 is accepted).
    return f"door-{uuid.uuid4().hex}-{uuid.uuid4().hex[:8]}"


def start_run(
    body: dict[str, Any], origin: str = "visitor", run_date: str | None = None
) -> dict[str, Any]:
    if not RUNTIME_ARN:
        # Loud, not silent. A door that pretends to start a run it cannot start
        # is the same defect as an interrupt with no persisted state.
        return _json(
            503,
            {
                "error": "agent_runtime_not_configured",
                "detail": (
                    "AGENT_RUNTIME_ARN is unset, so this door has no agent to "
                    "invoke. /api/stats still recomputes the statutory engine."
                ),
            },
        )

    # Validate BEFORE claiming a slot. The claim counts against a daily paid
    # budget, so 200 requests carrying {"capacity": "not-an-integer"} used to
    # exhaust the cap while every one of them failed before AgentCore, and a
    # judge arriving later got a 429 for someone else's garbage.
    try:
        capacity = int(body.get("capacity", 2))
    except (TypeError, ValueError):
        return _json(
            400,
            {"error": "invalid_capacity", "detail": "capacity must be an integer, 1 to 48."},
        )
    if not 1 <= capacity <= 48:
        return _json(
            400,
            {"error": "invalid_capacity", "detail": "capacity must be an integer, 1 to 48."},
        )

    allowed, used_today = claim_daily_run_slot(origin)
    if not allowed:
        # 429 with the numbers in it, so a judge who hits the cap knows this is
        # a deliberate spend bound rather than a broken endpoint, and knows the
        # free endpoint is still there.
        return _json(
            429,
            {
                "error": "daily_run_cap_reached",
                "origin": origin,
                "runs_today": used_today,
                "cap": (MAX_SCHEDULED_RUNS_PER_DAY if origin == "scheduled" else MAX_RUNS_PER_DAY),
                "detail": (
                    "This door invokes a paid model, so live runs are capped per "
                    "day. /api/stats is pure arithmetic and is never capped."
                ),
            },
        )

    run_id = uuid.uuid4().hex[:16]
    session = _runtime_session_id()
    now = int(time.time())
    table().put_item(
        Item={
            "run_id": run_id,
            "status": "starting",
            "origin": origin,
            "runtime_session_id": session,
            "created_at": now,
            "expires_at": now + 7 * 24 * 3600,
            "capacity": capacity,
            **({"run_date": run_date} if run_date else {}),
        }
    )

    payload: dict[str, Any] = {
        "action": "start",
        "run_id": run_id,
        "capacity": capacity,
    }
    if run_date:
        # Threaded through explicitly. Without it the agent falls back to the
        # corpus's frozen demo_run_date, so every morning would re-triage the
        # same day and rank a case by a stale distance to its deadline.
        payload["run_date"] = run_date
    try:
        # Constructed INSIDE the try. It was outside, which meant a client that
        # failed to build (bad region, missing credentials, a botocore data
        # error) escaped as an unhandled exception: the run row stayed on
        # "starting" forever and the caller got an opaque 500. The point of
        # this block is that a failure is surfaced, and a crash is neither
        # surfaced nor swallowed.
        client = boto3.client("bedrock-agentcore", region_name=REGION)
        response = client.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            runtimeSessionId=session,
            payload=json.dumps(payload).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
        result = attach_steps(json.loads(response["response"].read()))
    except Exception as exc:  # surfaced, never swallowed
        table().update_item(
            Key={"run_id": run_id},
            UpdateExpression="SET #s = :s, #e = :e",
            ExpressionAttributeNames={"#s": "status", "#e": "error"},
            ExpressionAttributeValues={":s": "failed", ":e": str(exc)[:900]},
        )
        return _json(502, {"run_id": run_id, "status": "failed", "error": str(exc)[:400]})

    status = "awaiting_attorney" if result.get("interrupted") else "complete"
    table().update_item(
        Key={"run_id": run_id},
        UpdateExpression="SET #s = :s, #r = :r",
        ExpressionAttributeNames={"#s": "status", "#r": "result"},
        ExpressionAttributeValues={":s": status, ":r": json.dumps(result, default=str)},
    )
    try:
        door_lock.lock_record(
            "sweep_interrupted" if status == "awaiting_attorney" else "sweep_complete",
            run_id,
            {"status": status, "interrupted": bool(result.get("interrupted"))},
        )
    except Exception as exc:
        result = dict(result)
        result["audit_lock_error"] = str(exc)[:200]
    if status == "awaiting_attorney" and os.environ.get("PUSH_TABLE"):
        # A crashed notify must not break the run, but it must not read as
        # "zero subscribers" either: a lost IAM grant would otherwise report
        # push_sent: 0 forever while every phone stays silent.
        result = dict(result)
        try:
            result["push_sent"] = door_push.notify_interrupt(
                boto3.resource("dynamodb", region_name=REGION).Table(os.environ["PUSH_TABLE"])
            )
        except Exception as exc:
            result["push_sent"] = 0
            result["push_error"] = str(exc)[:200]
    # Persist the operational outcome to the ROW, not only to this response.
    # The scheduled sweep's response goes to EventBridge, which reads none of
    # it, so an AccessDenied on the Object Lock write at 7am would otherwise
    # be an error nobody ever received.
    ops = {
        k: result[k]
        for k in ("audit_lock_error", "push_sent", "push_error")
        if isinstance(result, dict) and k in result
    }
    if ops:
        # Best-effort: the response still carries the outcome.
        with contextlib.suppress(Exception):
            table().update_item(
                Key={"run_id": run_id},
                UpdateExpression="SET ops_report = :o",
                ExpressionAttributeValues={":o": json.dumps(ops, default=str)},
            )
    return _json(202, {"run_id": run_id, "status": status, "result": result})


def get_run(run_id: str) -> dict[str, Any]:
    item = table().get_item(Key={"run_id": run_id}).get("Item")
    if not item:
        return _json(404, {"error": "no_such_run", "run_id": run_id})
    payload = dict(item)
    raw = payload.pop("result", None)
    if raw:
        payload["result"] = json.loads(raw)
    payload.pop("runtime_session_id", None)  # internal
    return _json(200, payload)


def _release_decision_claim(run_id: str) -> None:
    """Best-effort: hand the run back to awaiting_attorney after a failed resume.

    A failed release is not retried; the stale-claim window in decide() is the
    backstop that lets a later decider re-claim.
    """
    with contextlib.suppress(Exception):
        table().update_item(
            Key={"run_id": run_id},
            UpdateExpression="SET #s = :a REMOVE deciding_at",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":a": "awaiting_attorney"},
        )


def decide(run_id: str, body: dict[str, Any]) -> dict[str, Any]:
    item = table().get_item(Key={"run_id": run_id}).get("Item")
    if not item:
        return _json(404, {"error": "no_such_run", "run_id": run_id})
    answer = str(body.get("response", "")).strip()
    if not answer:
        return _json(
            400, {"error": "empty_response", "detail": "Send 'approve' or 'defer: <reason>'."}
        )

    stored = json.loads(item.get("result") or "{}")
    interrupts = stored.get("interrupts") or []
    if not interrupts:
        return _json(
            409, {"error": "nothing_to_answer", "run_id": run_id, "status": item.get("status")}
        )

    # EVERYTHING that can fail without touching the runtime happens BEFORE the
    # claim: the client construction, the interrupt id read, the capacity
    # conversion. A failure here used to escape after the claim and leave the
    # run stuck in "deciding" for the whole 130-second stale window.
    interrupt_id = str(interrupts[0].get("id") or "") if isinstance(interrupts[0], dict) else ""
    if not interrupt_id:
        return _json(
            409, {"error": "nothing_to_answer", "run_id": run_id, "status": item.get("status")}
        )
    try:
        capacity = int(item.get("capacity", 2))
    except (TypeError, ValueError):
        capacity = 2  # a malformed legacy row resumes with the old default
    try:
        client = boto3.client("bedrock-agentcore", region_name=REGION)
    except Exception as exc:
        return _json(502, {"run_id": run_id, "error": str(exc)[:400]})
    payload = {
        "action": "resume",
        "run_id": run_id,
        "interrupt_id": interrupt_id,
        "response": answer,
        # The resume rebuilds context from these, so they must be the values
        # the run STARTED with. Omitting them made the runtime default to
        # capacity 2 and the corpus's frozen demo date, which re-ranked the
        # queue and made the approval digest refuse the attorney's own answer
        # on any run that used a real run_date or a different capacity.
        "capacity": capacity,
    }
    if item.get("run_date"):
        payload["run_date"] = str(item["run_date"])

    # CLAIM the decision, last thing before the invoke. Two deciders (the web
    # console and the phone) answering the same run concurrently would both
    # resume the graph, and the last DynamoDB writer would win while the S3
    # state reflected the other execution. The conditional transition makes
    # the second caller lose loudly. A claim older than the Lambda ceiling
    # (120s) belongs to a crashed decider and may be re-claimed.
    now = int(time.time())
    try:
        table().update_item(
            Key={"run_id": run_id},
            UpdateExpression="SET #s = :d, deciding_at = :now",
            ConditionExpression="#s = :a OR (#s = :d AND deciding_at < :stale)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":d": "deciding",
                ":a": "awaiting_attorney",
                ":now": now,
                ":stale": now - 130,
            },
        )
    except Exception as exc:
        if "ConditionalCheckFailed" in type(exc).__name__ or "ConditionalCheckFailed" in str(exc):
            return _json(
                409,
                {
                    "error": "decision_in_progress",
                    "run_id": run_id,
                    "detail": "Another decision for this run is already being recorded.",
                },
            )
        raise

    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            runtimeSessionId=_runtime_session_id(),
            payload=json.dumps(payload).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
        result = attach_steps(json.loads(response["response"].read()))
    except Exception as exc:
        _release_decision_claim(run_id)
        return _json(502, {"run_id": run_id, "error": str(exc)[:400]})

    if isinstance(result, dict) and result.get("error"):
        # The runtime answered with an application error (no_such_run when S3
        # state is gone, unknown_action, ...). Persisting THAT as resolved
        # would record a decision that never executed, so the run goes back to
        # awaiting and the caller hears the runtime's own words.
        _release_decision_claim(run_id)
        return _json(
            502,
            {
                "run_id": run_id,
                "error": "agent_error",
                "detail": str(result.get("error"))[:200],
            },
        )

    kind = "attorney_decision"
    lowered = answer.lower().strip()
    if lowered.startswith("approve"):
        kind = "attorney_approved"
    elif lowered.startswith("defer"):
        kind = "attorney_deferred"
    audit_lock_error = ""
    try:
        # The attorney decision is the one event the Object Lock trail exists
        # for. A swallowed failure here means a week of decisions can return
        # 200 while the Compliance trail records sweeps and zero decisions.
        door_lock.lock_record(kind, run_id, {"status": "resolved"})
    except Exception as exc:
        audit_lock_error = str(exc)[:200]

    # One final write carrying the outcome AND the operational failure, so the
    # row is evidence, not just this response nobody may be reading.
    update_expr = "SET #s = :s, #r = :r, #a = :a REMOVE deciding_at"
    names = {"#s": "status", "#r": "result", "#a": "attorney_response"}
    values: dict[str, Any] = {
        ":s": "resolved",
        ":r": json.dumps(result, default=str),
        ":a": answer[:2000],
    }
    if audit_lock_error:
        update_expr = "SET #s = :s, #r = :r, #a = :a, audit_lock_error = :e REMOVE deciding_at"
        values[":e"] = audit_lock_error
    row_update_error = ""
    for attempt in (1, 2):
        try:
            table().update_item(
                Key={"run_id": run_id},
                UpdateExpression=update_expr,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            row_update_error = ""
            break
        except Exception as exc:
            # The resume has ALREADY executed and persisted on the runtime, so
            # this row write failing must not release the claim back to
            # awaiting: a re-claimed decide would resume an already-resumed
            # run. Retry once; if it still fails, the row stays in "deciding"
            # DELIBERATELY (the runtime rejects a second resume with an error
            # envelope, which decide() refuses to persist) and the response
            # says so.
            row_update_error = f"attempt {attempt}: {str(exc)[:150]}"
    payload_out: dict[str, Any] = {"run_id": run_id, "status": "resolved", "result": result}
    if audit_lock_error:
        payload_out["audit_lock_error"] = audit_lock_error
    if row_update_error:
        payload_out["row_update_error"] = row_update_error
    return _json(200, payload_out)


# ------------------------------------------------------- the morning sweep


def list_awaiting() -> dict[str, Any]:
    """Runs that stopped and are still waiting on a licensed attorney.

    Queried through a sparse index on `status`, so the daily counter rows,
    which carry no status, are absent by construction rather than by filter.
    """
    # PAGINATE. A single Limit=25 page silently dropped everything after the
    # 25th row, and the console filters that page for scheduled runs, so 25
    # newer visitor rows could hide the morning sweep and make the banner say
    # "nothing is waiting" when something was. A truncated read that produces a
    # confident absence is the same defect as an errored query reading as a
    # pass. The cap below bounds the work; it does not hide it.
    waiting: list[dict[str, Any]] = []
    scanned = 0
    start_key: dict[str, Any] | None = None
    truncated = False
    while True:
        kwargs: dict[str, Any] = {
            "IndexName": AWAITING_INDEX,
            "KeyConditionExpression": Key("status").eq("awaiting_attorney"),
            "ScanIndexForward": False,
            "Limit": 100,
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        result = table().query(**kwargs)
        for item in result.get("Items", []):
            scanned += 1
            raw = item.get("result")
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except (TypeError, ValueError):
                # A row whose result is unparseable is still owed a decision.
                # Report it with an unknown case count rather than crashing the
                # endpoint or dropping it from the list.
                parsed = {}
            waiting.append(
                {
                    # NO run_id. This endpoint is public, and POST
                    # /api/run/{id}/decision performs no authorization, so
                    # publishing the id of an unattended run would let any
                    # stranger approve or defer the clinic's morning sweep.
                    # That directly contradicts the claim this product is built
                    # on, that a licensed attorney decides. The console only
                    # ever needed the count.
                    "origin": item.get("origin", "visitor"),
                    "created_at": int(item.get("created_at", 0)),
                    "cases": len(parsed.get("awaiting") or []),
                }
            )
        start_key = result.get("LastEvaluatedKey")
        if not start_key:
            break
        if scanned >= MAX_AWAITING_SCAN:
            # Bounded, and SAID so, so a client never reads a partial list as a
            # complete one.
            truncated = True
            break
    return {
        "awaiting": waiting,
        "count": len(waiting),
        "truncated": truncated,
        # Said plainly because it is the product's whole claim: the sweep runs
        # whether or not anyone opened the page, and it stops rather than
        # deciding.
        "detail": (
            "Runs that computed every deadline, ranked the queue, and then "
            "stopped. Nothing in them is committed until an attorney answers."
        ),
    }


def claim_occurrence(occurrence: str) -> bool:
    """Claim one scheduled occurrence exactly once, before spending anything.

    EventBridge Scheduler delivers AT LEAST once, and asynchronous Lambda
    delivery can repeat an event even after it succeeded. Without this, a
    duplicate delivery minted a fresh run_id and became a second paid model run
    and a second awaiting row, and the scheduled cap of 2 permitted exactly
    that rather than deduplicating it.

    A conditional write is the whole mechanism: the second writer loses.
    """
    try:
        table().put_item(
            Item={
                "run_id": f"__occurrence__{occurrence}",
                "claimed_at": int(time.time()),
                "expires_at": int(time.time()) + 3 * 24 * 3600,
            },
            ConditionExpression="attribute_not_exists(run_id)",
        )
        return True
    except Exception as exc:
        if "ConditionalCheckFailed" in type(exc).__name__ or "ConditionalCheckFailed" in str(exc):
            return False
        raise


def court_today(scheduled_time: str | None) -> str:
    """The date the court is actually on, as an ISO string.

    Prefers the scheduler's own occurrence time so a retry re-triages the day it
    was FOR rather than the day it retried on. Falls back to now.
    """
    if scheduled_time:
        try:
            stamp = datetime.fromisoformat(scheduled_time.replace("Z", "+00:00"))
            return stamp.astimezone(COURT_TZ).date().isoformat()
        except ValueError:
            pass
    return datetime.now(COURT_TZ).date().isoformat()


def scheduled_sweep(event: dict[str, Any]) -> dict[str, Any]:
    """The 7am sweep, fired by EventBridge Scheduler rather than by a click.

    The README says a walk-in clinic cannot watch every clock. Until this
    existed the agent only ran when a human pressed a button, which is the one
    thing the pitch says nobody has time to do. It ends the same way a visitor
    run does: at the attorney interrupt, with nothing committed.
    """
    raw_time = event.get("scheduled_time")
    # The literal token comes back unsubstituted if the target was configured
    # without it; treat that as absent rather than parsing it as a date.
    scheduled_time = None if not raw_time or raw_time.startswith("<") else str(raw_time)
    occurrence = scheduled_time or datetime.now(COURT_TZ).strftime("%Y-%m-%dT%H")

    if not claim_occurrence(occurrence):
        # Already ran for this occurrence. Report it and spend nothing.
        return {
            "scheduled": True,
            "statusCode": 200,
            "duplicate": True,
            "occurrence": occurrence,
            "detail": "this occurrence already ran; at-least-once delivery repeated it",
        }

    run_date = court_today(scheduled_time)
    body = {"capacity": int(event.get("capacity", 2))}
    response = start_run(body, origin="scheduled", run_date=run_date)
    payload = json.loads(response["body"])
    if response["statusCode"] >= 400:
        # RAISE, do not return. EventBridge Scheduler invokes Lambda
        # asynchronously, so a statusCode sitting inside a returned object is
        # not an invocation failure: the delivery is recorded as a success and
        # the retry policy never fires. This used to return normally, and the
        # comment here claimed the opposite of what the code did, which meant a
        # sweep that never ran looked exactly like a sweep that did.
        #
        # And RELEASE THE CLAIM first, or the raise is self-defeating: the
        # retry it triggers would find this occurrence already claimed and
        # report duplicate: True as a success, so a transiently failed 7am
        # sweep could never actually retry that day. The delete is retried,
        # because a release that fails once suppresses EVERY remaining retry
        # until the claim's own TTL.
        release_error = ""
        for _ in range(3):
            try:
                table().delete_item(Key={"run_id": f"__occurrence__{occurrence}"})
                release_error = ""
                break
            except Exception as exc:
                release_error = f" (occurrence claim not released: {str(exc)[:100]})"
        raise RuntimeError(
            f"scheduled sweep failed: HTTP {response['statusCode']} "
            f"{payload.get('error') or payload.get('detail') or ''}".strip()
            + release_error
        )
    inner = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    return {
        "scheduled": True,
        "statusCode": response["statusCode"],
        "duplicate": False,
        "occurrence": occurrence,
        "run_date": run_date,
        "run_id": payload.get("run_id"),
        "status": payload.get("status"),
        "error": payload.get("error"),
        # Operational outcomes ride the EventBridge-visible return too; the
        # row's ops_report is the durable copy.
        "audit_lock_error": inner.get("audit_lock_error"),
        "push_sent": inner.get("push_sent"),
        "push_error": inner.get("push_error"),
    }


# ------------------------------------------------------------------ routing


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    # The scheduled sweep is checked FIRST and is unreachable over HTTP. A
    # Function URL event always carries rawPath, and a POST body arrives as a
    # STRING in event["body"] rather than merged into the event, so a caller
    # cannot forge this marker by sending it as JSON. Both conditions are
    # required rather than either, so the path cannot be reached by accident.
    if event.get("instanter_scheduled_sweep") is True and "rawPath" not in event:
        return scheduled_sweep(event)

    path = event.get("rawPath", "/")
    method = (event.get("requestContext", {}).get("http", {}) or {}).get("method", "GET")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    if path in ("/api/health", "/health"):
        return _json(
            200,
            {
                "ok": True,
                "service": "instanter-judge-door",
                "git_sha": GIT_SHA,
                "agent_runtime_configured": bool(RUNTIME_ARN),
            },
        )

    # Everything below is only reachable through CloudFront, which injects the
    # shared secret. A caller who found the Function URL directly is refused,
    # which is the documented alternative to Origin Access Control (OAC would
    # force AWS_IAM, and a browser POST then needs x-amz-content-sha256).
    if ORIGIN_SECRET and headers.get("x-instanter-origin") != ORIGIN_SECRET:
        return _json(403, {"error": "direct_origin_access_refused"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _json(400, {"error": "invalid_json"})

    if path == "/api/stats" and method == "GET":
        return _json(200, compute_stats())
    if path == "/api/what-if" and method == "GET":
        return what_if_from_event(event, body)
    if path == "/api/queue" and method == "GET":
        return _json(200, compute_queue())
    if path == "/api/push/vapid" and method == "GET":
        key = door_push.public_key()
        if not key:
            return _json(
                503,
                {
                    "error": "push_not_configured",
                    "detail": (
                        "VAPID_PUBLIC_KEY is unset, so this door cannot accept subscriptions."
                    ),
                },
            )
        return _json(200, {"publicKey": key})
    if path == "/api/push/subscribe" and method == "POST":
        if not os.environ.get("PUSH_TABLE"):
            return _json(503, {"error": "push_not_configured"})
        push_tbl = boto3.resource("dynamodb", region_name=REGION).Table(os.environ["PUSH_TABLE"])
        saved = door_push.save_subscription(push_tbl, body)
        if saved.get("error"):
            return _json(400, saved)
        return _json(200, saved)
    if path == "/api/ocr" and method == "POST":
        # Cheap validation BEFORE the slot claim: 200 empty bodies used to
        # exhaust the day's OCR budget without ever reaching Nova.
        raw_b64 = str(body.get("image_b64") or "")
        if not raw_b64:
            return _json(
                400,
                {
                    "error": "image_required",
                    "detail": "Pass image_b64. The engine will not invent a summons.",
                },
            )
        if len(raw_b64) > 2_800_000:
            return _json(
                400,
                {"error": "image_too_large", "detail": "Max 2MB after decode."},
            )
        allowed, used_today = claim_daily_run_slot("ocr")
        if not allowed:
            return _json(
                429,
                {
                    "error": "daily_run_cap_reached",
                    "origin": "ocr",
                    "runs_today": used_today,
                    "cap": MAX_RUNS_PER_DAY,
                    "detail": (
                        "OCR invokes Nova Pro, so it is capped. /api/what-if is never capped."
                    ),
                },
            )
        try:
            # Constructed inside the try for the same reason start_run's
            # client is: a client that fails to build (bad region, missing
            # credentials) must surface as the route's own error, not escape
            # as an opaque Lambda 500 after the daily slot was claimed.
            converse = boto3.client("bedrock-runtime", region_name=REGION).converse
        except Exception as exc:
            return _json(502, {"error": "ocr_upstream", "detail": str(exc)[:400]})
        result = door_ocr.handle_ocr(body, converse)
        if result.get("error") == "ocr_upstream":
            return _json(502, result)
        status = 200 if "computed_deadline" in result else 400
        if result.get("error") in ("summons_unreadable", "model_refused"):
            status = 422
        return _json(status, result)
    if path == "/api/awaiting" and method == "GET":
        return _json(200, list_awaiting())
    if path == "/api/run" and method == "POST":
        return start_run(body)
    if path.startswith("/api/run/"):
        rest = path[len("/api/run/") :]
        if rest.endswith("/decision") and method == "POST":
            return decide(rest[: -len("/decision")], body)
        if method == "GET":
            return get_run(rest)

    return _json(404, {"error": "no_such_route", "path": path, "method": method})
