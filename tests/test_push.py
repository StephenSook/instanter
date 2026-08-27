"""Web Push stores a subscription and pings with no case data."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

DOOR = Path(__file__).parent.parent / "infra" / "door"
if str(DOOR) not in sys.path:
    sys.path.insert(0, str(DOOR))

import push as door_push  # noqa: E402


class _Table:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []

    def scan(self, **kwargs: object) -> dict[str, Any]:
        if kwargs.get("Select") == "COUNT":
            return {"Count": len(self.items)}
        return {"Items": list(self.items)}

    def put_item(self, **kwargs: Any) -> None:
        self.items.append(kwargs["Item"])

    def delete_item(self, **kwargs: Any) -> None:
        self.deleted.append(kwargs["Key"])


def test_notify_payload_names_no_case() -> None:
    blob = json.dumps(door_push._NOTIFY_BODY)
    assert "case" not in blob.lower()
    assert "26ED" not in blob
    assert door_push._NOTIFY_BODY["title"] == "Instanter"
    assert door_push._NOTIFY_BODY["body"] == "A sweep is waiting on an attorney."


def test_save_subscription_requires_keys() -> None:
    table = _Table()
    out = door_push.save_subscription(table, {"endpoint": "https://push.example/x"})
    assert out["error"] == "subscription_incomplete"
    assert table.items == []


def test_save_subscription_stores_endpoint() -> None:
    table = _Table()
    out = door_push.save_subscription(
        table,
        {
            "endpoint": "https://push.example/x",
            "keys": {"p256dh": "abc", "auth": "def"},
        },
    )
    assert out == {"ok": True}
    assert table.items[0]["endpoint"] == "https://push.example/x"


def test_notify_without_vapid_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(door_push, "VAPID_PRIVATE_KEY", "")
    monkeypatch.setattr(door_push, "PUSH_TABLE", "t")
    assert door_push.notify_interrupt(_Table()) == 0


def test_save_subscription_writes_an_expiry() -> None:
    """Endpoints churn; without TTL the table fills with corpses until the
    cap refuses every new subscriber."""
    table = _Table()
    door_push.save_subscription(
        table,
        {"endpoint": "https://push.example/x", "keys": {"p256dh": "abc", "auth": "def"}},
    )
    assert table.items[0]["expires_at"] > table.items[0]["created_at"]


def test_notify_prunes_an_endpoint_the_push_service_says_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 410 Gone endpoint will never deliver again. Leaving the row means it
    counts against the subscription cap forever and every ping walks it."""
    import types

    class _Resp:
        status_code = 410

    class _StubGoneError(Exception):
        def __init__(self) -> None:
            super().__init__("gone")
            self.response = _Resp()

    def _stub_webpush(**_kwargs: Any) -> None:
        raise _StubGoneError()

    stub = types.ModuleType("pywebpush")
    stub.WebPushException = _StubGoneError  # type: ignore[attr-defined]
    stub.webpush = _stub_webpush  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pywebpush", stub)
    monkeypatch.setattr(door_push, "VAPID_PRIVATE_KEY", "k")
    monkeypatch.setattr(door_push, "PUSH_TABLE", "t")

    table = _Table()
    table.items.append({"endpoint": "https://push.example/dead", "p256dh": "a", "auth": "b"})
    sent = door_push.notify_interrupt(table)
    assert sent == 0
    assert table.deleted == [{"endpoint": "https://push.example/dead"}]
