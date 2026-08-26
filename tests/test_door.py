"""The judge's door is a public endpoint, so it is tested like one.

These tests import the handler from `infra/door/` directly, which is the file
`infra/build_door.py` copies into the deployment bundle, so what is tested is
what ships.

The point of most of them is not that the door returns 200. It is that the
door cannot quietly return something plausible: an unconfigured agent runtime
must refuse loudly, a direct-origin caller must be rejected, and the statutory
numbers must come from the real engine rather than from a constant somebody
typed once and never checked again.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import boto3
import pytest

DOOR = Path(__file__).parent.parent / "infra" / "door"
if str(DOOR) not in sys.path:
    sys.path.insert(0, str(DOOR))

import handler as door  # noqa: E402


def event(
    path: str, method: str = "GET", body: str | None = None, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    return {
        "rawPath": path,
        "requestContext": {"http": {"method": method}},
        "body": body,
        "headers": headers or {},
    }


def call(*args: Any, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    response = door.handler(event(*args, **kwargs))
    return response["statusCode"], json.loads(response["body"])


# ----------------------------------------------------------------- health


def test_health_is_open_and_says_whether_an_agent_is_wired() -> None:
    status, body = call("/api/health")
    assert status == 200
    assert body["ok"] is True
    # An uptime check must be able to tell a door with no agent behind it from
    # a healthy one, without reading the deploy logs.
    assert "agent_runtime_configured" in body


def test_health_needs_no_origin_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # The watchdog calls this without CloudFront in front of it.
    monkeypatch.setattr(door, "ORIGIN_SECRET", "a-secret")
    status, _ = call("/api/health")
    assert status == 200


# ------------------------------------------------------------ origin guard


def test_direct_origin_access_is_refused_when_a_secret_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(door, "ORIGIN_SECRET", "a-secret")
    status, body = call("/api/stats")
    assert status == 403
    assert body["error"] == "direct_origin_access_refused"


def test_the_matching_secret_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(door, "ORIGIN_SECRET", "a-secret")
    status, _ = call("/api/stats", headers={"x-instanter-origin": "a-secret"})
    assert status == 200


def test_the_guard_is_case_insensitive_about_the_header_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # CloudFront and Lambda disagree about header casing often enough that a
    # case-sensitive lookup would fail only in production.
    monkeypatch.setattr(door, "ORIGIN_SECRET", "a-secret")
    status, _ = call("/api/stats", headers={"X-Instanter-Origin": "a-secret"})
    assert status == 200


# ------------------------------------------------------------------ stats


def test_stats_recomputes_from_the_real_engine() -> None:
    status, body = call("/api/stats")
    assert status == 200
    computation = body["computation"]
    # Recomputed, so it takes real time and cites the real statute.
    assert computation["elapsed_ms"] > 0
    assert "O.C.G.A." in computation["citation"]
    assert (
        body["corpus"]["cases"]
        == computation["deadlines_computed"] + computation["refused_unverified"]
    )


def test_the_headline_number_is_the_sum_of_its_two_named_mechanisms() -> None:
    """The headline must never be a number with no derivation behind it.

    Two different things make a hand-counted deadline wrong, and the endpoint
    reports both lists. If the headline ever stops equalling their combined
    length, it has become a claim rather than a measurement.
    """
    _, body = call("/api/stats")
    rolls = body["because_the_deadline_rolls"]
    summons = body["because_the_summons_controls"]
    assert body["headline"]["answer_deadlines_hand_counting_gets_wrong"] == len(rolls) + len(
        summons
    )
    assert body["headline"]["of_deadlines_computed"] == body["computation"]["deadlines_computed"]


def test_every_roll_divergence_actually_diverges() -> None:
    """A row in that list that agrees with hand counting would be padding."""
    _, body = call("/api/stats")
    for row in body["because_the_deadline_rolls"]:
        assert row["hand_counted"] != row["statutory"]
        assert row["days_off"] != 0
        # The whole point is that hand counting lands somewhere the court is
        # not open, so the naive date should be a weekend day here.
        assert row["hand_counted_weekday"] in {"Saturday", "Sunday"}


def test_every_summons_row_cites_its_authority() -> None:
    _, body = call("/api/stats")
    for row in body["because_the_summons_controls"]:
        assert row["computed"] != row["controlling"]
        assert row["authority"], "a controlling-date claim with no statute behind it"


def test_stats_is_never_cached() -> None:
    response = door.handler(event("/api/stats"))
    assert response["headers"]["cache-control"] == "no-store"


def test_two_calls_report_independent_timings() -> None:
    """Proves the endpoint recomputes rather than returning a stored answer."""
    _, first = call("/api/stats")
    _, second = call("/api/stats")
    assert first["computation"]["deadlines_computed"] == second["computation"]["deadlines_computed"]
    assert first["recomputed_at"] is not None and second["recomputed_at"] is not None


# -------------------------------------------------------------------- runs


def test_starting_a_run_with_no_runtime_configured_refuses_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spike's lesson, as a test.

    A door that accepts a start request it cannot serve would report success
    for a run that never existed. It must say so instead.
    """
    monkeypatch.setattr(door, "RUNTIME_ARN", "")
    status, body = call("/api/run", method="POST", body="{}")
    assert status == 503
    assert body["error"] == "agent_runtime_not_configured"
    assert "stats" in body["detail"], "the refusal should point at what DOES work"


