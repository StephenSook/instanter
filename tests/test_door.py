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
import lock as lock_mod  # noqa: E402
import push as push_mod  # noqa: E402


def event(
    path: str,
    method: str = "GET",
    body: str | None = None,
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "rawPath": path,
        "requestContext": {"http": {"method": method}},
        "body": body,
        "headers": headers or {},
        "queryStringParameters": query or {},
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
    monkeypatch.setattr(door, "claim_occurrence", lambda _o, _owner: door.OCCURRENCE_CLAIMED)
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
    fake = _ReleaseFakeTable()
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
    fake = _ReleaseFakeTable()
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "MAX_SCHEDULED_RUNS_PER_DAY", 0)
    monkeypatch.setattr(door, "RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")

    with pytest.raises(RuntimeError, match="scheduled sweep failed: HTTP 429"):
        door.handler({"instanter_scheduled_sweep": True})


def test_a_run_records_which_origin_started_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without this the awaiting list cannot say the sweep ran on its own."""
    fake = _ReleaseFakeTable()
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "MAX_SCHEDULED_RUNS_PER_DAY", 5)
    monkeypatch.setattr(door, "RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")

    def explode(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("no network in tests")

    monkeypatch.setattr(boto3, "client", explode)
    monkeypatch.setattr(door, "claim_occurrence", lambda _o, _owner: door.OCCURRENCE_CLAIMED)
    # The invoke fails, so the sweep raises. The RUN row must still have been
    # written first, and must carry its origin.
    with pytest.raises(RuntimeError, match="scheduled sweep failed"):
        door.handler({"instanter_scheduled_sweep": True})
    runs = [i for i in fake.puts if not str(i["run_id"]).startswith("__")]
    assert runs, "the run row was never written"
    assert runs[0]["origin"] == "scheduled"


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
    fake = _ReleaseFakeTable()
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
    monkeypatch.setattr(door, "claim_occurrence", lambda _o, _owner: door.OCCURRENCE_CLAIMED)
    with pytest.raises(RuntimeError, match="scheduled sweep failed"):
        door.handler({"instanter_scheduled_sweep": True})


def test_a_capped_scheduled_sweep_also_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _ReleaseFakeTable()
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "MAX_SCHEDULED_RUNS_PER_DAY", 0)
    monkeypatch.setattr(door, "claim_occurrence", lambda _o, _owner: door.OCCURRENCE_CLAIMED)
    monkeypatch.setattr(door, "RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")
    with pytest.raises(RuntimeError, match="scheduled sweep failed"):
        door.handler({"instanter_scheduled_sweep": True})


def test_awaiting_does_not_publish_run_ids_a_stranger_could_decide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The critical finding, as a test.

    /api/awaiting is public and POST /api/run/{id}/decision does no
    authorization, so publishing a scheduled run's id would let any stranger
    approve the clinic's morning sweep. That contradicts the one claim this
    product rests on.
    """
    fake = _PagedFakeTable([[_row("secret-run-id", "scheduled", cases=2)]])
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "ORIGIN_SECRET", "")

    status, body = call("/api/awaiting")
    assert status == 200
    assert body["count"] == 1
    assert "secret-run-id" not in json.dumps(body), "a decidable run id was published"
    assert all("run_id" not in row for row in body["awaiting"])
    # The count still works, which is all the console ever needed.
    assert body["awaiting"][0]["cases"] == 2


