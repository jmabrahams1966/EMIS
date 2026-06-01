"""Generate per-meeting briefs from the last 4 weeks of mail history.

Runs as ``mode=morning`` (separate from the weekly Monday/Wednesday/Friday
agenda). For each calendar event today with non-self attendees, EMIS pulls
recent threads where the attendees appear as sender or recipient, then asks
Claude to summarize the state of that relationship for each meeting.

Output: a list of three-line briefs per meeting (last_commitments / open_asks
/ context).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import anthropic

from ..graph.calendar import CalendarEvent
from ..graph.mail import Message
from .builder import _make_client
from .threading import group_into_threads

logger = logging.getLogger(__name__)

# Per-meeting context budget — same per-section caps apply as the weekly
# agenda. Briefs are intentionally short, so we don't need a huge prompt.
MAX_BODY_CHARS = 1_500
MAX_TOTAL_CHARS = 400_000


BRIEF_SYSTEM_PROMPT = """\
You are the user's chief-of-staff preparing them for today's meetings. For \
each meeting, scan the relevant mail history (provided after the meeting \
block) and produce a brief that helps the user walk in informed.

Each brief has three short lines:
- last_commitments: what the user has recently promised these attendees, or \
  what attendees promised the user. One sentence. Empty string if none.
- open_asks: anything still pending between the user and any attendee — \
  questions, waiting-ons, blockers. One sentence. Empty string if none.
- context: 1-2 sentences describing the most recent thread state. What is \
  the meeting actually about? Where did the last exchange leave off?

If there's truly no mail history with any attendee, all three lines may be \
empty strings. Don't fabricate context.

