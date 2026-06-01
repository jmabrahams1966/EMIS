"""Reply-to-EMIS command handler.

Runs as a separate Lambda every 30 minutes. Polls the user's inbox for
unread replies to the agenda email, parses each via Claude into structured
snooze records, persists them to S3, and marks the email read so we don't
re-process.

Snoozes are written to ``s3://{bucket}/state/snoozes.json`` as a single
accumulating list. ``builder.py`` reads them on the next agenda run and
includes them in the user turn as context — the system prompt tells Claude
to suppress snoozed items from priorities/action_items/follow_ups until
their ``until`` date has passed.

MVP vocabulary: snooze only. Future versions can add drop / done /
delegate.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import anthropic
import boto3
import httpx
from botocore.exceptions import ClientError

from .agenda.builder import _make_client
from .config import load_config
from .graph import auth as graph_auth

logger = logging.getLogger("emis.snooze")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Look back this far for replies. EventBridge runs every 30 min; 24h is
# generous slack to catch replies during overnight outages.
REPLY_LOOKBACK_HOURS = 24

# Snooze records older than this with their `until` date in the past are
# pruned on each run to keep the file small.
SNOOZE_EXPIRY_DAYS = 90


SNOOZE_PARSER_SYSTEM = """\
You parse a user's plain-English email reply into structured snooze commands.

The user receives a weekly or daily agenda email and replies with informal \
instructions like "snooze the Costco thread until Monday" or "snooze \
horizon decision for 2 weeks". Convert each instruction into a JSON object \
with these fields:

- item_match: a short string identifying which agenda item the user means. \
  Quote the most distinctive words verbatim from their reply — usually a \
  thread subject fragment, person's name, vendor, or topic. Don't \
  paraphrase or expand.
- until_iso: the ISO date (YYYY-MM-DD) the snooze expires. Resolve relative \
  expressions ("next Monday", "in 2 weeks", "until Friday") against \
  today's date provided in the user turn. Default to one week if the user \
  doesn't specify a duration.

Only output snooze commands. Ignore other instructions (drop, done, \
delegate) — those aren't supported yet. If you find no valid snooze \
commands, return an empty list.

Don't invent items — if you can't tell what the user means, skip it.
"""


SNOOZE_PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "snoozes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_match": {"type": "string"},
                    "until_iso": {
                        "type": "string",
                        "description": "YYYY-MM-DD",
                    },
                },
                "required": ["item_match", "until_iso"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["snoozes"],
    "additionalProperties": False,
}


@dataclass
class SnoozeRecord:
    item_match: str
    until_iso: str
    snoozed_at: str  # ISO timestamp
    source_message_id: str  # the reply email's Graph ID

    def to_dict(self) -> dict[str, str]:
        return {
            "item_match": self.item_match,
            "until_iso": self.until_iso,
            "snoozed_at": self.snoozed_at,
            "source_message_id": self.source_message_id,
        }


# ── S3 state ───────────────────────────────────────────────────────────────

def _snoozes_key() -> str:
    return "state/snoozes.json"


def load_snoozes(bucket: str) -> list[SnoozeRecord]:
    """Load all snoozes from S3. Returns empty list if file doesn't exist."""
    if not bucket:
        return []
    try:
        obj = boto3.client("s3").get_object(Bucket=bucket, Key=_snoozes_key())
        raw = json.loads(obj["Body"].read().decode("utf-8"))
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return []
        raise
    return [SnoozeRecord(**r) for r in raw]


def save_snoozes(bucket: str, snoozes: list[SnoozeRecord]) -> None:
    data = json.dumps([s.to_dict() for s in snoozes], indent=2)
    boto3.client("s3").put_object(
        Bucket=bucket, Key=_snoozes_key(),
        Body=data.encode("utf-8"), ContentType="application/json",
    )


def active_snoozes(snoozes: list[SnoozeRecord], now: datetime) -> list[SnoozeRecord]:
    """Return snoozes whose ``until_iso`` is still in the future."""
    today_iso = now.date().isoformat()
    return [s for s in snoozes if s.until_iso >= today_iso]


def prune_expired(snoozes: list[SnoozeRecord], now: datetime) -> list[SnoozeRecord]:
    """Drop snoozes whose ``until_iso`` is older than SNOOZE_EXPIRY_DAYS."""
    cutoff = (now - timedelta(days=SNOOZE_EXPIRY_DAYS)).date().isoformat()
    return [s for s in snoozes if s.until_iso >= cutoff]


# ── Inbox polling ──────────────────────────────────────────────────────────