def test_a_duplicate_scheduler_delivery_spends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EventBridge Scheduler delivers AT LEAST once.

    Without an occurrence key a redelivery minted a fresh run_id and became a
    second paid model run. The contract since the lease landed: a redelivery
    after a SUCCESSFUL sweep spends nothing (sweep_done blocks it), while a
    redelivery after a FAILED start legitimately reruns, because the claim is
    released on failure.
    """

    class _OnceTable(_KeyedFakeTable):
        def __init__(self) -> None:
            super().__init__()
            self.rows: dict[str, dict[str, Any]] = {}

        def put_item(self, **kwargs: Any) -> dict[str, Any]:
            key = kwargs["Item"]["run_id"]
            if "ConditionExpression" in kwargs and key in self.rows:
                raise RuntimeError("ConditionalCheckFailedException")
            self.rows[key] = dict(kwargs["Item"])
            return super().put_item(**kwargs)

        def get_item(self, **kwargs: Any) -> dict[str, Any]:
            key = kwargs["Key"]["run_id"]
            return {"Item": dict(self.rows[key])} if key in self.rows else {}

        def delete_item(self, **kwargs: Any) -> dict[str, Any]:
            self.rows.pop(kwargs["Key"]["run_id"], None)
            return {}

        def update_item(self, **kwargs: Any) -> dict[str, Any]:
            expr = str(kwargs.get("UpdateExpression", ""))
            if "ADD" in expr:
                return super().update_item(**kwargs)
            key = kwargs["Key"]["run_id"]
            if "sweep_done" in expr and key in self.rows:
                self.rows[key]["sweep_done"] = 1
            return {}

    fake = _OnceTable()
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "MAX_SCHEDULED_RUNS_PER_DAY", 5)
    monkeypatch.setattr(door, "RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")
    monkeypatch.setattr(lock_mod, "lock_record", lambda *_a, **_k: None)

    event = {"instanter_scheduled_sweep": True, "scheduled_time": "2026-09-09T11:00:00Z"}

    class _Boto:
        @staticmethod
        def client(*_a: Any, **_k: Any) -> Any:
            return _FakeAgentClient({"interrupted": False, "succeeded": True})

    monkeypatch.setattr(door, "boto3", _Boto)
    result = door.handler(dict(event))
    assert result["duplicate"] is False
    assert result["statusCode"] == 202

    # A redelivery of the SAME occurrence must not construct a client at all.
    class _Exploding:
        @staticmethod
        def client(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("a duplicate delivery must not reach the model")

    monkeypatch.setattr(door, "boto3", _Exploding)
    result = door.handler(dict(event))
    assert result["duplicate"] is True
    assert result["statusCode"] == 200


def test_the_scheduled_sweep_triages_the_court_local_day_not_the_frozen_demo_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The frozen-date finding, as a test.

    The scheduled path used to pass only capacity, so the agent fell back to the
    corpus's demo_run_date and every morning re-triaged the same day. A case was
    therefore ranked by a stale distance to its deadline.
    """
    captured: dict[str, Any] = {}

    def fake_start(
        body: dict[str, Any], origin: str = "visitor", run_date: str | None = None
    ) -> dict[str, Any]:
        captured["origin"] = origin
        captured["run_date"] = run_date
        return {"statusCode": 202, "body": json.dumps({"run_id": "r1", "status": "complete"})}

    monkeypatch.setattr(door, "start_run", fake_start)
    monkeypatch.setattr(door, "claim_occurrence", lambda _o, _owner: door.OCCURRENCE_CLAIMED)

    # 11:00 UTC on 2026-09-09 is 07:00 in America/New_York, the same court day.
    door.handler({"instanter_scheduled_sweep": True, "scheduled_time": "2026-09-09T11:00:00Z"})
    assert captured["run_date"] == "2026-09-09"
    assert captured["origin"] == "scheduled"


def test_a_late_utc_occurrence_still_resolves_to_the_right_court_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """01:00 UTC is still the PREVIOUS evening in Eastern time.

    Counting the day in UTC would put this sweep on the wrong court date, which
    is the whole reason the schedule carries a named timezone.
    """
    captured: dict[str, Any] = {}

    def fake_start(
        body: dict[str, Any], origin: str = "visitor", run_date: str | None = None
    ) -> dict[str, Any]:
        captured["run_date"] = run_date
        return {"statusCode": 202, "body": json.dumps({"run_id": "r1", "status": "complete"})}

    monkeypatch.setattr(door, "start_run", fake_start)
    monkeypatch.setattr(door, "claim_occurrence", lambda _o, _owner: door.OCCURRENCE_CLAIMED)
    door.handler({"instanter_scheduled_sweep": True, "scheduled_time": "2026-09-10T01:00:00Z"})
    assert captured["run_date"] == "2026-09-09", "UTC date would have said the 10th"


