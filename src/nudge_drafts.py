"""Auto-draft polite nudge replies for stale follow-ups.

When the Monday agenda has follow-ups with ``weeks_open >= 2``, generate a
polite nudge body for each via one Claude call and create drafts in the
user's Drafts folder. The user opens Outlook later and sends with one
click — no editing required for the easy cases.

Drafts are created via Graph's ``createReply`` endpoint so the threading
headers (In-Reply-To, References) and quoted history come along for free.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from .agenda.builder import _make_client

logger = logging.getLogger("emis.nudge_drafts")


NUDGE_SYSTEM_PROMPT = """\
You write polite, professional nudge emails on the user's behalf. For each \
stale follow-up provided, write a short reply body (2-4 sentences) that:

- Doesn't apologize for following up. Past 2-3 weeks of silence is normal; \
  treat the nudge as routine.
- Restates what the user is waiting on in one short clause ("checking in \
  on the X numbers", "circling back on the Y decision").
- Asks a specific, scoped question that's easy to answer ("is there an \
  ETA?", "is this still on your radar?", "want to push to next month?"). \
  Don't ask open-ended questions.
- Closes with a brief one-line sign-off. No "looking forward to your \
  response" filler.
- Matches the user's natural register: professional but warm; first name \
  basis if the counterparty's a colleague or vendor; formal if it's an \
  external counterpart you can't tell. Default to professional-but-warm.

Output plain text only — no HTML, no markdown. The body will be inserted \
into a reply that already includes the quoted thread.
"""


NUDGE_DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "drafts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "thread": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["thread", "body"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["drafts"],
    "additionalProperties": False,
}


def generate_nudge_bodies(
    *,
    follow_ups: list[dict[str, Any]],
    api_key: str,
    model: str,
    aws_region: str,
) -> dict[str, str]:
    """Generate a nudge body per follow-up. Returns ``{thread: body}`` dict."""
    if not follow_ups:
        return {}

    user_lines = ["Stale follow-ups requiring a nudge:\n"]
    for f in follow_ups:
        weeks = f.get("weeks_open", "?")
        user_lines.append(
            f"- thread: {f.get('thread', '')!r}\n"
            f"  counterparty: {f.get('counterparty', '')}\n"
            f"  what you're waiting on: {f.get('ask', '')}\n"
            f"  open for: {weeks} week(s)"
        )

    client = _make_client(api_key=api_key, model=model, aws_region=aws_region)
    is_bedrock = isinstance(client, anthropic.AnthropicBedrock)

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": 8_000,
        "system": NUDGE_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": "\n".join(user_lines)}],
    }
    if is_bedrock:
        kwargs["tools"] = [{
            "name": "emit_drafts",
            "description": "Emit nudge reply bodies, one per stale follow-up.",
            "input_schema": NUDGE_DRAFT_SCHEMA,
        }]
        kwargs["tool_choice"] = {"type": "tool", "name": "emit_drafts"}
    else:
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {
            "format": {"type": "json_schema", "schema": NUDGE_DRAFT_SCHEMA},
            "effort": "low",
        }

    with client.messages.stream(**kwargs) as stream:
        message = stream.get_final_message()

    if is_bedrock:
        block = next(
            (b for b in message.content if b.type == "tool_use" and b.name == "emit_drafts"),
            None,
        )
        if block is None:
            return {}
        parsed = block.input
    else:
        text_block = next((b for b in message.content if b.type == "text"), None)
        if text_block is None:
            return {}
        parsed = json.loads(text_block.text)

    return {d["thread"]: d["body"] for d in parsed.get("drafts", [])}