Populate meeting_web_link with the URL from the calendar event's `web:` \
line, copied verbatim. If unknown, use an empty string. Plain text in all \
string fields — no markdown.
"""


BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "briefs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "meeting_subject": {"type": "string"},
                    "meeting_time": {"type": "string"},
                    "meeting_web_link": {"type": "string"},
                    "last_commitments": {"type": "string"},
                    "open_asks": {"type": "string"},
                    "context": {"type": "string"},
                },
                "required": [
                    "meeting_subject", "meeting_time", "meeting_web_link",
                    "last_commitments", "open_asks", "context",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["briefs"],
    "additionalProperties": False,
}


@dataclass
class BriefsResult:
    briefs: list[dict[str, Any]]
    input_tokens: int
    output_tokens: int


def _attendee_set(event: CalendarEvent, self_email: str) -> set[str]:
    """Return the attendee emails for an event, excluding the user."""
    self_lc = (self_email or "").lower()
    return {
        a.lower() for a in event.attendees
        if a and a.lower() != self_lc
    }


def _threads_with(messages: list[Message], attendees: set[str]) -> list:
    """Return threads where any message touched one of ``attendees``."""
    matched: list[Message] = []
    for m in messages:
        participants = {
            (m.sender_email or "").lower(),
            *(r.lower() for r in m.to_recipients),
            *(r.lower() for r in m.cc_recipients),
        }
        if participants & attendees:
            matched.append(m)
    if not matched:
        return []
    return group_into_threads(matched, vip_patterns=[])


def _render_meeting_block(idx: int, event: CalendarEvent, threads: list) -> str:
    when = event.start.strftime("%a %Y-%m-%d %H:%MZ")
    end = event.end.strftime("%H:%MZ")
    attendees_str = ", ".join(a for a in event.attendees if a)[:200]
    head = [
        f"\n===== MEETING {idx}: {event.subject} =====",
        f"Time: {when}–{end}",
        f"Attendees: {attendees_str}",
    ]
    if event.location:
        head.append(f"Location: {event.location}")
    if event.web_link:
        head.append(f"web: {event.web_link}")
    if not threads:
        head.append("\n(no recent mail with these attendees)")
        return "\n".join(head) + "\n"
    head.append(f"\nRecent threads ({len(threads)}):")
    parts = ["\n".join(head)]
    used = sum(len(p) for p in parts)
    for t in threads:
        block_lines = [
            f"\n--- {t.subject} ({len(t.messages)} msgs, latest {t.latest_received.isoformat() if t.latest_received else '?'}) ---",
        ]
        for msg in t.messages[-3:]:  # last 3 messages per thread is plenty for a brief
            body = (msg.body_text or msg.preview or "").strip().replace("\n", " ")
            if len(body) > MAX_BODY_CHARS:
                body = body[:MAX_BODY_CHARS] + "…"
            block_lines.append(f"  {msg.sender} @ {msg.received_at.date()}: {body}")
        block = "\n".join(block_lines)
        if used + len(block) > MAX_TOTAL_CHARS // max(len(threads), 1):
            parts.append("\n  [more threads truncated to fit budget]")
            break
        parts.append(block)
        used += len(block)
    return "".join(parts) + "\n"


def _render_user_turn(
    *,
    events: list[CalendarEvent],
    per_meeting_threads: list[list],
    now: datetime,
) -> str:
    header = (
        f"Today's date: {now.date().isoformat()}\n"
        f"Meetings on calendar today: {len(events)}\n"
        f"Produce one brief per meeting in the same order.\n"
    )
    chunks = [header]
    for idx, (ev, threads) in enumerate(zip(events, per_meeting_threads), start=1):
        chunks.append(_render_meeting_block(idx, ev, threads))
    return "".join(chunks)


def build_briefs(
    *,
    events: list[CalendarEvent],
    messages: list[Message],
    self_email: str,
    now: datetime,
    api_key: str,
    model: str = "claude-opus-4-7",
    aws_region: str = "us-east-1",
) -> BriefsResult:
    """Generate briefs for every meeting today that has external attendees.

    ``events`` is today's calendar; ``messages`` is the last ~4 weeks of mail
    (already filtered + folder-scanned by the caller).
    """
    if not events:
        return BriefsResult(briefs=[], input_tokens=0, output_tokens=0)

    # Per-meeting thread lookup (same set order as events for the prompt)
    per_meeting_threads = []
    for ev in events:
        attendees = _attendee_set(ev, self_email)
        threads = _threads_with(messages, attendees) if attendees else []
        per_meeting_threads.append(threads)

    user_content = _render_user_turn(
        events=events,
        per_meeting_threads=per_meeting_threads,
        now=now,
    )

    client = _make_client(api_key=api_key, model=model, aws_region=aws_region)
    is_bedrock = isinstance(client, anthropic.AnthropicBedrock)

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": 32_000,
        "system": BRIEF_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_content}],
    }
    if is_bedrock:
        # Bedrock rejects `thinking` when tool_choice forces a specific tool,
        # so we drop thinking on this path (see builder.py for the same note).
        kwargs["tools"] = [{
            "name": "emit_briefs",
            "description": "Emit the list of per-meeting briefs.",
            "input_schema": BRIEF_SCHEMA,
        }]
        kwargs["tool_choice"] = {"type": "tool", "name": "emit_briefs"}
    else:
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {
            "format": {"type": "json_schema", "schema": BRIEF_SCHEMA},
            "effort": "high",
        }

    with client.messages.stream(**kwargs) as stream:
        message = stream.get_final_message()

    if is_bedrock:
        tool_block = next(
            (b for b in message.content if b.type == "tool_use" and b.name == "emit_briefs"),
            None,
        )
        if tool_block is None:
            raise RuntimeError(
                f"Bedrock returned no emit_briefs tool_use block "
                f"(stop_reason={message.stop_reason})"
            )
        result = tool_block.input
    else:
        text_block = next((b for b in message.content if b.type == "text"), None)
        if text_block is None:
            raise RuntimeError("Claude returned no text block for the briefs")
        result = json.loads(text_block.text)

    briefs = result.get("briefs", [])
    usage = message.usage
    logger.info(
        "briefs built: meetings=%d input=%d output=%d stop=%s",
        len(briefs), usage.input_tokens, usage.output_tokens, message.stop_reason,
    )
    return BriefsResult(
        briefs=briefs,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )
