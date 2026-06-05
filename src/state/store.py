"""S3-backed run state + artifact storage.

Layout:
  s3://{bucket}/runs/{YYYY-WW}/agenda.json           ← Monday (canonical; used as memory)
  s3://{bucket}/runs/{YYYY-WW}/agenda.{mode}.json    ← any run, indexed by mode
  s3://{bucket}/runs/{YYYY-WW}/agenda.{mode}.md
  s3://{bucket}/runs/{YYYY-WW}/agenda.{mode}.pdf
  s3://{bucket}/runs/{YYYY-WW}/attachments/{message_id}/{filename}
  s3://{bucket}/state/closures.json
  s3://{bucket}/state/notes.json
  s3://{bucket}/state/pins.json
  s3://{bucket}/config/vip_senders.json
  s3://{bucket}/config/blocklist.json
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


# ── Lightweight key-value state (notes + pins) ────────────────────────────

def _load_json(bucket: str, key: str, default: Any) -> Any:
    """Load a JSON object from S3, returning ``default`` if missing."""
    if not bucket:
        return default
    try:
        obj = boto3.client("s3").get_object(Bucket=bucket, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return default
        raise


def _save_json(bucket: str, key: str, data: Any) -> None:
    if not bucket:
        return
    boto3.client("s3").put_object(
        Bucket=bucket, Key=key,
        Body=json.dumps(data, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


def load_notes(bucket: str, user_id: str | None = None) -> dict[str, str]:
    """Return ``{item_match: note}`` saved via the dashboard note buttons."""
    raw = _load_json(bucket, _state_key("notes.json", user_id), {"notes": {}})
    return dict(raw.get("notes") or {})


def save_notes(bucket: str, notes: dict[str, str], user_id: str | None = None) -> None:
    _save_json(bucket, _state_key("notes.json", user_id), {"notes": notes})


def load_pins(bucket: str, user_id: str | None = None) -> list[str]:
    """Return the list of pinned item titles."""
    raw = _load_json(bucket, _state_key("pins.json", user_id), {"pins": []})
    return list(raw.get("pins") or [])


def save_pins(bucket: str, pins: list[str], user_id: str | None = None) -> None:
    _save_json(bucket, _state_key("pins.json", user_id), {"pins": pins})


def _week_dir(week_start: datetime, user_id: str | None = None) -> str:
    """Return the S3 prefix for a given week.

    Multi-tenant: when ``user_id`` is provided, returns
    ``users/{user_id}/runs/{YYYY-WW}``. Legacy (single-user): returns
    ``runs/{YYYY-WW}``. Both shapes coexist during the migration window
    so old archived agendas remain discoverable.
    """
    iso = week_start.isocalendar()
    week = f"{iso.year:04d}-W{iso.week:02d}"
    if user_id:
        return f"users/{user_id}/runs/{week}"
    return f"runs/{week}"


def _state_key(name: str, user_id: str | None = None) -> str:
    """Return the S3 key for a state file (closures, notes, pins)."""
    if user_id:
        return f"users/{user_id}/state/{name}"
    return f"state/{name}"


def save_agenda(
    bucket: str, week_start: datetime, mode: str, agenda: dict[str, Any],
    user_id: str | None = None,
) -> str:
    """Write the agenda JSON. Monday runs *also* write the canonical
    ``agenda.json`` used for cross-week memory."""
    s3 = boto3.client("s3")
    week = _week_dir(week_start, user_id)
    mode_key = f"{week}/agenda.{mode}.json"
    body = json.dumps(agenda, indent=2).encode("utf-8")
    s3.put_object(Bucket=bucket, Key=mode_key, Body=body, ContentType="application/json")
    if mode == "monday":
        s3.put_object(Bucket=bucket, Key=f"{week}/agenda.json", Body=body, ContentType="application/json")
    logger.info("saved agenda to s3://%s/%s", bucket, mode_key)
    return mode_key


def save_artifact(
    bucket: str, week_start: datetime, name: str, data: bytes, content_type: str,
    user_id: str | None = None,
) -> str:
    key = f"{_week_dir(week_start, user_id)}/{name}"
    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
    return key


def save_attachment(
    bucket: str, week_start: datetime, message_id: str,
    filename: str, data: bytes, content_type: str,
    user_id: str | None = None,
) -> str:
    key = f"{_week_dir(week_start, user_id)}/attachments/{message_id}/{filename}"
    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
    return key


def list_weeks(bucket: str, limit: int = 52, user_id: str | None = None) -> list[str]:
    """Return a sorted (newest first) list of ISO-week directory names that
    have a Monday agenda on disk for this user (or for the legacy single-user
    namespace when ``user_id`` is None)."""
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    weeks: set[str] = set()
    prefix = f"users/{user_id}/runs/" if user_id else "runs/"
    # When listing per-user, parts are [users, user_id, runs, <week>, ...] — 5 parts;
    # legacy parts are [runs, <week>, ...] — 3 parts. Compute the week index dynamically.
    week_idx = 3 if user_id else 1
    file_idx = 4 if user_id else 2
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            key = obj["Key"]
            parts = key.split("/")
            if len(parts) > file_idx and parts[file_idx].startswith("agenda"):
                weeks.add(parts[week_idx])
    return sorted(weeks, reverse=True)[:limit]


def load_agenda(
    bucket: str, iso_week: str, mode: str | None = None, user_id: str | None = None,
) -> dict[str, Any] | None:
    s3 = boto3.client("s3")
    prefix = f"users/{user_id}/runs" if user_id else "runs"
    key = f"{prefix}/{iso_week}/agenda{'' if mode is None else '.' + mode}.json"
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except Exception:
        return None
