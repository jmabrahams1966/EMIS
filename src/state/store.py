"""S3-backed run state + artifact storage.

Layout:
  s3://{bucket}/runs/{YYYY-WW}/agenda.json           ← Monday (canonical; used as memory)
  s3://{bucket}/runs/{YYYY-WW}/agenda.{mode}.json    ← any run, indexed by mode
  s3://{bucket}/runs/{YYYY-WW}/agenda.{mode}.md
  s3://{bucket}/runs/{YYYY-WW}/agenda.{mode}.pdf
  s3://{bucket}/runs/{YYYY-WW}/attachments/{message_id}/{filename}
  s3://{bucket}/config/vip_senders.json
  s3://{bucket}/config/blocklist.json
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import boto3

logger = logging.getLogger(__name__)


def _week_dir(week_start: datetime) -> str:
    iso = week_start.isocalendar()
    return f"runs/{iso.year:04d}-W{iso.week:02d}"


def save_agenda(bucket: str, week_start: datetime, mode: str, agenda: dict[str, Any]) -> str:
    """Write the agenda JSON. Monday runs *also* write the canonical
    ``agenda.json`` used for cross-week memory."""
    s3 = boto3.client("s3")
    week = _week_dir(week_start)
    mode_key = f"{week}/agenda.{mode}.json"
    body = json.dumps(agenda, indent=2).encode("utf-8")
    s3.put_object(Bucket=bucket, Key=mode_key, Body=body, ContentType="application/json")
    if mode == "monday":
        s3.put_object(Bucket=bucket, Key=f"{week}/agenda.json", Body=body, ContentType="application/json")
    logger.info("saved agenda to s3://%s/%s", bucket, mode_key)
    return mode_key


def save_artifact(bucket: str, week_start: datetime, name: str, data: bytes, content_type: str) -> str:
    key = f"{_week_dir(week_start)}/{name}"
    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
    return key


def save_attachment(
    bucket: str, week_start: datetime, message_id: str,
    filename: str, data: bytes, content_type: str,
) -> str:
    key = f"{_week_dir(week_start)}/attachments/{message_id}/{filename}"
    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
    return key


def list_weeks(bucket: str, limit: int = 52) -> list[str]:
    """Return a sorted (newest first) list of ISO-week directory names that
    have a Monday agenda on disk."""
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    weeks: set[str] = set()
    for page in paginator.paginate(Bucket=bucket, Prefix="runs/"):
        for obj in page.get("Contents", []) or []:
            key = obj["Key"]
            parts = key.split("/")
            if len(parts) >= 3 and parts[2].startswith("agenda"):
                weeks.add(parts[1])
    return sorted(weeks, reverse=True)[:limit]


def load_agenda(bucket: str, iso_week: str, mode: str | None = None) -> dict[str, Any] | None:
    s3 = boto3.client("s3")
    key = f"runs/{iso_week}/agenda{'' if mode is None else '.' + mode}.json"
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except Exception:
        return None