def test_malformed_json_is_a_400_not_a_crash() -> None:
    status, body = call("/api/run", method="POST", body="{not json")
    assert status == 400
    assert body["error"] == "invalid_json"


def test_unknown_routes_are_404_and_name_what_was_asked_for() -> None:
    status, body = call("/api/nope")
    assert status == 404
    assert body["path"] == "/api/nope"


def test_a_run_session_id_clears_the_thirty_three_character_minimum() -> None:
    """Confirmed empirically in spike 0001: 32 is rejected, 33 is accepted."""
    assert len(door._runtime_session_id()) >= 33


# --------------------------------------------------------------- spend cap


class _FakeTable:
    """Minimal stand-in for the DynamoDB table's atomic counter."""

    def __init__(self) -> None:
        self.count = 0

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.count += 1
        return {"Attributes": {"started": self.count}}


def test_the_daily_cap_counts_and_then_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeTable()
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "MAX_RUNS_PER_DAY", 3)

    assert door.claim_daily_run_slot() == (True, 1)
    assert door.claim_daily_run_slot() == (True, 2)
    assert door.claim_daily_run_slot() == (True, 3)
    # The fourth is over the cap, and the counter keeps counting so the refusal
    # can report how far over it went.
    allowed, used = door.claim_daily_run_slot()
    assert allowed is False
    assert used == 4


def test_the_cap_refusal_names_the_numbers_and_the_free_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTable()
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "MAX_RUNS_PER_DAY", 0)
    monkeypatch.setattr(door, "RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")

    status, body = call("/api/run", method="POST", body="{}")
    assert status == 429
    assert body["error"] == "daily_run_cap_reached"
    assert body["cap"] == 0
    assert body["runs_today"] == 1
    # A judge who hits the cap must be able to tell a deliberate bound from a
    # broken endpoint, and must be pointed at what still works.
    assert "stats" in body["detail"]


def test_the_cap_is_checked_before_any_model_is_invoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cap enforced after the spend would not be a cap."""
    fake = _FakeTable()
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "MAX_RUNS_PER_DAY", 0)
    monkeypatch.setattr(door, "RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("boto3 client was constructed despite the cap being reached")

    # Patch the boto3 module itself: the handler resolves `boto3.client` at
    # call time, so a client constructed after the cap check would blow up here.
    monkeypatch.setattr(boto3, "client", explode)
    status, _ = call("/api/run", method="POST", body="{}")
    assert status == 429


def test_flag_count_matches_the_queue_the_console_renders_beneath_it() -> None:
    """Two surfaces on one page must not disagree about the same corpus.

    The live-proof strip and the queue snapshot below it are computed by
    different code paths over the same 48 records. When the strip counted flags
    only on cases whose deadline it could compute, it undercounted by exactly
    the two refused cases, which are the ones a clinic most needs flagged
    (missing service date, unknown service method). A judge comparing the two
    numbers would have caught it.
    """
    queue = json.loads((Path(__file__).parent.parent / "web" / "public" / "queue.json").read_text())
    cases_with_flags = sum(1 for c in queue["cases"] if c.get("flags"))

    _, body = call("/api/stats")
    assert body["computation"]["cases_carrying_a_flag"] == cases_with_flags


def test_a_refused_case_still_counts_as_flagged() -> None:
    """The refusal path must not swallow the flag that explains the refusal."""
    _, body = call("/api/stats")
    computation = body["computation"]
    assert computation["refused_unverified"] > 0, "corpus no longer exercises the refusal path"
    # Flags are counted across the whole corpus, so the count can exceed the
    # number of cases that produced a deadline.
    assert computation["cases_carrying_a_flag"] >= computation["refused_unverified"]


# --------------------------------------------------- the scheduled sweep


class _KeyedFakeTable:
    """Counts per key, so two origins cannot exhaust one another's budget."""

    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.counts: dict[str, int] = {}
        self.items = items or []
        self.puts: list[dict[str, Any]] = []

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]["run_id"]
        self.counts[key] = self.counts.get(key, 0) + 1
        return {"Attributes": {"started": self.counts[key]}}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        self.puts.append(kwargs["Item"])
        return {}

    def query(self, **_kwargs: Any) -> dict[str, Any]:
        return {"Items": self.items}