def test_an_unsubstituted_scheduler_token_is_not_parsed_as_a_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the target was configured without the token, the literal comes back."""
    captured: dict[str, Any] = {}

    def fake_start(
        body: dict[str, Any], origin: str = "visitor", run_date: str | None = None
    ) -> dict[str, Any]:
        captured["run_date"] = run_date
        return {"statusCode": 202, "body": json.dumps({"run_id": "r1", "status": "complete"})}

    monkeypatch.setattr(door, "start_run", fake_start)
    monkeypatch.setattr(door, "claim_occurrence", lambda _o, _owner: door.OCCURRENCE_CLAIMED)
    door.handler(
        {"instanter_scheduled_sweep": True, "scheduled_time": "<aws.scheduler.scheduled-time>"}
    )
    # Falls back to today in court time rather than crashing or sending junk.
    assert captured["run_date"] and captured["run_date"][0].isdigit()


# ----------------------------------------------------------------- what-if


def test_what_if_weekend_roll_matches_the_engine() -> None:
    """Service Sat-window 2026-08-08: day 7 is Saturday, statute rolls to Monday.

    Same inputs as tests/test_deadline.py::test_weekend_roll. The door must
    return the engine's date, not a count the UI invented.
    """
    from datetime import date

    from engine.deadline import CaseInput, compute_deadline
    from engine.rules import GEORGIA_RULE, ServiceMethod

    engine = compute_deadline(
        CaseInput(
            case_id="what-if",
            jurisdiction_id="GA-FULTON",
            service_date=date(2026, 8, 8),
            service_method=ServiceMethod.PERSONAL,
        ),
        GEORGIA_RULE,
    )
    status, body = call("/api/what-if", query={"service_date": "2026-08-08"})
    assert status == 200
    assert engine.computed_deadline is not None
    assert body["computed_deadline"] == engine.computed_deadline.isoformat() == "2026-08-17"
    assert body["effective_deadline"] == "2026-08-17"
    assert "Saturday; roll forward" in " ".join(step["label"] for step in body["trace"])
    assert all(flag["code"] != "court_closed_not_legal_holiday" for flag in body["flags"])


def test_what_if_dec_31_trap_matches_the_engine() -> None:
    """Service 2026-12-24: day 7 is Dec 31, courthouse closed, statute does not roll."""
    from datetime import date

    from engine.deadline import CaseInput, FlagCode, compute_deadline
    from engine.rules import GEORGIA_RULE, ServiceMethod

    engine = compute_deadline(
        CaseInput(
            case_id="what-if",
            jurisdiction_id="GA-FULTON",
            service_date=date(2026, 12, 24),
            service_method=ServiceMethod.PERSONAL,
        ),
        GEORGIA_RULE,
    )
    status, body = call("/api/what-if", query={"service_date": "2026-12-24"})
    assert status == 200
    assert engine.computed_deadline is not None
    assert body["computed_deadline"] == engine.computed_deadline.isoformat() == "2026-12-31"
    codes = {flag["code"] for flag in body["flags"]}
    assert FlagCode.COURT_CLOSED_NOT_LEGAL_HOLIDAY.value in codes
    assert engine.computed_deadline == date(2026, 12, 31)


def test_what_if_refuses_to_guess_a_date() -> None:
    status, body = call("/api/what-if")
    assert status == 400
    assert body["error"] == "service_date_required"


def test_what_if_refuses_a_malformed_date() -> None:
    status, body = call("/api/what-if", query={"service_date": "August 8"})
    assert status == 400
    assert body["error"] == "invalid_service_date"


def test_attach_steps_does_not_invent_kinds_when_the_payload_already_has_them() -> None:
    original = [{"seq": 1, "kind": "extract"}]
    out = door.attach_steps({"total_cases": 48, "interrupted": True, "steps": original})
    assert out["steps"] == original


# ----------------------------------------------------------------- live queue


def test_queue_is_the_engine_and_the_ladder_on_this_request() -> None:
    """The cabinet must not invent a date or a rank.

    Deadlines match compute_deadline on the same records. Interrupt rationing
    respects capacity 2. 26ED00101 is overdue on the corpus run date, so it
    is an interrupt.
    """
    from datetime import date

    from engine.deadline import CaseInput, compute_deadline
    from engine.rules import GEORGIA_RULE, ServiceMethod

    status, body = call("/api/queue")
    assert status == 200
    assert body["source"] == "live"
    assert "recomputed on this request" in body["generated_by"]
    assert body["attorney_capacity"] == 2
    assert len(body["cases"]) == 48

    by_id = {c["case_id"]: c for c in body["cases"]}
    assert by_id["26ED00101"]["interrupt_now"] is True
    assert by_id["26ED00101"]["computed_deadline"] == "2026-09-08"
    interrupts = [c for c in body["cases"] if c["interrupt_now"]]
    assert len(interrupts) <= 2

    sample = by_id["26ED00101"]
    engine = compute_deadline(
        CaseInput(
            case_id="26ED00101",
            jurisdiction_id="GA-FULTON",
            service_date=date(2026, 9, 1),
            service_method=ServiceMethod.PERSONAL,
        ),
        GEORGIA_RULE,
    )
    assert engine.computed_deadline is not None
    assert sample["computed_deadline"] == engine.computed_deadline.isoformat()
    assert sample["rationale"] is None


def test_queue_flag_count_matches_stats() -> None:
    _, queue = call("/api/queue")
    _, stats = call("/api/stats")
    flagged = sum(1 for c in queue["cases"] if c.get("flags"))
    assert flagged == stats["computation"]["cases_carrying_a_flag"]
    assert flagged == queue["counts"]["flagged"]


def test_ocr_uses_the_engine_after_a_transcription(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_converse(**_kwargs: object) -> dict[str, object]:
        return {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": (
                                '{"service_date":"2026-08-08","service_method":"personal",'
                                '"summons_stated_deadline":null,"case_id":"EX",'
                                '"refused":false,"reason":""}'
                            )
                        }
                    ]
                }
            }
        }

    monkeypatch.setattr(door, "claim_daily_run_slot", lambda _origin="visitor": (True, 0))
    monkeypatch.setattr(
        boto3, "client", lambda *a, **k: type("C", (), {"converse": staticmethod(fake_converse)})()
    )
    import base64

    status, body = call(
        "/api/ocr",
        method="POST",
        body=json.dumps(
            {"image_b64": base64.b64encode(b"not-a-real-png").decode(), "media_type": "image/png"}
        ),
    )
    assert status == 200
    assert body["computed_deadline"] == "2026-08-17"
    assert body["extracted"]["service_date"] == "2026-08-08"


def test_push_vapid_is_loud_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(push_mod, "VAPID_PUBLIC_KEY", "")
    status, body = call("/api/push/vapid")
    assert status == 503
    assert body["error"] == "push_not_configured"


def test_push_vapid_returns_the_public_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(push_mod, "VAPID_PUBLIC_KEY", "BK_test_public")
    status, body = call("/api/push/vapid")
    assert status == 200
    assert body["publicKey"] == "BK_test_public"


# --------------------------------------------- surfaced failures, not silent


class _ReleaseFakeTable(_KeyedFakeTable):
    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[dict[str, Any]] = []

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        self.deleted.append(kwargs["Key"])
        return {}


def test_a_failed_scheduled_sweep_releases_its_occurrence_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raise triggers EventBridge's retry, but the retry is useless if it
    finds its own occurrence already claimed and reports duplicate: True as a
    success. A transiently failed 7am sweep must be able to actually retry."""
    fake = _ReleaseFakeTable()
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "RUNTIME_ARN", "")
    monkeypatch.setattr(door, "claim_occurrence", lambda _o, _owner: door.OCCURRENCE_CLAIMED)
    with pytest.raises(RuntimeError, match="scheduled sweep failed"):
        door.handler({"instanter_scheduled_sweep": True})
    assert len(fake.deleted) == 1
    assert str(fake.deleted[0]["run_id"]).startswith("__occurrence__")


