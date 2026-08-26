"""The judge's door: a public, credential-free front door to Instanter.

ADR-0006 explains why this exists. `InvokeAgentRuntime` accepts only IAM SigV4
or an OAuth bearer token, so a browser cannot reach the agent at all. This
function holds the credential and invokes the runtime server-side.

Four endpoints, and only one of them is the point:

* ``GET  /api/health``          liveness, open, so an uptime check needs no secret.
* ``GET  /api/stats``           **the checkable number.** Recomputes every answer
                                deadline in the corpus with the real engine, on
                                request, and reports how many of them hand
                                counting gets wrong. No cache, no stored answer.
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

import json
import os
import time
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

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


# --------------------------------------------------------------------- runs


def _runtime_session_id() -> str:
    # runtimeSessionId has a 33 character MINIMUM (confirmed empirically in
    # spike 0001: 32 is rejected, 33 is accepted).
    return f"door-{uuid.uuid4().hex}-{uuid.uuid4().hex[:8]}"


def start_run(body: dict[str, Any], origin: str = "visitor") -> dict[str, Any]:
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
            "capacity": int(body.get("capacity", 2)),
        }
    )

    payload = {"action": "start", "run_id": run_id, "capacity": int(body.get("capacity", 2))}
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
        result = json.loads(response["response"].read())
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

    client = boto3.client("bedrock-agentcore", region_name=REGION)
    payload = {
        "action": "resume",
        "run_id": run_id,
        "interrupt_id": interrupts[0]["id"],
        "response": answer,
    }
    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            runtimeSessionId=_runtime_session_id(),
            payload=json.dumps(payload).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
        result = json.loads(response["response"].read())
    except Exception as exc:
        return _json(502, {"run_id": run_id, "error": str(exc)[:400]})

    table().update_item(
        Key={"run_id": run_id},
        UpdateExpression="SET #s = :s, #r = :r, #a = :a",
        ExpressionAttributeNames={"#s": "status", "#r": "result", "#a": "attorney_response"},
        ExpressionAttributeValues={
            ":s": "resolved",
            ":r": json.dumps(result, default=str),
            ":a": answer[:2000],
        },
    )
    return _json(200, {"run_id": run_id, "status": "resolved", "result": result})


# ------------------------------------------------------- the morning sweep


def list_awaiting() -> dict[str, Any]:
    """Runs that stopped and are still waiting on a licensed attorney.

    Queried through a sparse index on `status`, so the daily counter rows,
    which carry no status, are absent by construction rather than by filter.
    """
    result = table().query(
        IndexName=AWAITING_INDEX,
        KeyConditionExpression=Key("status").eq("awaiting_attorney"),
        ScanIndexForward=False,
        Limit=25,
    )
    waiting = []
    for item in result.get("Items", []):
        raw = item.get("result")
        parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
        waiting.append(
            {
                "run_id": item["run_id"],
                "origin": item.get("origin", "visitor"),
                "created_at": int(item.get("created_at", 0)),
                "cases": len(parsed.get("awaiting") or []),
            }
        )
    return {
        "awaiting": waiting,
        "count": len(waiting),
        # Said plainly because it is the product's whole claim: the sweep runs
        # whether or not anyone opened the page, and it stops rather than
        # deciding.
        "detail": (
            "Runs that computed every deadline, ranked the queue, and then "
            "stopped. Nothing in them is committed until an attorney answers."
        ),
    }


def scheduled_sweep(event: dict[str, Any]) -> dict[str, Any]:
    """The 7am sweep, fired by EventBridge Scheduler rather than by a click.

    The README says a walk-in clinic cannot watch every clock. Until this
    existed the agent only ran when a human pressed a button, which is the one
    thing the pitch says nobody has time to do. It ends the same way a visitor
    run does: at the attorney interrupt, with nothing committed.
    """
    body = {"capacity": int(event.get("capacity", 2))}
    response = start_run(body, origin="scheduled")
    payload = json.loads(response["body"])
    # Returned rather than logged only, so a failed sweep shows up as a failed
    # Scheduler invocation and can be alarmed on.
    return {
        "scheduled": True,
        "statusCode": response["statusCode"],
        "run_id": payload.get("run_id"),
        "status": payload.get("status"),
        "error": payload.get("error"),
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
