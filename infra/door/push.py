"""Web Push for the public console.

A subscription is stored. A push is sent only when a sweep actually stops
for an attorney. The payload names no case, because a lock-screen preview
must not carry case data.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_MAILTO = os.environ.get("VAPID_MAILTO", "mailto:stephensookra@gmail.com")
PUSH_TABLE = os.environ.get("PUSH_TABLE", "")
MAX_SUBSCRIPTIONS = int(os.environ.get("MAX_PUSH_SUBSCRIPTIONS", "200"))

_NOTIFY_BODY = {
    "title": "Instanter",
    "body": "A sweep is waiting on an attorney.",
}


def public_key() -> str:
    return VAPID_PUBLIC_KEY


def save_subscription(table: Any, subscription: dict[str, Any]) -> dict[str, Any]:
    endpoint = str(subscription.get("endpoint") or "")
    raw_keys = subscription.get("keys")
    keys: dict[str, Any] = raw_keys if isinstance(raw_keys, dict) else {}
    p256dh = str(keys.get("p256dh") or "")
    auth = str(keys.get("auth") or "")
    if not endpoint or not p256dh or not auth:
        return {"error": "subscription_incomplete"}
    if len(endpoint) > 2048:
        return {"error": "subscription_too_long"}
    counted = table.scan(Select="COUNT").get("Count") or 0
    if counted >= MAX_SUBSCRIPTIONS:
        return {"error": "subscription_cap_reached", "cap": MAX_SUBSCRIPTIONS}
    table.put_item(
        Item={
            "endpoint": endpoint,
            "p256dh": p256dh,
            "auth": auth,
            "created_at": int(time.time()),
        }
    )
    return {"ok": True}


def notify_interrupt(table: Any) -> int:
    """Send the no-case-data ping. Returns how many endpoints were attempted."""
    if not VAPID_PRIVATE_KEY or not PUSH_TABLE:
        return 0
    try:
        from pywebpush import WebPushException, webpush  # type: ignore[import-not-found]
    except ImportError:
        return 0
    sent = 0
    scan = table.scan()
    items = list(scan.get("Items") or [])
    while scan.get("LastEvaluatedKey"):
        scan = table.scan(ExclusiveStartKey=scan["LastEvaluatedKey"])
        items.extend(scan.get("Items") or [])
    payload = json.dumps(_NOTIFY_BODY)
    vapid_claims = {"sub": VAPID_MAILTO}
    for item in items:
        try:
            webpush(
                subscription_info={
                    "endpoint": item["endpoint"],
                    "keys": {"p256dh": item["p256dh"], "auth": item["auth"]},
                },
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=vapid_claims,
                ttl=120,
            )
            sent += 1
        except WebPushException:
            continue
        except Exception:
            continue
    return sent