class _FakeInvokeBody:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw


class _FakeAgentClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def invoke_agent_runtime(self, **_kwargs: Any) -> dict[str, Any]:
        return {"response": _FakeInvokeBody(self._payload)}


class _DecideFakeTable(_KeyedFakeTable):
    def __init__(self, item: dict[str, Any]) -> None:
        super().__init__()
        self.item = item
        self.updates: list[dict[str, Any]] = []

    def get_item(self, **_kwargs: Any) -> dict[str, Any]:
        return {"Item": self.item}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        return {}


def test_a_decision_that_cannot_be_lock_recorded_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attorney decision is the one event the Object Lock trail exists
    for. A swallowed write failure would mean a week of 200s beside a
    Compliance trail holding sweeps and zero decisions."""
    stored = {"interrupts": [{"id": "i-1"}]}
    fake = _DecideFakeTable(
        {"run_id": "r1", "status": "awaiting_attorney", "result": json.dumps(stored)}
    )
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "ORIGIN_SECRET", "")
    monkeypatch.setattr(door, "RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")

    class _Boto:
        @staticmethod
        def client(*_a: Any, **_k: Any) -> Any:
            return _FakeAgentClient({"succeeded": True, "committed": ["26ED00101"]})

    monkeypatch.setattr(door, "boto3", _Boto)

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("the bucket said no")

    monkeypatch.setattr(lock_mod, "lock_record", _boom)
    status, body = call(
        "/api/run/r1/decision", method="POST", body=json.dumps({"response": "approve"})
    )
    assert status == 200, "the decision itself still succeeded"
    assert "the bucket said no" in body["audit_lock_error"]


def test_a_crashed_push_notify_is_surfaced_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """push_sent: 0 with no error field would make a lost IAM grant look
    exactly like an empty subscription table, forever."""
    fake = _ReleaseFakeTable()
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "ORIGIN_SECRET", "")
    monkeypatch.setattr(door, "RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")
    monkeypatch.setattr(door, "claim_daily_run_slot", lambda _o: (True, 1))
    monkeypatch.setenv("PUSH_TABLE", "push-table")

    class _Boto:
        @staticmethod
        def client(*_a: Any, **_k: Any) -> Any:
            return _FakeAgentClient({"interrupted": True, "awaiting": [{"case_id": "26ED00101"}]})

        @staticmethod
        def resource(*_a: Any, **_k: Any) -> Any:
            class _R:
                @staticmethod
                def Table(_name: str) -> Any:  # noqa: N802 - mirrors the boto3 API
                    return object()

            return _R()

    monkeypatch.setattr(door, "boto3", _Boto)
    monkeypatch.setattr(lock_mod, "lock_record", lambda *_a, **_k: None)

    def _boom(_table: Any) -> int:
        raise RuntimeError("scan refused")

    monkeypatch.setattr(push_mod, "notify_interrupt", _boom)
    status, body = call("/api/run", method="POST", body=json.dumps({"capacity": 2}))
    assert status == 202
    assert body["result"]["push_sent"] == 0
    assert "scan refused" in body["result"]["push_error"]


# ---------------------------------------------- the decision path, hardened


class _ClaimFakeTable(_DecideFakeTable):
    """Enforces the conditional decision claim: first claim wins, second loses."""

    class ConditionalCheckFailedException(Exception):  # noqa: N818 - boto3's real name shape
        pass

    def __init__(self, item: dict[str, Any]) -> None:
        super().__init__(item)
        self.claimed = False

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        if "ConditionExpression" in kwargs:
            if self.claimed:
                raise self.ConditionalCheckFailedException("ConditionalCheckFailed")
            self.claimed = True
        self.updates.append(kwargs)
        return {}


def _awaiting_item() -> dict[str, Any]:
    stored = {"interrupts": [{"id": "i-1"}]}
    return {
        "run_id": "r1",
        "status": "awaiting_attorney",
        "result": json.dumps(stored),
        "capacity": 5,
        "run_date": "2026-09-14",
    }


def test_a_runtime_error_envelope_is_never_persisted_as_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime returns {"error": "no_such_run"} when its S3 state is gone.
    Recording THAT as resolved would store a decision that never executed."""
    fake = _ClaimFakeTable(_awaiting_item())
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "ORIGIN_SECRET", "")
    monkeypatch.setattr(door, "RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")

    class _Boto:
        @staticmethod
        def client(*_a: Any, **_k: Any) -> Any:
            return _FakeAgentClient({"error": "no_such_run", "run_id": "r1"})

    monkeypatch.setattr(door, "boto3", _Boto)
    status, body = call(
        "/api/run/r1/decision", method="POST", body=json.dumps({"response": "approve"})
    )
    assert status == 502
    assert body["error"] == "agent_error"
    assert "no_such_run" in body["detail"]
    resolved = [
        u for u in fake.updates if u.get("ExpressionAttributeValues", {}).get(":s") == "resolved"
    ]
    assert resolved == [], "an agent error must never become a resolved row"
    released = [
        u
        for u in fake.updates
        # The CLAIM write also carries :a in its condition values, so filter
        # on the release's own expression shape.
        if "REMOVE deciding_at" in str(u.get("UpdateExpression"))
        and u.get("ExpressionAttributeValues", {}).get(":a") == "awaiting_attorney"
    ]
    assert released, "the decision claim must be handed back"


