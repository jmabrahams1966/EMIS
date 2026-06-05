"""Call Claude to convert a week of email + calendar + memory into a
structured agenda.

Output: structured JSON via ``output_config.format``.

On prompt caching: we don't bother. The system prompt is ~600 tokens, well
under Opus 4.7's 4096-token cache-write minimum, and runs are 48+ hours
apart — longer than any cache TTL. Reads will always miss, so adding
``cache_control`` would just pay the write premium for nothing.
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
# Per-section caps so a single noisy section can't crowd out the rest.
MAX_CALENDAR_CHARS = 50_000
MAX_SENT_CHARS = 40_000
MAX_MEMORY_CHARS = 100_000


@dataclass
class AgendaResult:
    agenda: dict[str, Any]
    raw_text: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int


def _make_client(*, api_key: str, model: str, aws_region: str):
    """Return either an Anthropic public-API client or a Bedrock client.

    Model IDs prefixed with ``anthropic.`` or the cross-region inference
    prefixes (``us.``, ``eu.``, ``apac.``) route through Bedrock. AWS creds
    come from the standard boto3 chain — environment, profile, or task role.
    Anything else uses the public Anthropic API with ``api_key``.
    """
    if model.startswith(("anthropic.", "us.anthropic.", "eu.anthropic.", "apac.anthropic.")):
        return anthropic.AnthropicBedrock(aws_region=aws_region)
    return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()


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
    model: str = "claude-opus-4-7",
    aws_region: str = "us-east-1",
    closures: dict[str, list[dict[str, str]]] | None = None,
    user_notes: dict[str, str] | None = None,
    user_pins: list[str] | None = None,
) -> AgendaResult:
    """Generate the agenda for ``mode`` in (monday, wednesday, friday)."""
    if mode not in MODE_NOTES:
        raise ValueError(f"unknown mode: {mode!r}; expected one of {list(MODE_NOTES)}")

    client = _make_client(api_key=api_key, model=model, aws_region=aws_region)
    is_bedrock = isinstance(client, anthropic.AnthropicBedrock)

    user_content = _render_user_turn(
        mode=mode, threads=threads, sent_messages=sent_messages,
        calendar_events=calendar_events, prior_agendas=prior_agendas,
        attachment_texts=attachment_texts,
        week_start=week_start, week_end=week_end,
        closures=closures or {"snoozes": [], "done": [], "drops": []},
        user_notes=user_notes or {},
        user_pins=user_pins or [],
    )

    # 64K gives the model room to produce the full agenda without truncating;
    # we're streaming so HTTP timeouts aren't a concern.
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": 64_000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_content}],
    }
    if is_bedrock:
        # Bedrock doesn't accept output_config.format yet — force a tool call
        # with the same schema as input_schema; the agenda comes back as the
        # tool_use block's input dict. Bedrock also rejects `thinking` when
        # tool_choice forces a specific tool, so adaptive thinking is dropped
        # on this path — quality is still good enough for synthesis-to-JSON.
        kwargs["tools"] = [{
            "name": "build_agenda",
            "description": "Emit the structured agenda for the week.",
            "input_schema": AGENDA_SCHEMA,
        }]
        kwargs["tool_choice"] = {"type": "tool", "name": "build_agenda"}
    else:
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {
            "format": {"type": "json_schema", "schema": AGENDA_SCHEMA},
            "effort": "high",
        }

    with client.messages.stream(**kwargs) as stream:
        message = stream.get_final_message()

    if is_bedrock:
        tool_block = next(
            (b for b in message.content if b.type == "tool_use" and b.name == "build_agenda"),
            None,
        )
        if tool_block is None:
            raise RuntimeError(
                f"Bedrock returned no build_agenda tool_use block "
                f"(stop_reason={message.stop_reason})"
            )
        # tool_block.input is already a parsed dict matching AGENDA_SCHEMA
        agenda = tool_block.input
    else:
        text_block = next((b for b in message.content if b.type == "text"), None)
        if text_block is None:
            raise RuntimeError("Claude returned no text block for the agenda")
        try:
            agenda = json.loads(text_block.text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Claude returned invalid JSON "
                f"(stop_reason={message.stop_reason}, "
                f"output_tokens={message.usage.output_tokens}, "
                f"max_tokens=64000): {exc}"
            ) from exc

    usage = message.usage
    logger.info(
        "agenda built [%s]: input=%d output=%d cache_read=%d cache_write=%d stop=%s",
        mode, usage.input_tokens, usage.output_tokens,
        usage.cache_read_input_tokens or 0,
        usage.cache_creation_input_tokens or 0,
        message.stop_reason,
    )

    # raw_text is useful for logging/debugging — for tool_use we serialize
    # the parsed input back to JSON; for text blocks we keep what came back.
    raw_text = json.dumps(agenda) if is_bedrock else text_block.text
    return AgendaResult(
        agenda=agenda, raw_text=raw_text,
        input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
        cache_creation_tokens=usage.cache_creation_input_tokens or 0,
    )


# ── User turn rendering ────────────────────────────────────────────────────

def _render_closures(closures: dict[str, list[dict[str, str]]]) -> str:
    snoozes = closures.get("snoozes", [])
    done = closures.get("done", [])
    drops = closures.get("drops", [])
    if not (snoozes or done or drops):
        return ""
    lines = ["===== USER-DEFINED CLOSURES ====="]
    if snoozes:
        lines.append("Snoozed (suppress until date):")
        for s in snoozes:
            lines.append(f"  - {s['item_match']} until {s['until_iso']}")
    if done:
        lines.append("Done (treat related threads as resolved):")
        for d in done:
            # completed_at is an ISO timestamp; only display the date.
            completed_date = d.get("completed_at", "")[:10]
            lines.append(f"  - {d['item_match']} (completed {completed_date})")
    if drops:
        lines.append("Dropped (never resurface):")
        for d in drops:
            lines.append(f"  - {d['item_match']}")
    lines.append("")
    return "\n".join(lines) + "\n"


def _render_user_extras(notes: dict[str, str], pins: list[str]) -> str:
    """Render USER_NOTES and USER_PINS blocks the prompt will consume."""
    out: list[str] = []
    pins = [p for p in pins if p.strip()]
    if pins:
        out.append("===== USER_PINS =====")
        out.append(
            "These items MUST appear as priorities in the agenda regardless "
            "of natural ranking. Set pinned: true on the matching priority."
        )
        for p in pins:
            out.append(f"  - {p}")
        out.append("")
    notes = {k.strip(): v.strip() for k, v in (notes or {}).items() if k.strip() and v.strip()}
    if notes:
        out.append("===== USER_NOTES =====")
        out.append(
            "Per-item notes from the user. Copy each note verbatim into the "
            "matching action_item's user_note field. Treat as ground truth."
        )
        for k, v in notes.items():
            out.append(f"  - {k}: {v}")
        out.append("")
    return "\n".join(out) + ("\n" if out else "")


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
    closures: dict[str, list[dict[str, str]]],
    user_notes: dict[str, str] | None = None,
    user_pins: list[str] | None = None,
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
    chunks.append(_render_closures(closures))
    chunks.append(_render_user_extras(user_notes or {}, user_pins or []))
    chunks.append(render_memory_block(prior_agendas, max_chars=MAX_MEMORY_CHARS))
    chunks.append(_render_calendar(calendar_events, max_chars=MAX_CALENDAR_CHARS))
    chunks.append(_render_sent(sent_messages, max_chars=MAX_SENT_CHARS))
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


def _render_calendar(events: list[CalendarEvent], max_chars: int = MAX_CALENDAR_CHARS) -> str:
    if not events:
        return "===== CALENDAR =====\n(none scheduled)\n\n"
    lines = ["===== CALENDAR ====="]
    used = len(lines[0])
    dropped = 0
    for idx, ev in enumerate(events):
        when = ev.start.strftime("%a %Y-%m-%d %H:%MZ")
        end = ev.end.strftime("%H:%MZ")
        attendees = ", ".join(a for a in ev.attendees if a)[:200]
        block = [
            f"  • {when}–{end}  {ev.subject}"
            + (f"  [{ev.importance}]" if ev.importance and ev.importance != "normal" else "")
        ]
        if attendees:
            block.append(f"    Attendees: {attendees}")
        if ev.location:
            block.append(f"  Location: {ev.location}")
        if ev.web_link:
            block.append(f"    web: {ev.web_link}")
        block_text = "\n".join(block)
        if used + len(block_text) + 1 > max_chars:
            dropped = len(events) - idx
            break
        lines.append(block_text)
        used += len(block_text) + 1
    if dropped:
        lines.append(f"[+{dropped} more calendar events truncated to fit budget]")
    lines.append("")
    return "\n".join(lines) + "\n"


def _render_sent(messages: list[Message], max_chars: int = MAX_SENT_CHARS) -> str:
    if not messages:
        return "===== SENT MAIL =====\n(none)\n\n"
    lines = ["===== SENT MAIL (commitments you may have made) ====="]
    used = len(lines[0])
    dropped = 0
    for idx, m in enumerate(messages):
        body = (m.body_text or m.preview or "").strip().replace("\n", " ")
        if len(body) > 400:
            body = body[:400] + "…"
        link_line = f"web: {m.web_link}\n" if m.web_link else ""
        block = (
            f"\n--- TO {', '.join(m.to_recipients)[:120]} ---\n"
            f"Subject: {m.subject}\n"
            f"Sent: {m.received_at.isoformat()}\n"
            f"{link_line}"
            f"{body}"
        )
        if used + len(block) > max_chars:
            dropped = len(messages) - idx
            break
        lines.append(block)
        used += len(block)
    if dropped:
        lines.append(f"\n[+{dropped} more sent messages truncated to fit budget]")
    lines.append("")
    return "\n".join(lines) + "\n"


def _render_thread(t: Thread, attachment_texts: dict[str, list[tuple[str, str]]]) -> str:
    flag = " [VIP]" if t.is_vip else ""
    # Use the most recent message's webLink as the canonical thread link —
    # Outlook resolves any message in the conversation to the thread view.
    thread_link = t.messages[-1].web_link if t.messages else ""
    head = [
        f"\n--- THREAD {t.conversation_id[-16:]}{flag} ---",
        f"Subject: {t.subject}",
        f"Participants: {', '.join(t.participants[:8])}"
        + (f"  (+{len(t.participants) - 8} more)" if len(t.participants) > 8 else ""),
        f"Messages: {len(t.messages)}  "
        f"Latest: {t.latest_received.isoformat() if t.latest_received else '?'}",
    ]
    if thread_link:
        head.append(f"web: {thread_link}")
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
