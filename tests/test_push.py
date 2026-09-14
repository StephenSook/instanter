"""Web Push admission and bounded interrupt delivery."""

from __future__ import annotations

import base64
import json
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

DOOR = Path(__file__).parent.parent / "infra" / "door"
if str(DOOR) not in sys.path:
    sys.path.insert(0, str(DOOR))

import push as door_push  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


P256DH = _b64(
    ec.derive_private_key(1, ec.SECP256R1())
    .public_key()
    .public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
)
AUTH = _b64(b"a" * 16)


def _subscription(endpoint: str = "https://fcm.googleapis.com/fcm/send/x") -> dict[str, Any]:
    return {"endpoint": endpoint, "keys": {"p256dh": P256DH, "auth": AUTH}}


class _ConditionalFailureError(Exception):
    def __init__(self) -> None:
        super().__init__("conditional check failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class _Table:
    """Small locked fake for the DynamoDB conditions this module depends on."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.deleted: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            item = self.items.get(kwargs["Key"]["endpoint"])
            return {"Item": dict(item)} if item else {}

    def update_item(self, **kwargs: Any) -> None:
        key = kwargs["Key"]["endpoint"]
        values = kwargs["ExpressionAttributeValues"]
        with self._lock:
            current = self.items.get(key, {})
            if int(current.get("admissions", 0)) >= int(values[":cap"]):
                raise _ConditionalFailureError
            self.items[key] = {
                "endpoint": key,
                "admissions": int(current.get("admissions", 0)) + int(values[":one"]),
                "expires_at": values[":expires"],
                "endpoint_digest": values[":endpoint_digest"],
            }

    def put_item(self, **kwargs: Any) -> None:
        item = dict(kwargs["Item"])
        key = item["endpoint"]
        expression = kwargs.get("ConditionExpression", "")
        values = kwargs.get("ExpressionAttributeValues", {})
        with self._lock:
            current = self.items.get(key)
            if expression == "#p = :same AND #c = :prior AND #a = :admitted":
                if (
                    not current
                    or current.get("push_endpoint") != values[":same"]
                    or current.get("claim_id") != values[":prior"]
                    or current.get("admitted") is not values[":admitted"]
                ):
                    raise _ConditionalFailureError
            elif expression == "#c = :claim AND #a = :provisional":
                if (
                    not current
                    or current.get("claim_id") != values[":claim"]
                    or current.get("admitted") is not values[":provisional"]
                ):
                    raise _ConditionalFailureError
            elif current and int(current.get("expires_at", 0)) >= int(values[":now"]):
                raise _ConditionalFailureError
            self.items[key] = item

    def delete_item(self, **kwargs: Any) -> None:
        with self._lock:
            current = self.items.get(kwargs["Key"]["endpoint"])
            values = kwargs.get("ExpressionAttributeValues", {})
            expression = kwargs.get("ConditionExpression")
            if expression == "#c = :claim":
                if not current or current.get("claim_id") != values.get(":claim"):
                    raise _ConditionalFailureError
            elif expression and (
                not current
                or current.get("claim_id")
                or current.get("push_endpoint") != values.get(":endpoint")
                or current.get("p256dh") != values.get(":key")
            ):
                raise _ConditionalFailureError
            self.deleted.append(kwargs["Key"])
            self.items.pop(kwargs["Key"]["endpoint"], None)


def _stored(table: _Table) -> dict[str, Any]:
    return next(item for key, item in table.items.items() if key.startswith(door_push._SLOT_PREFIX))


def _store_direct(table: _Table, endpoint: str, index: int = 0) -> None:
    table.items[door_push._slot_key(index)] = door_push._subscription_item(
        door_push._slot_key(index),
        endpoint,
        P256DH,
        AUTH,
        int(time.time()),
        f"claim-{index}",
        admitted=True,
    )


def _stub_module(monkeypatch: pytest.MonkeyPatch, webpush: Any, error: type[Exception]) -> None:
    stub = types.ModuleType("pywebpush")
    stub.WebPushException = error  # type: ignore[attr-defined]
    stub.webpush = webpush  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pywebpush", stub)
    monkeypatch.setattr(door_push, "VAPID_PRIVATE_KEY", "k")
    monkeypatch.setattr(door_push, "PUSH_TABLE", "t")


def test_notify_payload_names_no_case() -> None:
    blob = json.dumps(door_push._NOTIFY_BODY)
    assert "case" not in blob.lower()
    assert "26ED" not in blob
    assert door_push._NOTIFY_BODY["title"] == "Instanter"


def test_save_subscription_requires_keys() -> None:
    table = _Table()
    out = door_push.save_subscription(
        table, {"endpoint": "https://fcm.googleapis.com/x"}, client_key="one"
    )
    assert out["error"] == "subscription_incomplete"
    assert table.items == {}


def test_save_subscription_stores_a_valid_browser_subscription() -> None:
    table = _Table()
    assert door_push.save_subscription(table, _subscription(), client_key="one") == {"ok": True}
    assert _stored(table)["push_endpoint"] == "https://fcm.googleapis.com/fcm/send/x"
    assert _stored(table)["expires_at"] > _stored(table)["created_at"]


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/fcm/send/x",
        "https://127.0.0.1/push",
        "https://169.254.169.254/latest/meta-data",
        "https://fcm.googleapis.com.evil.example/push",
        "https://user@fcm.googleapis.com/fcm/send/x",
        "https://fcm.googleapis.com:8443/fcm/send/x",
    ],
)
def test_save_subscription_refuses_non_provider_endpoints(endpoint: str) -> None:
    table = _Table()
    out = door_push.save_subscription(table, _subscription(endpoint), client_key="one")
    assert out == {"error": "subscription_endpoint_refused"}
    assert table.items == {}


@pytest.mark.parametrize(
    ("p256dh", "auth"),
    [
        ("abc", AUTH),
        (_b64(b"\x03" + bytes(range(64))), AUTH),
        (_b64(b"\x04" + bytes(range(64))), AUTH),
        (P256DH, "def"),
        (P256DH, _b64(b"a" * 15)),
        (123, AUTH),
        (P256DH, "!" * 16),
    ],
)
def test_save_subscription_refuses_invalid_key_material(p256dh: Any, auth: Any) -> None:
    table = _Table()
    candidate = _subscription()
    candidate["keys"] = {"p256dh": p256dh, "auth": auth}
    assert door_push.save_subscription(table, candidate, client_key="one") == {
        "error": "subscription_keys_invalid"
    }


def test_save_subscription_rate_limits_new_rows_but_allows_refresh() -> None:
    table = _Table()
    assert door_push.save_subscription(table, _subscription(), client_key="one") == {"ok": True}
    other = _subscription("https://fcm.googleapis.com/fcm/send/y")
    assert door_push.save_subscription(table, other, client_key="one") == {
        "error": "subscription_rate_limited"
    }
    assert door_push.save_subscription(table, _subscription(), client_key="one") == {
        "ok": True,
        "refreshed": True,
    }


def test_retry_after_promotion_failure_reuses_the_rate_debit() -> None:
    class _PromotionFailsOnce(_Table):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        def put_item(self, **kwargs: Any) -> None:
            if (
                kwargs.get("ConditionExpression") == "#c = :claim AND #a = :provisional"
                and not self.failed
            ):
                self.failed = True
                raise RuntimeError("promotion failed after the rate debit")
            super().put_item(**kwargs)

    table = _PromotionFailsOnce()
    with pytest.raises(RuntimeError, match="promotion failed"):
        door_push.save_subscription(table, _subscription(), client_key="one")

    assert not any(key.startswith(door_push._SLOT_PREFIX) for key in table.items)
    assert door_push.save_subscription(table, _subscription(), client_key="one") == {"ok": True}
    rate = next(item for key, item in table.items.items() if key.startswith(door_push._RATE_PREFIX))
    assert rate["admissions"] == 1


def test_ambiguous_rate_response_recovers_from_the_committed_debit() -> None:
    class _RateCommitsThenRaisesOnce(_Table):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        def update_item(self, **kwargs: Any) -> None:
            super().update_item(**kwargs)
            if not self.failed:
                self.failed = True
                raise RuntimeError("connection dropped after DynamoDB committed")

    table = _RateCommitsThenRaisesOnce()
    assert door_push.save_subscription(table, _subscription(), client_key="one") == {"ok": True}
    rate = next(item for key, item in table.items.items() if key.startswith(door_push._RATE_PREFIX))
    assert rate["admissions"] == 1


def test_conditional_slots_enforce_capacity_under_concurrency() -> None:
    table = _Table()

    def save(index: int) -> dict[str, Any]:
        candidate = _subscription(f"https://fcm.googleapis.com/fcm/send/{index}")
        return door_push.save_subscription(table, candidate, client_key=f"client-{index}")

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(save, range(10)))
    assert sum(result.get("ok") is True for result in results) == door_push.MAX_SUBSCRIPTIONS
    assert sum(result.get("error") == "subscription_cap_reached" for result in results) == 5
    assert len([key for key in table.items if key.startswith(door_push._SLOT_PREFIX)]) == 5


def test_concurrent_identical_subscriptions_share_one_slot() -> None:
    table = _Table()

    def save(index: int) -> dict[str, Any]:
        return door_push.save_subscription(table, _subscription(), client_key=f"client-{index}")

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(save, range(10)))
    assert any(result.get("ok") is True for result in results)
    assert all(
        result.get("ok") is True or result.get("error") == "subscription_busy" for result in results
    )
    assert len([key for key in table.items if key.startswith(door_push._SLOT_PREFIX)]) == 1


def test_provisional_slot_cannot_bypass_a_concurrent_rate_rejection() -> None:
    class _BlockingRateTable(_Table):
        def __init__(self) -> None:
            super().__init__()
            self.rate_started = threading.Event()
            self.allow_rate = threading.Event()

        def update_item(self, **kwargs: Any) -> None:
            self.rate_started.set()
            assert self.allow_rate.wait(timeout=2)
            super().update_item(**kwargs)

    table = _BlockingRateTable()
    now = int(time.time())
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    rate_key = f"{door_push._RATE_PREFIX}{day}:capped"
    table.items[rate_key] = {
        "endpoint": rate_key,
        "admissions": door_push.MAX_ADMISSIONS_PER_IP_DAY,
        "expires_at": now + 3600,
    }

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(
            door_push.save_subscription, table, _subscription(), client_key="capped"
        )
        assert table.rate_started.wait(timeout=2)
        second = door_push.save_subscription(table, _subscription(), client_key="capped")
        table.allow_rate.set()
        assert first.result(timeout=2) == {"error": "subscription_rate_limited"}

    assert second == {"error": "subscription_busy"}
    assert not any(key.startswith(door_push._SLOT_PREFIX) for key in table.items)


def test_notify_skips_a_provisional_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def webpush(**_kwargs: Any) -> None:
        nonlocal called
        called = True

    _stub_module(monkeypatch, webpush, RuntimeError)
    table = _Table()
    slot = door_push._slot_key(0)
    table.items[slot] = door_push._subscription_item(
        slot,
        "https://fcm.googleapis.com/fcm/send/provisional",
        P256DH,
        AUTH,
        int(time.time()),
        "provisional-claim",
        admitted=False,
    )
    assert door_push.notify_interrupt(table) == 0
    assert called is False
    assert slot in table.items


def test_a_full_table_does_not_spend_the_callers_daily_admission() -> None:
    table = _Table()
    for index in range(door_push.MAX_SUBSCRIPTIONS):
        result = door_push.save_subscription(
            table,
            _subscription(f"https://fcm.googleapis.com/fcm/send/{index}"),
            client_key=f"owner-{index}",
        )
        assert result.get("ok") is True
    waiting = _subscription("https://fcm.googleapis.com/fcm/send/waiting")
    assert door_push.save_subscription(table, waiting, client_key="waiting") == {
        "error": "subscription_cap_reached",
        "cap": door_push.MAX_SUBSCRIPTIONS,
    }
    assert not any(key.endswith(":waiting") for key in table.items)
    occupied = next(key for key in table.items if key.startswith(door_push._SLOT_PREFIX))
    table.delete_item(Key={"endpoint": occupied})
    assert door_push.save_subscription(table, waiting, client_key="waiting") == {"ok": True}


def test_notify_without_vapid_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(door_push, "VAPID_PRIVATE_KEY", "")
    monkeypatch.setattr(door_push, "PUSH_TABLE", "t")
    assert door_push.notify_interrupt(_Table()) == 0


def test_notify_prunes_an_endpoint_the_push_service_says_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Resp:
        status_code = 410

    class _GoneError(Exception):
        def __init__(self) -> None:
            super().__init__("gone")
            self.response = _Resp()

    seen: dict[str, Any] = {}

    def webpush(**kwargs: Any) -> None:
        seen.update(kwargs)
        raise _GoneError

    _stub_module(monkeypatch, webpush, _GoneError)
    table = _Table()
    _store_direct(table, "https://fcm.googleapis.com/fcm/send/dead")
    assert door_push.notify_interrupt(table) == 0
    assert seen["timeout"] == 2.0
    assert table.deleted == [{"endpoint": door_push._slot_key(0)}]


def test_notify_uses_a_fresh_audience_for_each_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    claims: list[dict[str, str]] = []

    def webpush(**kwargs: Any) -> None:
        claims.append(dict(kwargs["vapid_claims"]))
        kwargs["vapid_claims"]["aud"] = "mutated"

    _stub_module(monkeypatch, webpush, RuntimeError)
    table = _Table()
    _store_direct(table, "https://fcm.googleapis.com/fcm/send/a", 0)
    _store_direct(table, "https://updates.push.services.mozilla.com/wpush/v2/b", 1)
    assert door_push.notify_interrupt(table) == 2
    assert [claim["aud"] for claim in claims] == [
        "https://fcm.googleapis.com",
        "https://updates.push.services.mozilla.com",
    ]


def test_notify_surfaces_partial_delivery_count(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code = 403

    class _ForbiddenError(Exception):
        def __init__(self) -> None:
            super().__init__("forbidden")
            self.response = _Resp()

    calls = 0

    def webpush(**_kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise _ForbiddenError

    _stub_module(monkeypatch, webpush, _ForbiddenError)
    table = _Table()
    _store_direct(table, "https://fcm.googleapis.com/fcm/send/a", 0)
    _store_direct(table, "https://updates.push.services.mozilla.com/wpush/v2/b", 1)
    with pytest.raises(door_push.PushDeliveryError, match="1 of 2") as caught:
        door_push.notify_interrupt(table)
    assert caught.value.sent == 1


def test_notify_has_one_absolute_elapsed_time_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    def webpush(**_kwargs: Any) -> None:
        time.sleep(0.2)

    _stub_module(monkeypatch, webpush, RuntimeError)
    monkeypatch.setattr(door_push, "_NOTIFY_BUDGET_SECONDS", 0.03)
    table = _Table()
    _store_direct(table, "https://fcm.googleapis.com/fcm/send/slow")
    started = time.monotonic()
    with pytest.raises(door_push.PushDeliveryError, match="absolute deadline"):
        door_push.notify_interrupt(table)
    assert time.monotonic() - started < 0.15


def test_transport_cannot_translate_and_swallow_the_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _TranslatedReadTimeoutError(Exception):
        pass

    calls = 0

    def webpush(**_kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        try:
            time.sleep(0.2)
        except Exception as exc:
            raise _TranslatedReadTimeoutError("transport translated timeout") from exc

    _stub_module(monkeypatch, webpush, _TranslatedReadTimeoutError)
    monkeypatch.setattr(door_push, "_NOTIFY_BUDGET_SECONDS", 0.03)
    table = _Table()
    _store_direct(table, "https://fcm.googleapis.com/fcm/send/slow-a", 0)
    _store_direct(table, "https://updates.push.services.mozilla.com/wpush/v2/slow-b", 1)
    started = time.monotonic()
    with pytest.raises(door_push.PushDeliveryError, match="absolute deadline"):
        door_push.notify_interrupt(table)
    assert calls == 1
    assert time.monotonic() - started < 0.15


def test_notify_budget_includes_subscription_table_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    class _SlowTable(_Table):
        def get_item(self, **kwargs: Any) -> dict[str, Any]:
            time.sleep(0.2)
            return super().get_item(**kwargs)

    _stub_module(monkeypatch, lambda **_kwargs: None, RuntimeError)
    monkeypatch.setattr(door_push, "_NOTIFY_BUDGET_SECONDS", 0.03)
    started = time.monotonic()
    with pytest.raises(door_push.PushDeliveryError, match="absolute deadline"):
        door_push.notify_interrupt(_SlowTable())
    assert time.monotonic() - started < 0.15


def test_gone_response_cannot_delete_a_concurrent_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Resp:
        status_code = 410

    class _GoneError(Exception):
        def __init__(self) -> None:
            super().__init__("gone")
            self.response = _Resp()

    table = _Table()
    endpoint = "https://fcm.googleapis.com/fcm/send/replaced"
    _store_direct(table, endpoint)
    slot = door_push._slot_key(0)

    def webpush(**_kwargs: Any) -> None:
        table.items[slot] = door_push._subscription_item(
            slot,
            endpoint,
            P256DH,
            AUTH,
            int(time.time()),
            "new-generation",
            admitted=True,
        )
        raise _GoneError

    _stub_module(monkeypatch, webpush, _GoneError)
    assert door_push.notify_interrupt(table) == 0
    assert table.items[slot]["claim_id"] == "new-generation"


def test_notify_prunes_a_legacy_invalid_slot_without_fetching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def webpush(**_kwargs: Any) -> None:
        nonlocal called
        called = True

    _stub_module(monkeypatch, webpush, RuntimeError)
    table = _Table()
    table.items[door_push._slot_key(0)] = {
        "endpoint": door_push._slot_key(0),
        "push_endpoint": "http://127.0.0.1/private",
        "p256dh": P256DH,
        "auth": AUTH,
    }
    assert door_push.notify_interrupt(table) == 0
    assert called is False
    assert table.deleted == [{"endpoint": door_push._slot_key(0)}]