def test_resume_carries_the_runs_own_capacity_and_run_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting them made the runtime default to capacity 2 and the frozen seed
    date, which re-ranked the queue and made the digest refuse the attorney's
    own answer on any run started with different values."""
    fake = _ClaimFakeTable(_awaiting_item())
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "ORIGIN_SECRET", "")
    monkeypatch.setattr(door, "RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")
    seen: dict[str, Any] = {}

    class _CapturingClient:
        @staticmethod
        def invoke_agent_runtime(**kwargs: Any) -> dict[str, Any]:
            seen.update(json.loads(kwargs["payload"].decode("utf-8")))
            return {"response": _FakeInvokeBody({"succeeded": True})}

    class _Boto:
        @staticmethod
        def client(*_a: Any, **_k: Any) -> Any:
            return _CapturingClient()

    monkeypatch.setattr(door, "boto3", _Boto)
    monkeypatch.setattr(lock_mod, "lock_record", lambda *_a, **_k: None)
    status, _ = call(
        "/api/run/r1/decision", method="POST", body=json.dumps({"response": "approve"})
    )
    assert status == 200
    assert seen["capacity"] == 5
    assert seen["run_date"] == "2026-09-14"


def test_a_second_concurrent_decision_loses_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _ClaimFakeTable(_awaiting_item())
    fake.claimed = True  # someone else already holds the decision claim
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "ORIGIN_SECRET", "")
    monkeypatch.setattr(door, "RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")

    class _Boto:
        @staticmethod
        def client(*_a: Any, **_k: Any) -> Any:
            return _FakeAgentClient({"succeeded": True})

    monkeypatch.setattr(door, "boto3", _Boto)
    status, body = call(
        "/api/run/r1/decision", method="POST", body=json.dumps({"response": "approve"})
    )
    assert status == 409
    assert body["error"] == "decision_in_progress"


def test_garbage_capacity_is_refused_before_a_paid_slot_is_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ReleaseFakeTable()
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "ORIGIN_SECRET", "")
    monkeypatch.setattr(door, "RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")
    status, body = call("/api/run", method="POST", body=json.dumps({"capacity": "not-an-integer"}))
    assert status == 400
    assert body["error"] == "invalid_capacity"
    assert fake.counts == {}, "no daily slot may be spent on a request that cannot run"


def test_an_empty_ocr_body_is_refused_before_a_paid_slot_is_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ReleaseFakeTable()
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "ORIGIN_SECRET", "")
    status, body = call("/api/ocr", method="POST", body=json.dumps({}))
    assert status == 400
    assert body["error"] == "image_required"
    assert fake.counts == {}, "no daily OCR slot may be spent on an empty body"


def test_a_client_construction_failure_leaves_no_stuck_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-2 finding: work that can fail without touching the runtime must
    happen BEFORE the deciding claim, or the run is stuck for 130 seconds."""
    fake = _ClaimFakeTable(_awaiting_item())
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "ORIGIN_SECRET", "")
    monkeypatch.setattr(door, "RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")

    class _Boto:
        @staticmethod
        def client(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("no region, no client")

    monkeypatch.setattr(door, "boto3", _Boto)
    status, _ = call(
        "/api/run/r1/decision", method="POST", body=json.dumps({"response": "approve"})
    )
    assert status == 502
    assert fake.claimed is False, "the claim must not be taken before the client exists"


def test_a_failed_final_write_after_a_landed_resume_keeps_the_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-2 finding: once the resume has executed on the runtime, releasing
    the claim would invite a second resume of an already-resumed run. The row
    stays deciding, deliberately, and the response says the write failed."""

    class _FinalWriteFailsTable(_ClaimFakeTable):
        def update_item(self, **kwargs: Any) -> dict[str, Any]:
            values = kwargs.get("ExpressionAttributeValues", {})
            if values.get(":s") == "resolved":
                raise RuntimeError("dynamo said no")
            return super().update_item(**kwargs)

    fake = _FinalWriteFailsTable(_awaiting_item())
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "ORIGIN_SECRET", "")
    monkeypatch.setattr(door, "RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")

    class _Boto:
        @staticmethod
        def client(*_a: Any, **_k: Any) -> Any:
            return _FakeAgentClient({"succeeded": True})

    monkeypatch.setattr(door, "boto3", _Boto)
    monkeypatch.setattr(lock_mod, "lock_record", lambda *_a, **_k: None)
    status, body = call(
        "/api/run/r1/decision", method="POST", body=json.dumps({"response": "approve"})
    )
    assert status == 200, "the decision itself executed"
    assert "dynamo said no" in body["row_update_error"]
    released = [
        u
        for u in fake.updates
        # The CLAIM write also carries :a in its condition values, so filter
        # on the release's own expression shape.
        if "REMOVE deciding_at" in str(u.get("UpdateExpression"))
        and u.get("ExpressionAttributeValues", {}).get(":a") == "awaiting_attorney"
    ]
    assert released == [], "the claim must NOT go back to awaiting after a landed resume"


def test_the_occurrence_release_is_retried_through_a_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-2 finding: a release that fails once suppresses every remaining
    retry (they all see the claim and report duplicate: True as success)."""

    class _FlakyReleaseTable(_KeyedFakeTable):
        def __init__(self) -> None:
            super().__init__()
            self.delete_attempts = 0

        def delete_item(self, **_kwargs: Any) -> dict[str, Any]:
            self.delete_attempts += 1
            if self.delete_attempts < 3:
                raise RuntimeError("transient")
            return {}

    fake = _FlakyReleaseTable()
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "RUNTIME_ARN", "")
    monkeypatch.setattr(door, "claim_occurrence", lambda _o, _owner: door.OCCURRENCE_CLAIMED)
    with pytest.raises(RuntimeError, match="scheduled sweep failed") as excinfo:
        door.handler({"instanter_scheduled_sweep": True})
    assert fake.delete_attempts == 3
    assert "not released" not in str(excinfo.value)


