"""Reply-to-EMIS command handler.

Polls the user's inbox every 30 minutes for unread replies to the agenda
email, parses each body via Claude into structured closure records, and
persists them to ``s3://<bucket>/state/closures.json``. The agenda builder
reads these on the next run and respects them.

Three closure verbs supported:

- ``snooze``: hide the item until ``until_iso``; resurfaces once the date
  passes.
- ``done``: mark as completed. The agenda treats incoming threads about
  this item as ``resolved`` (a closure signal) rather than
  ``carried_over``. Also appears in the dashboard's History tab.
- ``drop``: permanently suppress. Never surfaces again. No History entry.

The polling Lambda is invoked by EventBridge (`SnoozePollSchedule`) but the
naming has stuck for backward compat; functionally it handles all three
verbs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import anthropic
import boto3
import httpx
from botocore.exceptions import ClientError

from .agenda.builder import _make_client
from .config import load_config
from .graph import auth as graph_auth

logger = logging.getLogger("emis.closures")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
REPLY_LOOKBACK_HOURS = 24
# Snoozes whose until date is past + this much get pruned. Done/drop records
# are retained longer (see DONE_RETAIN_DAYS) since they feed the History tab.
SNOOZE_EXPIRY_DAYS = 90
DONE_RETAIN_DAYS = 730  # ~2 years of completion history
DROP_RETAIN_DAYS = 365


CLOSURES_PARSER_SYSTEM = """\
You parse a user's plain-English email reply into structured closure \
commands. The user receives weekly or daily agenda emails and replies with \
informal instructions like:

- "snooze the Costco thread until Monday"
- "done with the malpractice premium"
- "drop the FYI about the all-hands meeting"
- "Already paid Rising Fastball" (this is a done command)

Three actions are supported:

- ``snooze``: defer until a specific date. Fill ``until_iso`` with the ISO \
  date the snooze expires. Resolve relative phrases ("next Monday", "for 2 \
  weeks") against today's date in the user turn. Default to one week if no \
  duration given.
- ``done``: the user has completed (or confirmed handled) the item. Leave \
  ``until_iso`` empty. "Did", "done", "finished", "handled", "paid", \
  "sent", "called", "scheduled", "confirmed" are all done signals.
- ``drop``: the user wants the item permanently suppressed. Leave \
  ``until_iso`` empty. "Drop", "skip", "ignore", "not relevant", "don't \
  care" are drop signals.

For each command, ``item_match`` is a short string identifying the agenda \
item — quote the most distinctive verbatim words (vendor name, person, \
amount, thread subject fragment). Don't paraphrase. If you cannot tell \
which item the user means, skip it entirely.

