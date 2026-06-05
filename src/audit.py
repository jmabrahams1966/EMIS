"""Audit log — append-only event stream for HIPAA traceability.

Records who-did-what to S3 as daily JSONL files at::

    s3://{bucket}/state/audit/{YYYY-MM-DD}.jsonl

Each line is one JSON object::

    {"ts": "2026-06-03T10:30:00Z", "event": "login_success",
     "user_id": "...", "email": "...", "ip": "...", "extra": {...}}

S3 lifecycle rule on the ``state/audit/`` prefix would handle long-term
retention; for now the files just accumulate (small footprint).

Read path: ``list_recent`` fetches the most recent N days' files, parses
each line, returns newest first. Cheap enough for an admin UI table.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


def _client_ip(event: dict[str, Any] | None) -> str:
    """Best-effort source IP from a Lambda Function URL event."""
    if not event:
        return ""
    rc = (event.get("requestContext") or {}).get("http", {})
    return rc.get("sourceIp", "") or ""


def _day_key(when: datetime) -> str:
    return f"state/audit/{when.date().isoformat()}.jsonl"


def record_event(
    event_type: str,
    *,
    user_id: str = "",
    email: str = "",
    actor_user_id: str = "",     # set when an admin acts on behalf of another user
    target_user_id: str = "",
    request: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append a single event to today's audit JSONL. Best-effort; never raises."""
    bucket = os.getenv("STATE_BUCKET") or ""
    if not bucket:
        return
    now = datetime.now(timezone.utc)
    line = {
        "ts": now.isoformat(),
        "event": event_type,
        "user_id": user_id,
        "email": email,
        "actor_user_id": actor_user_id,
        "target_user_id": target_user_id,
        "ip": _client_ip(request),
        "extra": extra or {},
    }
    body = (json.dumps(line, separators=(",", ":")) + "\n").encode("utf-8")
    key = _day_key(now)
    s3 = boto3.client("s3")
    try:
        # S3 lacks native append, so read + concatenate + put. For the very
        # low volume here (handfuls of events per day per user) the cost is
        # negligible. If volume ever crosses ~1k/day, swap to DynamoDB.
        try:
            existing = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("NoSuchKey", "404"):
                raise
            existing = b""
        s3.put_object(
            Bucket=bucket, Key=key,
            Body=existing + body,
            ContentType="application/jsonl",
        )
    except Exception as exc:
        logger.warning("audit record failed (event=%s): %s", event_type, exc)


def list_recent(days: int = 7, limit: int = 200) -> list[dict[str, Any]]:
    """Return the most recent events across the last ``days`` daily files."""
    bucket = os.getenv("STATE_BUCKET") or ""
    if not bucket:
        return []
    out: list[dict[str, Any]] = []
    s3 = boto3.client("s3")
    now = datetime.now(timezone.utc)
    for d in range(days):
        when = now - timedelta(days=d)
        key = _day_key(when)
        try:
            blob = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                continue
            logger.warning("audit read failed for %s: %s", key, exc)
            continue
        for raw in blob.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except Exception:
                pass
    out.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return out[:limit]