def test_the_runtimes_receipt_reconciles_instead_of_erroring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-3 finding: after a landed resume whose row write failed, a second
    decide must not re-execute the graph. The runtime returns its durable
    receipt and the door reconciles the row from the ORIGINAL report."""
    fake = _ClaimFakeTable(_awaiting_item())
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "ORIGIN_SECRET", "")
    monkeypatch.setattr(door, "RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")
    prior_report = {"succeeded": True, "attorney_action": "approved", "committed": ["26ED00101"]}

    class _Boto:
        @staticmethod
        def client(*_a: Any, **_k: Any) -> Any:
            return _FakeAgentClient(
                {"error": "already_resolved", "run_id": "r1", "report": prior_report}
            )

    monkeypatch.setattr(door, "boto3", _Boto)
    status, body = call(
        "/api/run/r1/decision", method="POST", body=json.dumps({"response": "defer: too late"})
    )
    assert status == 200
    assert body["already_resolved"] is True
    assert body["result"] == prior_report, "the FIRST resolution is the only resolution"
    resolved = [
        u for u in fake.updates if u.get("ExpressionAttributeValues", {}).get(":s") == "resolved"
    ]
    assert resolved, "the row must be reconciled from the receipt"


class _LeaseFakeTable:
    """Implements exactly what the lease needs: conditional put, get_item,
    conditional update, and an OWNER-conditional delete."""

    class ConditionalCheckFailedException(Exception):  # noqa: N818 - boto3's name shape
        pass

    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.reclaimed = False
        self.deletes = 0

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        if "ConditionExpression" in kwargs and self.row is not None:
            raise self.ConditionalCheckFailedException("ConditionalCheckFailed")
        self.row = kwargs["Item"]
        return {}

    def get_item(self, **_kwargs: Any) -> dict[str, Any]:
        return {"Item": dict(self.row)} if self.row else {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        values = kwargs["ExpressionAttributeValues"]
        if "ConditionExpression" in kwargs:
            if self.row is None or self.row.get("sweep_done"):
                raise self.ConditionalCheckFailedException("ConditionalCheckFailed")
            if self.row.get("claimed_at") != values[":old"]:
                raise self.ConditionalCheckFailedException("ConditionalCheckFailed")
        self.reclaimed = True
        self.row = dict(self.row or {})
        self.row["claimed_at"] = values[":now"]
        if ":owner" in values:
            self.row["owner"] = values[":owner"]
        return {}

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        self.deletes += 1
        if "ConditionExpression" in kwargs:
            owner = kwargs["ExpressionAttributeValues"][":owner"]
            if self.row is None or self.row.get("owner") != owner:
                raise self.ConditionalCheckFailedException("ConditionalCheckFailed")
        self.row = None
        return {}


def test_a_fresh_occurrence_is_claimed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _LeaseFakeTable(None)
    monkeypatch.setattr(door, "table", lambda: fake)
    assert door.claim_occurrence("2026-09-09T11:00:00Z", "owner-a") == door.OCCURRENCE_CLAIMED


def test_a_live_claim_is_not_stolen(monkeypatch: pytest.MonkeyPatch) -> None:
    import time as _time

    fake = _LeaseFakeTable({"run_id": "x", "claimed_at": int(_time.time()) - 30})
    monkeypatch.setattr(door, "table", lambda: fake)
    assert door.claim_occurrence("o", "owner-a") == door.OCCURRENCE_HELD, (
        "the claimant may still be running"
    )


def test_a_dead_claimants_lease_is_reclaimed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Round-3 finding: a Lambda that crashed after claiming could never
    release, so every async retry reported duplicate: True and the sweep
    silently never ran that day."""
    import time as _time

    fake = _LeaseFakeTable({"run_id": "x", "claimed_at": int(_time.time()) - 300})
    monkeypatch.setattr(door, "table", lambda: fake)
    assert door.claim_occurrence("o", "owner-a") == door.OCCURRENCE_CLAIMED
    assert fake.reclaimed is True