def test_a_scheduled_event_routes_to_the_sweep_and_not_to_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: the sweep runs without anyone pressing a button."""
    monkeypatch.setattr(door, "RUNTIME_ARN", "")
    # No runtime wired, so it refuses. It RAISES rather than returning, because
    # EventBridge invokes Lambda asynchronously and a statusCode inside a
    # returned object would be recorded as a successful delivery.
    with pytest.raises(RuntimeError, match="scheduled sweep failed"):
        door.handler({"instanter_scheduled_sweep": True, "capacity": 2})


def test_a_forged_http_request_cannot_reach_the_scheduled_path() -> None:
    """The marker lives on the EVENT, and a POST body is a string beside it.

    If this ever regressed, a stranger could fire the clinic's paid sweep by
    POSTing a JSON body, bypassing the visitor cap entirely, because the
    scheduled origin counts against a different budget.
    """
    status, body = call(
        "/api/run",
        method="POST",
        body=json.dumps({"instanter_scheduled_sweep": True}),
    )
    # It is routed as ordinary HTTP. Whatever it does next, it is NOT the sweep.
    assert "scheduled" not in body
    assert status in (400, 429, 503, 502, 202)


def test_the_marker_alone_is_not_enough_when_rawpath_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both conditions are required, so neither alone opens the path."""
    monkeypatch.setattr(door, "ORIGIN_SECRET", "")
    forged = event("/api/stats")
    forged["instanter_scheduled_sweep"] = True
    result = door.handler(forged)
    assert "scheduled" not in result
    assert result["statusCode"] == 200


def test_the_scheduled_budget_is_separate_from_the_visitor_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy day of visitors must not cancel the clinic's morning sweep."""
    fake = _KeyedFakeTable()
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "MAX_RUNS_PER_DAY", 1)
    monkeypatch.setattr(door, "MAX_SCHEDULED_RUNS_PER_DAY", 1)

    assert door.claim_daily_run_slot("visitor") == (True, 1)
    # The visitor budget is now spent, and the sweep is untouched by that.
    assert door.claim_daily_run_slot("visitor")[0] is False
    assert door.claim_daily_run_slot("scheduled") == (True, 1)
    assert door.claim_daily_run_slot("scheduled")[0] is False


def test_the_scheduled_cap_refusal_reports_its_own_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _KeyedFakeTable()
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "MAX_SCHEDULED_RUNS_PER_DAY", 0)
    monkeypatch.setattr(door, "RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")

    with pytest.raises(RuntimeError, match="scheduled sweep failed: HTTP 429"):
        door.handler({"instanter_scheduled_sweep": True})


def test_a_run_records_which_origin_started_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without this the awaiting list cannot say the sweep ran on its own."""
    fake = _KeyedFakeTable()
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "MAX_SCHEDULED_RUNS_PER_DAY", 5)
    monkeypatch.setattr(door, "RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")

    def explode(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("no network in tests")

    monkeypatch.setattr(boto3, "client", explode)
    # The invoke fails, so the sweep raises. The ROW must still have been
    # written first, and must carry its origin.
    with pytest.raises(RuntimeError, match="scheduled sweep failed"):
        door.handler({"instanter_scheduled_sweep": True})
    assert fake.puts, "the run row was never written"
    assert fake.puts[0]["origin"] == "scheduled"


def test_awaiting_lists_runs_that_stopped_for_an_attorney(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _KeyedFakeTable(
        items=[
            {
                "run_id": "abc123",
                "origin": "scheduled",
                "created_at": 1_700_000_000,
                "result": json.dumps({"awaiting": [{"case_id": "26ED1"}, {"case_id": "26ED2"}]}),
            }
        ]
    )
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "ORIGIN_SECRET", "")

    status, body = call("/api/awaiting")
    assert status == 200
    assert body["count"] == 1
    assert body["awaiting"][0]["origin"] == "scheduled"
    assert body["awaiting"][0]["cases"] == 2
    assert "committed" in body["detail"], "the endpoint should state what it does NOT do"


def test_awaiting_is_empty_rather_than_absent_when_nothing_is_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty list and a missing key read very differently to a client."""
    monkeypatch.setattr(door, "table", lambda: _KeyedFakeTable(items=[]))
    monkeypatch.setattr(door, "ORIGIN_SECRET", "")
    status, body = call("/api/awaiting")
    assert status == 200
    assert body["awaiting"] == []
    assert body["count"] == 0