async def _fetch_unread_replies(*, access_token: str, sender: str, since: datetime) -> list[dict]:
    """Pull unread emails from the user to the agenda sender within the window.

    Matches by ``from`` address (the user replying to themselves on the
    agenda thread) and a subject prefix that identifies it as a reply to an
    EMIS email.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Prefer": 'outlook.body-content-type="text"',
    }
    iso = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Replies have "Re:" prefix; the recipient is the agenda sender; isRead is false.
    params = {
        "$top": "50",
        "$filter": (
            f"receivedDateTime ge {iso} "
            f"and isRead eq false "
            f"and from/emailAddress/address eq '{sender}'"
        ),
        "$orderby": "receivedDateTime desc",
        "$select": "id,subject,from,receivedDateTime,body,bodyPreview,webLink",
    }
    url = f"{GRAPH_BASE}/me/messages"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json().get("value", [])


async def _mark_read(*, access_token: str, message_id: str) -> None:
    """Mark a message as read so it isn't re-processed on the next poll."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(
            f"{GRAPH_BASE}/me/messages/{message_id}",
            headers=headers, json={"isRead": True},
        )
        resp.raise_for_status()


# ── Parser ─────────────────────────────────────────────────────────────────

def parse_reply_via_claude(
    *,
    reply_text: str,
    now: datetime,
    api_key: str,
    model: str,
    aws_region: str,
) -> list[dict[str, str]]:
    """Send a reply body to Claude and get back snooze records."""
    client = _make_client(api_key=api_key, model=model, aws_region=aws_region)
    is_bedrock = isinstance(client, anthropic.AnthropicBedrock)

    user_content = (
        f"Today's date: {now.date().isoformat()}\n\n"
        f"User's reply:\n{reply_text}"
    )

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": 4_000,
        "system": SNOOZE_PARSER_SYSTEM,
        "messages": [{"role": "user", "content": user_content}],
    }
    if is_bedrock:
        kwargs["tools"] = [{
            "name": "emit_snoozes",
            "description": "Emit parsed snooze commands.",
            "input_schema": SNOOZE_PARSE_SCHEMA,
        }]
        kwargs["tool_choice"] = {"type": "tool", "name": "emit_snoozes"}
    else:
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {
            "format": {"type": "json_schema", "schema": SNOOZE_PARSE_SCHEMA},
            "effort": "low",  # parsing is simple; don't burn tokens on thinking
        }

    with client.messages.stream(**kwargs) as stream:
        message = stream.get_final_message()

    if is_bedrock:
        block = next(
            (b for b in message.content if b.type == "tool_use" and b.name == "emit_snoozes"),
            None,
        )
        if block is None:
            logger.warning("snooze parser returned no tool_use block")
            return []
        parsed = block.input
    else:
        text_block = next((b for b in message.content if b.type == "text"), None)
        if text_block is None:
            return []
        parsed = json.loads(text_block.text)

    return parsed.get("snoozes", []) or []


# ── Lambda entry ───────────────────────────────────────────────────────────

def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return asyncio.run(_run())


async def _run() -> dict[str, Any]:
    cfg = load_config()
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=REPLY_LOOKBACK_HOURS)

    tokens = await graph_auth.exchange_refresh_token(
        tenant_id=cfg.graph_tenant_id,
        client_id=cfg.graph_client_id,
        client_secret=cfg.graph_client_secret,
        refresh_token=cfg.graph_refresh_token,
    )
    if not cfg.dry_run and tokens.refresh_token != cfg.graph_refresh_token:
        graph_auth.rotate_refresh_token_secret(
            os.environ["GRAPH_SECRET_ID"], tokens.refresh_token
        )
        cfg.graph_refresh_token = tokens.refresh_token

    replies = await _fetch_unread_replies(
        access_token=tokens.access_token,
        sender=cfg.agenda_recipient,  # user replies to their own agenda email
        since=since,
    )
    if not replies:
        return {"status": "ok", "replies": 0, "snoozes_added": 0}

    existing = load_snoozes(cfg.state_bucket) if cfg.state_bucket else []
    existing = prune_expired(existing, now)

    added = 0
    snoozed_at = now.isoformat()
    for reply in replies:
        body = (reply.get("body") or {}).get("content") or reply.get("bodyPreview", "")
        if not body.strip():
            continue
        try:
            parsed = parse_reply_via_claude(
                reply_text=body,
                now=now,
                api_key=cfg.anthropic_api_key,
                model=cfg.anthropic_model,
                aws_region=cfg.aws_region,
            )
        except Exception as exc:
            logger.warning("parse failed for message %s: %s", reply["id"], exc)
            continue
        for s in parsed:
            existing.append(SnoozeRecord(
                item_match=s["item_match"],
                until_iso=s["until_iso"],
                snoozed_at=snoozed_at,
                source_message_id=reply["id"],
            ))
            added += 1
        if not cfg.dry_run:
            try:
                await _mark_read(access_token=tokens.access_token, message_id=reply["id"])
            except Exception as exc:
                logger.warning("mark-read failed for %s: %s", reply["id"], exc)

    if cfg.state_bucket and not cfg.dry_run:
        save_snoozes(cfg.state_bucket, existing)

    logger.info(
        "snooze poll: replies=%d snoozes_added=%d total_active=%d",
        len(replies), added, len(active_snoozes(existing, now)),
    )
    return {
        "status": "ok",
        "replies": len(replies),
        "snoozes_added": added,
        "total_active": len(active_snoozes(existing, now)),
    }


if __name__ == "__main__":
    print(asyncio.run(_run()))