Only emit valid commands. If the reply has no recognizable closure \
instructions, return an empty list.
"""


CLOSURES_PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "commands": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["snooze", "done", "drop"]},
                    "item_match": {"type": "string"},
                    "until_iso": {
                        "type": "string",
                        "description": "YYYY-MM-DD for snooze; empty string for done/drop",
                    },
                },
                "required": ["action", "item_match", "until_iso"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["commands"],
    "additionalProperties": False,
}


# ── Records ────────────────────────────────────────────────────────────────

@dataclass
class SnoozeRecord:
    item_match: str
    until_iso: str       # YYYY-MM-DD
    snoozed_at: str      # ISO timestamp
    source_message_id: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class DoneRecord:
    item_match: str
    completed_at: str    # ISO timestamp
    source: str          # "reply_command" | "todo_sync"
    source_id: str       # message id or To Do task id

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class DropRecord:
    item_match: str
    dropped_at: str      # ISO timestamp
    source_message_id: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class Closures:
    snoozes: list[SnoozeRecord] = field(default_factory=list)
    done: list[DoneRecord] = field(default_factory=list)
    drops: list[DropRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, list[dict[str, str]]]:
        return {
            "snoozes": [s.to_dict() for s in self.snoozes],
            "done": [d.to_dict() for d in self.done],
            "drops": [d.to_dict() for d in self.drops],
        }


# ── S3 state ───────────────────────────────────────────────────────────────

def _closures_key() -> str:
    return "state/closures.json"


def load_closures(bucket: str) -> Closures:
    """Load all closures from S3. Returns empty Closures if file missing."""
    if not bucket:
        return Closures()
    try:
        obj = boto3.client("s3").get_object(Bucket=bucket, Key=_closures_key())
        raw = json.loads(obj["Body"].read().decode("utf-8"))
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return Closures()
        raise
    return Closures(
        snoozes=[SnoozeRecord(**s) for s in raw.get("snoozes", [])],
        done=[DoneRecord(**d) for d in raw.get("done", [])],
        drops=[DropRecord(**d) for d in raw.get("drops", [])],
    )


def save_closures(bucket: str, closures: Closures) -> None:
    if not bucket:
        return
    data = json.dumps(closures.to_dict(), indent=2)
    boto3.client("s3").put_object(
        Bucket=bucket, Key=_closures_key(),
        Body=data.encode("utf-8"), ContentType="application/json",
    )


def prune_closures(closures: Closures, now: datetime) -> Closures:
    """Drop very old records to keep the state file small."""
    today_iso = now.date().isoformat()
    snooze_cutoff = (now - timedelta(days=SNOOZE_EXPIRY_DAYS)).date().isoformat()
    done_cutoff = (now - timedelta(days=DONE_RETAIN_DAYS)).isoformat()
    drop_cutoff = (now - timedelta(days=DROP_RETAIN_DAYS)).isoformat()
    return Closures(
        snoozes=[s for s in closures.snoozes if s.until_iso >= snooze_cutoff],
        done=[d for d in closures.done if d.completed_at >= done_cutoff],
        drops=[d for d in closures.drops if d.dropped_at >= drop_cutoff],
    )


def active_snoozes(closures: Closures, now: datetime) -> list[SnoozeRecord]:
    today_iso = now.date().isoformat()
    return [s for s in closures.snoozes if s.until_iso >= today_iso]


# ── Inbox polling ──────────────────────────────────────────────────────────

async def _fetch_unread_replies(*, access_token: str, sender: str, since: datetime) -> list[dict]:
    """Pull unread emails from the user to the agenda sender within the window."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Prefer": 'outlook.body-content-type="text"',
    }
    iso = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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
    """Send a reply body to Claude and get back closure commands."""
    client = _make_client(api_key=api_key, model=model, aws_region=aws_region)
    is_bedrock = isinstance(client, anthropic.AnthropicBedrock)
    user_content = (
        f"Today's date: {now.date().isoformat()}\n\n"
        f"User's reply:\n{reply_text}"
    )
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": 4_000,
        "system": CLOSURES_PARSER_SYSTEM,
        "messages": [{"role": "user", "content": user_content}],
    }
    if is_bedrock:
        kwargs["tools"] = [{
            "name": "emit_closures",
            "description": "Emit parsed closure commands.",
            "input_schema": CLOSURES_PARSE_SCHEMA,
        }]
        kwargs["tool_choice"] = {"type": "tool", "name": "emit_closures"}
    else:
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {
            "format": {"type": "json_schema", "schema": CLOSURES_PARSE_SCHEMA},
            "effort": "low",
        }

    with client.messages.stream(**kwargs) as stream:
        message = stream.get_final_message()

    if is_bedrock:
        block = next(
            (b for b in message.content if b.type == "tool_use" and b.name == "emit_closures"),
            None,
        )
        if block is None:
            return []
        parsed = block.input
    else:
        text_block = next((b for b in message.content if b.type == "text"), None)
        if text_block is None:
            return []
        parsed = json.loads(text_block.text)

    return parsed.get("commands", []) or []


def apply_commands(
    *, closures: Closures, commands: list[dict[str, str]],
    now: datetime, source_message_id: str,
) -> int:
    """Append parsed commands onto the closures store. Returns count added."""
    added = 0
    ts = now.isoformat()
    for cmd in commands:
        action = cmd.get("action")
        item = cmd.get("item_match", "").strip()
        if not item:
            continue
        if action == "snooze":
            until = cmd.get("until_iso", "").strip()
            if not until:
                continue
            closures.snoozes.append(SnoozeRecord(
                item_match=item, until_iso=until,
                snoozed_at=ts, source_message_id=source_message_id,
            ))
            added += 1
        elif action == "done":
            closures.done.append(DoneRecord(
                item_match=item, completed_at=ts,
                source="reply_command", source_id=source_message_id,
            ))
            added += 1
        elif action == "drop":
            closures.drops.append(DropRecord(
                item_match=item, dropped_at=ts,
                source_message_id=source_message_id,
            ))
            added += 1
    return added


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
        sender=cfg.agenda_recipient,
        since=since,
    )
    if not replies:
        return {"status": "ok", "replies": 0, "added": 0}

    closures = prune_closures(load_closures(cfg.state_bucket), now)
    added = 0
    for reply in replies:
        body = (reply.get("body") or {}).get("content") or reply.get("bodyPreview", "")
        if not body.strip():
            continue
        try:
            commands = parse_reply_via_claude(
                reply_text=body, now=now,
                api_key=cfg.anthropic_api_key,
                model=cfg.anthropic_model,
                aws_region=cfg.aws_region,
            )
        except Exception as exc:
            logger.warning("parse failed for message %s: %s", reply["id"], exc)
            continue
        added += apply_commands(
            closures=closures, commands=commands,
            now=now, source_message_id=reply["id"],
        )
        if not cfg.dry_run:
            try:
                await _mark_read(access_token=tokens.access_token, message_id=reply["id"])
            except Exception as exc:
                logger.warning("mark-read failed for %s: %s", reply["id"], exc)

    if not cfg.dry_run:
        save_closures(cfg.state_bucket, closures)

    logger.info(
        "closures poll: replies=%d added=%d snoozes=%d done=%d drops=%d",
        len(replies), added,
        len(closures.snoozes), len(closures.done), len(closures.drops),
    )
    return {
        "status": "ok",
        "replies": len(replies),
        "added": added,
        "snoozes_total": len(closures.snoozes),
        "done_total": len(closures.done),
        "drops_total": len(closures.drops),
    }


if __name__ == "__main__":
    print(asyncio.run(_run()))
