"""Write a JSON record to the Object Lock audit bucket.

Used by the public door for visitor sweeps and attorney decisions, the
events a stranger can actually cause. The AgentCore runtime writes the
full trail through LockedAuditSink. Both land in the same bucket.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

AUDIT_LOCK_BUCKET = os.environ.get("AUDIT_LOCK_BUCKET", "")
RETAIN_DAYS = int(os.environ.get("AUDIT_LOCK_RETAIN_DAYS", "30"))


def lock_record(kind: str, run_id: str, payload: dict[str, Any]) -> None:
    if not AUDIT_LOCK_BUCKET:
        # RAISE, do not silently skip. LockedAuditSink takes the same stance
        # and its docstring says why: a sink that claims to lock and quietly
        # does not is the original lie. Callers catch this and surface it as
        # audit_lock_error on the response, so a misconfigured deploy is
        # visible on every run instead of discovered in a bucket audit.
        raise RuntimeError("AUDIT_LOCK_BUCKET is unset; refusing to drop an audit record")
    import boto3

    now = datetime.now(UTC)
    key = f"door/{run_id}/{now.strftime('%Y%m%dT%H%M%SZ')}-{kind}.json"
    boto3.client("s3").put_object(
        Bucket=AUDIT_LOCK_BUCKET,
        Key=key,
        Body=json.dumps(
            {"kind": kind, "run_id": run_id, "recorded_at": now.isoformat(), "payload": payload},
            default=str,
        ).encode("utf-8"),
        ContentType="application/json",
        ObjectLockMode="COMPLIANCE",
        ObjectLockRetainUntilDate=now + timedelta(days=RETAIN_DAYS),
    )
