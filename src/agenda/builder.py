"""Call Claude to convert a week of email + calendar + memory into a
structured agenda.

Caching strategy:
  - System prompt is fully frozen and shared across all three modes (Monday,
    Wednesday, Friday) so the cache prefix is byte-identical.
  - ``cache_control: ephemeral`` on the last system block caches system text
    + (empty) tools list. Reads on every run after the first.
  - All volatile data — mode header, week range, calendar, sent mail, threads,
    prior agendas — goes in the user turn after the breakpoint.
  - ``cache_creation_input_tokens`` / ``cache_read_input_tokens`` are logged.

Output: structured JSON via ``output_config.format``.
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
from .memory import render_memory_block
from .prompts import AGENDA_SCHEMA, MODE_NOTES, SYSTEM_PROMPT
from .threading import Thread

logger = logging.getLogger(__name__)

MAX_BODY_CHARS = 3_500
MAX_ATTACHMENT_CHARS = 5_000
MAX_TOTAL_CHARS = 700_000


@dataclass
class AgendaResult:
    agenda: dict[str, Any]
    raw_text: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int


def build_agenda(
    *,
    mode: str,
    threads: list[Thread],
    sent_messages: list[Message],
    calendar_events: list[CalendarEvent],
    prior_agendas: list[dict[str, Any]],
    attachment_texts: dict[str, list[tuple[str, str]]],
    week_start: datetime,
    week_end: datetime,
    api_key: str,
    model: str = "claude-opus-4-8",
) -> AgendaResult:
    """Generate the agenda for ``mode`` in (monday, wednesday, friday)."""
    if mode not in MODE_NOTES:
        raise ValueError(f"unknown mode: {mode!r}; expected one of {list(MODE_NOTES)}")

    client = anthropic.Anthropic(api_key=api_key)

    user_content = _render_user_turn(
        mode=mode, threads=threads, sent_messages=sent_messages,
        calendar_events=calendar_events, prior_agendas=prior_agendas,
        attachment_texts=attachment_texts,
        week_start=week_start, week_end=week_end,
    )

    with client.messages.stream(
        model=model,
        max_tokens=8_000,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[{"role": "user", "content": user_content}],
        thinking={"type": "adaptive"},
        output_config={
            "format": {"type": "json_schema", "schema": AGENDA_SCHEMA},
            "effort": "high",
        },
    ) as stream:
        message = stream.get_final_message()

    text_block = next((b for b in message.content if b.type == "text"), None)
    if text_block is None:
        raise RuntimeError("Claude returned no text block for the agenda")
    try:
        agenda = json.loads(text_block.text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Claude returned invalid JSON: {exc}") from exc

    usage = message.usage
    logger.info(
        "agenda built [%s]: input=%d output=%d cache_read=%d cache_write=%d stop=%s",
        mode, usage.input_tokens, usage.output_tokens,
        usage.cache_read_input_tokens or 0,
        usage.cache_creation_input_tokens or 0,
        message.stop_reason,
    )

    return AgendaResult(
        agenda=agenda, raw_text=text_block.text,
        input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
        cache_creation_tokens=usage.cache_creation_input_tokens or 0,
    )


# ── User turn rendering ────────────────────────────────────────────────────

def _render_user_turn(
    *,
    mode: str,
    threads: list[Thread],
    sent_messages: list[Message],
    calendar_events: list[CalendarEvent],
    prior_agendas: list[dict[str, Any]],
    attachment_texts: dict[str, list[tuple[str, str]]],
    week_start: datetime,
    week_end: datetime,
) -> str:
    header = (
        f"{MODE_NOTES[mode]}\n\n"
        f"Window: {week_start.date()} to {week_end.date()} (UTC).\n"
        f"Threads: {len(threads)}  "
        f"Sent: {len(sent_messages)}  "
        f"Calendar events: {len(calendar_events)}\n"
        f"Today's date: {week_end.date().isoformat()}\n"
    )
    chunks: list[str] = [header]
    chunks.append(render_memory_block(prior_agendas))
    chunks.append(_render_calendar(calendar_events))
    chunks.append(_render_sent(sent_messages))
    chunks.append("===== INCOMING THREADS =====\n")

    total = sum(len(c) for c in chunks)
    truncated_at = None
    for idx, t in enumerate(threads, start=1):
        rendered = _render_thread(t, attachment_texts)
        if total + len(rendered) > MAX_TOTAL_CHARS:
            truncated_at = idx
            break
        chunks.append(rendered)
        total += len(rendered)
    if truncated_at is not None:
        chunks.append(
            f"\n[Truncated after thread {truncated_at - 1} of {len(threads)} "
            f"to fit context budget.]\n"
        )
    return "".join(chunks)


def _render_calendar(events: list[CalendarEvent]) -> str:
    if not events:
        return "===== CALENDAR =====\n(none scheduled)\n\n"
    lines = ["===== CALENDAR ====="]
    for ev in events:
        when = ev.start.strftime("%a %Y-%m-%d %H:%MZ")
        end = ev.end.strftime("%H:%MZ")
        attendees = ", ".join(a for a in ev.attendees if a)[:200]
        loc = f"  Location: {ev.location}" if ev.location else ""
        lines.append(
            f"  • {when}–{end}  {ev.subject}"
            + (f"  [{ev.importance}]" if ev.importance and ev.importance != "normal" else "")
        )
        if attendees:
            lines.append(f"    Attendees: {attendees}")
        if loc:
            lines.append(loc)
    lines.append("")
    return "\n".join(lines) + "\n"


def _render_sent(messages: list[Message]) -> str:
    if not messages:
        return "===== SENT MAIL =====\n(none)\n\n"
    lines = ["===== SENT MAIL (commitments you may have made) ====="]
    for m in messages[:50]:
        body = (m.body_text or m.preview or "").strip().replace("\n", " ")
        if len(body) > 400:
            body = body[:400] + "…"
        lines.append(
            f"\n--- TO {', '.join(m.to_recipients)[:120]} ---\n"
            f"Subject: {m.subject}\n"
            f"Sent: {m.received_at.isoformat()}\n"
            f"{body}"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def _render_thread(t: Thread, attachment_texts: dict[str, list[tuple[str, str]]]) -> str:
    flag = " [VIP]" if t.is_vip else ""
    head = [
        f"\n--- THREAD {t.conversation_id[-16:]}{flag} ---",
        f"Subject: {t.subject}",
        f"Participants: {', '.join(t.participants[:8])}"
        + (f"  (+{len(t.participants) - 8} more)" if len(t.participants) > 8 else ""),
        f"Messages: {len(t.messages)}  "
        f"Latest: {t.latest_received.isoformat() if t.latest_received else '?'}",
    ]
    lines = ["\n".join(head)]
    for msg in t.messages:
        body = (msg.body_text or msg.preview or "").strip()
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS] + "\n…[body truncated]"
        lines.append(
            f"\n  >> {msg.sender} <{msg.sender_email}> @ {msg.received_at.isoformat()}\n"
            f"     {body}"
        )
        for name, text in attachment_texts.get(msg.id, []):
            if not text:
                continue
            snippet = text if len(text) <= MAX_ATTACHMENT_CHARS else (
                text[:MAX_ATTACHMENT_CHARS] + "\n…[attachment truncated]"
            )
            lines.append(f"\n     ATTACHMENT {name}:\n     {snippet}")
    return "".join(lines) + "\n"
