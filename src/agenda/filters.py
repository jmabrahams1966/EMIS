"""VIP sender + automated-mail filtering.

Both lists live as JSON in S3 so they can be edited without redeploying:

    s3://{bucket}/config/vip_senders.json
        ["jane@example.com", "ceo@example.com", "@board.example.com"]

    s3://{bucket}/config/blocklist.json
        ["no-reply@", "notifications@", "newsletter@",
         "@mailchimp.com", "@constantcontact.com"]

Each entry is matched as a case-insensitive substring against the sender's
email address. VIP senders are *always* surfaced; blocklist senders are
dropped before the prompt is built. VIP wins over blocklist on conflict.
"""
from __future__ import annotations

import json
import logging
from typing import Iterable

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

DEFAULT_BLOCKLIST = [
    "no-reply@", "noreply@", "do-not-reply@", "donotreply@",
    "notifications@", "notification@", "alerts@", "alert@",
    "newsletter@", "marketing@", "promo@", "promotions@",
    "@mailchimp.com", "@sendgrid.net", "@constantcontact.com",
    "@bounce.", "mailer-daemon@",
]


def _read_json(bucket: str, key: str, default: list[str]) -> list[str]:
    if not bucket:
        return default
    try:
        obj = boto3.client("s3").get_object(Bucket=bucket, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            logger.info("no %s in bucket; using default", key)
            return default
        raise


def load_vip(bucket: str) -> list[str]:
    return [s.lower() for s in _read_json(bucket, "config/vip_senders.json", [])]


def load_blocklist(bucket: str) -> list[str]:
    return [s.lower() for s in _read_json(bucket, "config/blocklist.json", DEFAULT_BLOCKLIST)]


def is_vip(sender_email: str, vip_patterns: Iterable[str]) -> bool:
    e = (sender_email or "").lower()
    return any(p in e for p in vip_patterns)


def is_blocked(sender_email: str, blocklist_patterns: Iterable[str]) -> bool:
    e = (sender_email or "").lower()
    return any(p in e for p in blocklist_patterns)


def apply_filters(messages, vip_patterns: list[str], blocklist_patterns: list[str]):
    """Drop blocklisted senders (unless VIP). Returns (filtered, dropped_count)."""
    kept = []
    dropped = 0
    for m in messages:
        if is_vip(m.sender_email, vip_patterns):
            kept.append(m)
        elif is_blocked(m.sender_email, blocklist_patterns):
            dropped += 1
        else:
            kept.append(m)
    logger.info("filters: kept %d, dropped %d", len(kept), dropped)
    return kept, dropped