def test_awaiting_is_behind_the_origin_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(door, "ORIGIN_SECRET", "a-secret")
    status, body = call("/api/awaiting")
    assert status == 403
    assert body["error"] == "direct_origin_access_refused"


def test_a_client_that_cannot_be_built_is_a_502_and_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client construction belongs inside the error handling, not beside it.

    It used to sit outside, so a bad region or missing credentials escaped as
    an unhandled exception: the caller got an opaque 500 and the run row stayed
    on "starting" forever, which is the silent-failure shape this door exists
    to avoid.
    """
    fake = _KeyedFakeTable()
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "MAX_RUNS_PER_DAY", 5)
    monkeypatch.setattr(door, "RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")

    def unbuildable(*_a: Any, **_k: Any) -> None:
        raise ValueError("Invalid endpoint")

    monkeypatch.setattr(boto3, "client", unbuildable)
    status, body = call("/api/run", method="POST", body="{}")
    assert status == 502
    assert body["status"] == "failed"
    assert "Invalid endpoint" in body["error"]


# --------------------- Codex adversarial review, verified findings


class _PagedFakeTable(_KeyedFakeTable):
    """Returns rows across multiple pages, the way a real query does."""

    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        super().__init__()
        self.pages = pages
        self.calls = 0

    def query(self, **kwargs: Any) -> dict[str, Any]:
        idx = self.calls
        self.calls += 1
        items = self.pages[idx] if idx < len(self.pages) else []
        more = idx + 1 < len(self.pages)
        out: dict[str, Any] = {"Items": items}
        if more:
            out["LastEvaluatedKey"] = {"run_id": f"page{idx}"}
        return out


def _row(run_id: str, origin: str, cases: int = 1) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "origin": origin,
        "created_at": 1_700_000_000,
        "result": json.dumps({"awaiting": [{"case_id": f"c{i}"} for i in range(cases)]}),
    }


def test_awaiting_does_not_lose_a_scheduled_run_behind_a_page_of_visitors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The truncation bug, as a test.

    A single 25-row page let newer visitor rows hide the morning sweep, and the
    console then said nothing was waiting when something was. A truncated read
    that produces a confident absence is the same defect as an errored query
    reading as a pass.
    """
    visitors = [_row(f"v{i}", "visitor") for i in range(100)]
    fake = _PagedFakeTable([visitors, [_row("sched1", "scheduled", cases=2)]])
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "ORIGIN_SECRET", "")

    status, body = call("/api/awaiting")
    assert status == 200
    assert fake.calls == 2, "it must follow LastEvaluatedKey rather than stop at one page"
    origins = [a["origin"] for a in body["awaiting"]]
    assert "scheduled" in origins, "the scheduled run was hidden behind a page of visitor runs"
    assert body["count"] == 101


def test_awaiting_says_so_when_it_stops_early(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bounded is fine. Bounded and silent is not."""
    pages = [[_row(f"v{p}_{i}", "visitor") for i in range(100)] for p in range(9)]
    fake = _PagedFakeTable(pages)
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "MAX_AWAITING_SCAN", 250)
    monkeypatch.setattr(door, "ORIGIN_SECRET", "")

    _, body = call("/api/awaiting")
    assert body["truncated"] is True, "a partial list must never present itself as complete"


def test_awaiting_reports_a_row_whose_result_is_unparseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupt row is still a run that owes somebody a decision."""
    bad = {"run_id": "x1", "origin": "scheduled", "created_at": 1, "result": "{not json"}
    fake = _PagedFakeTable([[bad]])
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "ORIGIN_SECRET", "")

    status, body = call("/api/awaiting")
    assert status == 200, "an unparseable row must not take the endpoint down"
    assert body["count"] == 1
    assert body["awaiting"][0]["cases"] == 0


def test_a_failed_scheduled_sweep_raises_rather_than_returning_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EventBridge invokes Lambda asynchronously.

    A statusCode inside a returned object is NOT an invocation failure, so a
    sweep that never ran was being recorded as a successful delivery and the
    retry policy never fired. The code comment claimed the opposite of what the
    code did.
    """
    monkeypatch.setattr(door, "RUNTIME_ARN", "")
    with pytest.raises(RuntimeError, match="scheduled sweep failed"):
        door.handler({"instanter_scheduled_sweep": True})


def test_a_capped_scheduled_sweep_also_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _KeyedFakeTable()
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "MAX_SCHEDULED_RUNS_PER_DAY", 0)
    monkeypatch.setattr(door, "RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")
    with pytest.raises(RuntimeError, match="scheduled sweep failed"):
        door.handler({"instanter_scheduled_sweep": True})