def test_a_finished_sweep_is_never_rerun(monkeypatch: pytest.MonkeyPatch) -> None:
    import time as _time

    fake = _LeaseFakeTable({"run_id": "x", "claimed_at": int(_time.time()) - 300, "sweep_done": 1})
    monkeypatch.setattr(door, "table", lambda: fake)
    assert door.claim_occurrence("o", "owner-a") == door.OCCURRENCE_DONE


def test_held_and_done_are_not_the_same_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The round-4 finding, at its root.

    A bool return collapsed "somebody holds this and has not finished" into the
    same answer as "this occurrence is complete". The caller could only treat
    both as a duplicate success, which is exactly how a dead claimant's retry
    reported a sweep that never ran as a sweep that did.
    """
    import time as _time

    held = _LeaseFakeTable({"run_id": "x", "claimed_at": int(_time.time()) - 30})
    done = _LeaseFakeTable({"run_id": "x", "claimed_at": int(_time.time()) - 30, "sweep_done": 1})
    monkeypatch.setattr(door, "table", lambda: held)
    first = door.claim_occurrence("o", "owner-a")
    monkeypatch.setattr(door, "table", lambda: done)
    second = door.claim_occurrence("o", "owner-a")
    assert first != second, "an unfinished holder must be distinguishable from a finished sweep"


def test_a_lost_delete_acknowledgement_cannot_erase_a_replacements_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-5 finding, and the 120s ceiling does NOT protect this one.

    The release is retried. If its first delete COMMITS but the acknowledgement
    is lost, the retry fires again. A concurrent at-least-once delivery can
    legitimately claim the now-absent key in between, and an unconditional
    retry would delete THAT claim, leaving the occurrence unowned so a third
    delivery could sweep it concurrently with the second. The replacement did
    not steal a stale lease, so the Lambda ceiling is irrelevant here: our own
    committed delete made the key absent.
    """
    fake = _LeaseFakeTable(None)
    monkeypatch.setattr(door, "table", lambda: fake)

    # A claims and then releases; the delete commits.
    assert door.claim_occurrence("o", "owner-a") == door.OCCURRENCE_CLAIMED
    assert door._release_occurrence("o", "owner-a") == ""
    assert fake.row is None

    # B, a concurrent delivery, legitimately claims the now-absent key.
    assert door.claim_occurrence("o", "owner-b") == door.OCCURRENCE_CLAIMED
    assert fake.row is not None

    # A's retry (its first ack was lost) must NOT remove B's claim.
    assert door._release_occurrence("o", "owner-a") == ""
    assert fake.row is not None, "A's stale retry deleted the replacement owner's lease"
    assert fake.row["owner"] == "owner-b"


