"""Web Push for the public console.

A subscription is stored. A push is sent only when a sweep actually stops
for an attorney. The payload names no case, because a lock-screen preview
must not carry case data.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import signal
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_MAILTO = os.environ.get("VAPID_MAILTO", "mailto:stephensookra@gmail.com")
PUSH_TABLE = os.environ.get("PUSH_TABLE", "")
MAX_SUBSCRIPTIONS = int(os.environ.get("MAX_PUSH_SUBSCRIPTIONS", "5"))
MAX_ADMISSIONS_PER_IP_DAY = int(os.environ.get("MAX_PUSH_ADMISSIONS_PER_IP_DAY", "1"))

_PUSH_HOSTS = {
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    "web.push.apple.com",
}
_PUSH_HOST_SUFFIXES = (".notify.windows.com",)
_WEBPUSH_TIMEOUT_SECONDS = 2.0
_NOTIFY_BUDGET_SECONDS = 8.0
_PROVISIONAL_TTL_SECONDS = 30
_SLOT_PREFIX = "__push_slot__"
_RATE_PREFIX = "__push_rate__"

_NOTIFY_BODY = {
    "title": "Instanter",
    "body": "A sweep is waiting on an attorney.",
}


def public_key() -> str:
    return VAPID_PUBLIC_KEY


def _valid_push_endpoint(endpoint: str) -> bool:
    """Accept only the browser vendors that issue Web Push capabilities.

    The endpoint is later fetched by the Lambda. Treating an arbitrary URL as
    a subscription would turn a public form into blind SSRF and let one slow
    host consume the function's remaining execution time.
    """
    try:
        parsed = urlsplit(endpoint)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return False
    if port not in (None, 443):
        return False
    return host in _PUSH_HOSTS or any(host.endswith(suffix) for suffix in _PUSH_HOST_SUFFIXES)


def _decode_webpush_key(value: Any) -> bytes | None:
    if not isinstance(value, str) or not value or len(value) > 512:
        return None
    unpadded = value.rstrip("=")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    if "=" in unpadded or any(char not in alphabet for char in unpadded):
        return None
    try:
        return base64.b64decode(
            unpadded + "=" * ((4 - len(unpadded) % 4) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError):
        return None


def _valid_webpush_keys(p256dh: Any, auth: Any) -> bool:
    public_point = _decode_webpush_key(p256dh)
    auth_secret = _decode_webpush_key(auth)
    if not (
        public_point
        and len(public_point) == 65
        and public_point[0] == 4
        and auth_secret
        and len(auth_secret) == 16
    ):
        return False
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_point)
    except ValueError:
        return False
    return True


def _conditional_failed(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    if not isinstance(response, dict):
        return False
    error = response.get("Error")
    return isinstance(error, dict) and error.get("Code") == "ConditionalCheckFailedException"


def _slot_key(index: int) -> str:
    return f"{_SLOT_PREFIX}{index}"


def _slot_order(endpoint: str) -> list[int]:
    start = int.from_bytes(hashlib.sha256(endpoint.encode("utf-8")).digest()[:4], "big")
    start %= MAX_SUBSCRIPTIONS
    return [(start + offset) % MAX_SUBSCRIPTIONS for offset in range(MAX_SUBSCRIPTIONS)]


def _endpoint_digest(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def _slots(table: Any) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    for index in range(MAX_SUBSCRIPTIONS):
        key = _slot_key(index)
        item = table.get_item(Key={"endpoint": key}, ConsistentRead=True).get("Item")
        if item:
            found.append((key, item))
    return found


def _subscription_item(
    slot: str,
    endpoint: str,
    p256dh: str,
    auth: str,
    now: int,
    claim_id: str,
    *,
    admitted: bool,
) -> dict[str, Any]:
    return {
        "endpoint": slot,
        "push_endpoint": endpoint,
        "p256dh": p256dh,
        "auth": auth,
        "claim_id": claim_id,
        "admitted": admitted,
        "created_at": now,
        "expires_at": now + (180 * 24 * 3600 if admitted else _PROVISIONAL_TTL_SECONDS),
    }


def save_subscription(
    table: Any,
    subscription: dict[str, Any],
    *,
    client_key: str,
) -> dict[str, Any]:
    raw_endpoint = subscription.get("endpoint")
    endpoint = raw_endpoint if isinstance(raw_endpoint, str) else ""
    raw_keys = subscription.get("keys")
    keys: dict[str, Any] = raw_keys if isinstance(raw_keys, dict) else {}
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not endpoint or not p256dh or not auth:
        return {"error": "subscription_incomplete"}
    if len(endpoint) > 2048:
        return {"error": "subscription_too_long"}
    if not _valid_push_endpoint(endpoint):
        return {"error": "subscription_endpoint_refused"}
    if not _valid_webpush_keys(p256dh, auth):
        return {"error": "subscription_keys_invalid"}
    if not client_key or len(client_key) > 128:
        return {"error": "subscription_client_invalid"}

    assert isinstance(p256dh, str)
    assert isinstance(auth, str)
    now = int(time.time())
    claim_id = uuid.uuid4().hex
    endpoint_digest = _endpoint_digest(endpoint)
    slots = dict(_slots(table))
    for slot, item in slots.items():
        if item.get("push_endpoint") != endpoint or item.get("admitted") is not True:
            continue
        prior_claim = item.get("claim_id")
        if not isinstance(prior_claim, str) or not prior_claim:
            continue
        try:
            table.put_item(
                Item=_subscription_item(slot, endpoint, p256dh, auth, now, claim_id, admitted=True),
                ConditionExpression="#p = :same AND #c = :prior AND #a = :admitted",
                ExpressionAttributeNames={
                    "#p": "push_endpoint",
                    "#c": "claim_id",
                    "#a": "admitted",
                },
                ExpressionAttributeValues={
                    ":same": endpoint,
                    ":prior": prior_claim,
                    ":admitted": True,
                },
            )
            return {"ok": True, "refreshed": True}
        except Exception as exc:
            if not _conditional_failed(exc):
                raise

    for index in _slot_order(endpoint):
        slot = _slot_key(index)
        try:
            table.put_item(
                Item=_subscription_item(
                    slot, endpoint, p256dh, auth, now, claim_id, admitted=False
                ),
                ConditionExpression="attribute_not_exists(#k) OR #e < :now",
                ExpressionAttributeNames={"#k": "endpoint", "#e": "expires_at"},
                ExpressionAttributeValues={":now": now},
            )
        except Exception as exc:
            if not _conditional_failed(exc):
                raise
            latest = table.get_item(Key={"endpoint": slot}, ConsistentRead=True).get("Item")
            if latest and latest.get("push_endpoint") == endpoint:
                if latest.get("admitted") is True:
                    return {"ok": True, "refreshed": True}
                return {"error": "subscription_busy"}
            continue

        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        try:
            table.update_item(
                Key={"endpoint": f"{_RATE_PREFIX}{day}:{client_key}"},
                UpdateExpression="ADD #n :one SET #e = :expires, #d = :endpoint_digest",
                ConditionExpression="attribute_not_exists(#n) OR #n < :cap",
                ExpressionAttributeNames={
                    "#n": "admissions",
                    "#e": "expires_at",
                    "#d": "endpoint_digest",
                },
                ExpressionAttributeValues={
                    ":one": 1,
                    ":cap": MAX_ADMISSIONS_PER_IP_DAY,
                    ":expires": now + 2 * 24 * 3600,
                    ":endpoint_digest": endpoint_digest,
                },
            )
        except Exception as exc:
            rate_item = table.get_item(
                Key={"endpoint": f"{_RATE_PREFIX}{day}:{client_key}"}, ConsistentRead=True
            ).get("Item")
            if rate_item and rate_item.get("endpoint_digest") == endpoint_digest:
                pass
            else:
                with contextlib.suppress(Exception):
                    table.delete_item(
                        Key={"endpoint": slot},
                        ConditionExpression="#c = :claim",
                        ExpressionAttributeNames={"#c": "claim_id"},
                        ExpressionAttributeValues={":claim": claim_id},
                    )
                if _conditional_failed(exc):
                    return {"error": "subscription_rate_limited"}
                raise
        try:
            table.put_item(
                Item=_subscription_item(slot, endpoint, p256dh, auth, now, claim_id, admitted=True),
                ConditionExpression="#c = :claim AND #a = :provisional",
                ExpressionAttributeNames={"#c": "claim_id", "#a": "admitted"},
                ExpressionAttributeValues={":claim": claim_id, ":provisional": False},
            )
        except Exception:
            with contextlib.suppress(Exception):
                table.delete_item(
                    Key={"endpoint": slot},
                    ConditionExpression="#c = :claim",
                    ExpressionAttributeNames={"#c": "claim_id"},
                    ExpressionAttributeValues={":claim": claim_id},
                )
            raise
        return {"ok": True}
    return {"error": "subscription_cap_reached", "cap": MAX_SUBSCRIPTIONS}


class PushDeliveryError(RuntimeError):
    def __init__(self, message: str, *, sent: int) -> None:
        super().__init__(message)
        self.sent = sent


class _PushDeadlineExpired(BaseException):
    """Escape transports that catch and translate ordinary exceptions."""


def _delete_claim(table: Any, slot: str, item: dict[str, Any]) -> None:
    """Delete only the slot generation that produced this delivery result."""
    claim_id = item.get("claim_id")
    values: dict[str, Any]
    if isinstance(claim_id, str) and claim_id:
        condition = "#c = :claim"
        names = {"#c": "claim_id"}
        values = {":claim": claim_id}
    else:
        condition = "attribute_not_exists(#c) AND #p = :endpoint AND p256dh = :key"
        names = {"#c": "claim_id", "#p": "push_endpoint"}
        values = {":endpoint": item.get("push_endpoint"), ":key": item.get("p256dh")}
    try:
        table.delete_item(
            Key={"endpoint": slot},
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except TimeoutError:
        raise
    except Exception:
        # TTL is the cleanup backstop. A conditional miss means this slot has
        # already been refreshed or reclaimed and must stay.
        return


@contextlib.contextmanager
def _absolute_timeout(seconds: float) -> Any:
    """Interrupt a stalled requests call before the judge-door budget expires."""

    def _raise_timeout(_signum: int, _frame: Any) -> None:
        # urllib3 catches ordinary exceptions raised inside socket reads and
        # can translate TimeoutError into requests.ReadTimeout. BaseException
        # crosses that boundary so the one-shot alarm cannot be consumed.
        raise _PushDeadlineExpired("Web Push delivery exceeded its absolute deadline")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    started = time.monotonic()
    try:
        signal.signal(signal.SIGALRM, _raise_timeout)
        signal.setitimer(signal.ITIMER_REAL, max(seconds, 0.001))
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        old_delay, old_interval = previous_timer
        if old_delay > 0:
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.001, old_delay - (time.monotonic() - started)),
                old_interval,
            )


def notify_interrupt(table: Any) -> int:
    """Send the no-case-data ping. Returns how many pushes were delivered.

    Unset VAPID keys or table return 0, indistinguishable here from zero
    subscribers on purpose: the vapid endpoint 503s when unconfigured, so
    nobody can subscribe in that state. A CRASH is not handled here at all;
    the caller catches it and surfaces push_error on the run result.
    """
    if not VAPID_PRIVATE_KEY or not PUSH_TABLE:
        return 0
    try:
        from pywebpush import WebPushException, webpush  # type: ignore[import-not-found]
    except ImportError:
        return 0
    sent = 0
    systemic = 0
    first_error = ""
    attempted = 0
    try:
        # The single alarm covers DynamoDB reads, provider delivery, and
        # generation-bound cleanup. Starting it after any of those operations
        # would make the stated budget incomplete.
        with _absolute_timeout(_NOTIFY_BUDGET_SECONDS):
            slots = _slots(table)
            payload = json.dumps(_NOTIFY_BODY)
            for slot, item in slots:
                if item.get("admitted") is not True:
                    if "admitted" not in item:
                        _delete_claim(table, slot, item)
                    continue
                endpoint = str(item.get("push_endpoint") or "")
                if not _valid_push_endpoint(endpoint) or not _valid_webpush_keys(
                    item.get("p256dh"), item.get("auth")
                ):
                    _delete_claim(table, slot, item)
                    continue
                attempted += 1
                host = (urlsplit(endpoint).hostname or "").lower()
                try:
                    webpush(
                        subscription_info={
                            "endpoint": endpoint,
                            "keys": {"p256dh": item["p256dh"], "auth": item["auth"]},
                        },
                        data=payload,
                        vapid_private_key=VAPID_PRIVATE_KEY,
                        vapid_claims={"sub": VAPID_MAILTO, "aud": f"https://{host}"},
                        ttl=120,
                        timeout=_WEBPUSH_TIMEOUT_SECONDS,
                    )
                    sent += 1
                except TimeoutError:
                    raise
                except WebPushException as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status in (404, 410):
                        _delete_claim(table, slot, item)
                        continue
                    systemic += 1
                    first_error = first_error or f"HTTP {status or '?'}: {str(exc)[:120]}"
                except Exception as exc:
                    systemic += 1
                    first_error = first_error or str(exc)[:120]
    except _PushDeadlineExpired as exc:
        raise PushDeliveryError(str(exc), sent=sent) from exc
    if systemic > 0:
        raise PushDeliveryError(
            f"push delivery failed for {systemic} of {attempted} endpoint(s): {first_error}",
            sent=sent,
        )
    return sent
