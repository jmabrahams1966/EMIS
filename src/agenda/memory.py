"""Cross-week memory — load prior agendas so Claude can mark items as
carried-over, resolved, or stale.

The last 4 weeks of agenda JSON files are loaded from
``s3://{bucket}/runs/{YYYY-WW}/agenda.json`` and rendered into a compact
"prior week" block that goes into the user turn alongside this week's email.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

LOOKBACK_WEEKS = 4


def _week_key(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"runs/{iso.year:04d}-W{iso.week:02d}/agenda.json"


def load_prior_agendas(bucket: str, current_week: datetime) -> list[dict[str, Any]]:
    """Return up to ``LOOKBACK_WEEKS`` previous agendas, newest first.

    Each entry is ``{"iso_week": "YYYY-WW", "agenda": {...}}``.
    """
    if not bucket:
        return []
    out: list[dict[str, Any]] = []
    s3 = boto3.client("s3")
    for weeks_back in range(1, LOOKBACK_WEEKS + 1):
        target = current_week - timedelta(days=7 * weeks_back)
        key = _week_key(target)
        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
            agenda = json.loads(obj["Body"].read().decode("utf-8"))
            iso = target.isocalendar()
            out.append({
                "iso_week": f"{iso.year:04d}-W{iso.week:02d}",
                "agenda": agenda,
            })
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                continue
            logger.warning("S3 get failed for %s: %s", key, exc)
    logger.info("loaded %d prior agendas for memory", len(out))
    return out


def render_memory_block(prior: list[dict[str, Any]]) -> str:
    """Render the prior agendas into a compact text block for the prompt.

    Only the open-ended sections (priorities, action_items, follow_ups) are
    included — meetings and FYI from prior weeks aren't actionable.
    """
    if not prior:
        return "No prior agendas on record. This is the first run.\n"

    lines: list[str] = ["===== PRIOR AGENDAS (oldest to newest) ====="]
    for entry in reversed(prior):
        iso = entry["iso_week"]
        a = entry["agenda"]
        lines.append(f"\n--- Week {iso} ---")
        if a.get("priorities"):
            lines.append("Priorities:")
            for p in a["priorities"]:
                lines.append(f"  • {p.get('title', '')}")
        if a.get("action_items"):
            lines.append("Action items:")
            for it in a["action_items"]:
                owner = it.get("owner", "")
                lines.append(f"  • [{owner}] {it.get('task', '')}")
        if a.get("follow_ups"):
            lines.append("Follow-ups:")
            for f in a["follow_ups"]:
                cp = f.get("counterparty", "")
                lines.append(f"  • [{cp}] {f.get('thread', '')} — {f.get('ask', '')}")
    lines.append("")
    return "\n".join(lines) + "\n"
