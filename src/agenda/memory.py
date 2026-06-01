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


def render_memory_block(prior: list[dict[str, Any]], max_chars: int = 100_000) -> str:
    """Render the prior agendas into a compact text block for the prompt.

    Only the open-ended sections (priorities, action_items, follow_ups) are
    included — meetings and FYI from prior weeks aren't actionable. Truncated
    at ``max_chars`` (newest week kept).
    """
    if not prior:
        return "No prior agendas on record. This is the first run.\n"

    rendered = ["===== PRIOR AGENDAS (oldest to newest) ====="]
    # ``prior`` is newest-first; reverse to render oldest-first.
    for entry in reversed(prior):
        iso = entry["iso_week"]
        a = entry["agenda"]
        block = [f"\n--- Week {iso} ---"]
        if a.get("priorities"):
            block.append("Priorities:")
            for p in a["priorities"]:
                block.append(f"  • {p.get('title', '')}")
        if a.get("action_items"):
            block.append("Action items:")
            for it in a["action_items"]:
                owner = it.get("owner", "")
                block.append(f"  • [{owner}] {it.get('task', '')}")
        if a.get("follow_ups"):
            block.append("Follow-ups:")
            for f in a["follow_ups"]:
                cp = f.get("counterparty", "")
                block.append(f"  • [{cp}] {f.get('thread', '')} — {f.get('ask', '')}")
        rendered.append("\n".join(block))

    # Drop oldest weeks first if the block is too big; the prompt cares more
    # about recent context. rendered[0] is the header.
    out = "\n".join(rendered) + "\n"
    dropped = 0
    while len(out) > max_chars and len(rendered) > 2:
        del rendered[1]
        dropped += 1
        out = "\n".join(rendered) + "\n"
    if dropped:
        rendered.insert(1, f"[+{dropped} older week(s) truncated to fit budget]")
        out = "\n".join(rendered) + "\n"
    return out