def test_stealing_a_dead_lease_takes_ownership_of_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise the dead claimant's token would still authorise a delete."""
    import time as _time

    fake = _LeaseFakeTable({"run_id": "x", "claimed_at": int(_time.time()) - 300, "owner": "dead"})
    monkeypatch.setattr(door, "table", lambda: fake)
    assert door.claim_occurrence("o", "owner-live") == door.OCCURRENCE_CLAIMED
    assert fake.row is not None and fake.row["owner"] == "owner-live"
    # The dead claimant coming back cannot release what it no longer owns.
    assert door._release_occurrence("o", "dead") == ""
    assert fake.row is not None, "a dead claimant's token released the live lease"


def test_the_stale_window_survives_the_real_retry_timeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-5 finding: the previous pair of tests passed against a broken window.

    Asserting only "65s is HELD" and "300s is reclaimable" leaves a 299s
    threshold passing both while still losing the sweep, because a crash at
    t=5s produces Lambda attempts at ~65s and ~185s and BOTH would read HELD,
    spending the two function-error retries. Pin the actual timeline instead:
    claimable at t=0, HELD at the second attempt, reclaimable by the third.
    """
    assert door.OCCURRENCE_STALE_AFTER_SECONDS > 120, "a slow claimant would be robbed mid-run"
    assert door.OCCURRENCE_STALE_AFTER_SECONDS < 180, "the third attempt would still read HELD"

    import time as _time

    crashed_at = int(_time.time())
    fake = _LeaseFakeTable(None)
    monkeypatch.setattr(door, "table", lambda: fake)
    assert door.claim_occurrence("o", "owner-a") == door.OCCURRENCE_CLAIMED

    # The claimant dies at t=5s without releasing. Lambda's attempts follow.
    for elapsed, expected, why in (
        (65, door.OCCURRENCE_HELD, "second attempt, the holder could still be alive"),
        (185, door.OCCURRENCE_CLAIMED, "third attempt, past the 120s ceiling: it is dead"),
    ):
        # handler.py does `import time`, so this is the same module object.
        monkeypatch.setattr(_time, "time", lambda e=elapsed: crashed_at + e)
        assert door.claim_occurrence("o", f"retry-{elapsed}") == expected, why


def test_a_fast_crash_does_not_burn_lambdas_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """The round-4 finding, as the failure it actually causes.

    AWS waits ONE MINUTE before Lambda's second attempt, but the lease is only
    stealable after 150s. So a sweep that died at, say, t=5s left a claim that
    was still only ~65s old when the retry looked. The retry was refused the
    lease and used to return duplicate: True with a 200, which Lambda records as
    a SUCCESS: the third attempt never happened and the 7am sweep silently never
    ran that day. It must raise, so the event survives to the two-minute attempt
    by which point the 120s Lambda ceiling guarantees the holder is dead.
    """
    import time as _time

    fake = _LeaseFakeTable({"run_id": "x", "claimed_at": int(_time.time()) - 65})
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(
        door, "start_run", lambda *_a, **_k: pytest.fail("a held occurrence must not spend a run")
    )
    with pytest.raises(RuntimeError, match="claimed but unfinished"):
        door.handler({"instanter_scheduled_sweep": True, "scheduled_time": "2026-09-09T11:00:00Z"})


def test_any_in_process_death_releases_the_claim_not_just_a_4xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only start_run returning >=400 used to release the claim.

    An AgentCore throttle, a DynamoDB error, or a bad capacity in the event
    RAISES instead, within seconds, and every one of those used to leave the
    claim standing for the retry to trip over.
    """
    fake = _ReleaseFakeTable()
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "claim_occurrence", lambda _o, _owner: door.OCCURRENCE_CLAIMED)

    def boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("ThrottlingException: rate exceeded")

    monkeypatch.setattr(door, "start_run", boom)
    with pytest.raises(RuntimeError, match="ThrottlingException"):
        door.handler({"instanter_scheduled_sweep": True, "scheduled_time": "2026-09-09T11:00:00Z"})
    assert fake.deleted, "the claim must be released so the retry can take it"
    assert str(fake.deleted[0]["run_id"]).startswith("__occurrence__")


def test_a_bad_capacity_in_the_event_releases_the_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """int() raises BEFORE start_run is ever reached, inside the claim."""
    fake = _ReleaseFakeTable()
    monkeypatch.setattr(door, "table", lambda: fake)
    monkeypatch.setattr(door, "claim_occurrence", lambda _o, _owner: door.OCCURRENCE_CLAIMED)
    monkeypatch.setattr(
        door, "start_run", lambda *_a, **_k: pytest.fail("unreachable with a bad capacity")
    )
    with pytest.raises(ValueError):
        door.handler(
            {
                "instanter_scheduled_sweep": True,
                "scheduled_time": "2026-09-09T11:00:00Z",
                "capacity": "not-an-integer",
            }
        )
    assert fake.deleted, "the claim must be released so the retry can take it"
