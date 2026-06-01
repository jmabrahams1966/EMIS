"""Stable prompt text + JSON schema for the agenda.

Caching strategy: the system prompt is frozen — no timestamps, run IDs, or
per-week data — so the prefix is byte-identical across runs. Volatile data
(week range, today's date, mail, calendar, prior agendas) goes in the user
turn after the cache breakpoint.

Three modes share the same schema:
  - monday    — Monday morning: full agenda for the upcoming week
  - wednesday — mid-week check-in: what's slipping, what still needs attention
  - friday    — end-of-week recap: what closed, what's still open

The system prompt is a single block so it caches as one prefix. Mode-specific
guidance lives in MODE_NOTES and is injected at the top of the user turn,
which is fine — the user turn is volatile by design.
"""
from __future__ import annotations

SYSTEM_PROMPT = """\
You are a meticulous chief-of-staff who produces concise, structured agendas \
from the user's inbox, calendar, and recent sent mail. You write for an \
executive who skims at three points in the week: Monday morning to plan, \
Wednesday morning to course-correct, and Friday afternoon to close out.

What you receive each run:
- The week's incoming messages, already grouped into conversation threads.
- Recent sent mail from the user, so you can see what they have committed to \
  others.
- Calendar events for the upcoming or current week.
- A compressed view of the prior 1-4 weeks of agendas, so you can track items \
  across time.
- Filters: VIP senders are flagged. Blocklisted senders have already been \
  removed.

Operating principles:
- Prefer specifics over generalities. Quote dates, names, dollar amounts, and \
  short verbatim phrases when they capture intent.
- Distinguish things the user must do (action items) from things others owe \
  the user (follow-ups). Don't put the same item in both.
- A calendar event is a meeting. An email proposing a meeting time but with \
  no calendar entry is a follow-up until accepted. Don't fabricate meetings.
- Action items must have a clear owner. If the message doesn't name one, \
  infer from context; only write "you" when the ask is unambiguously the \
  user's.
- `priorities` is the top of the agenda — three to five items the user should \
  do or decide this week, ranked by consequence. Each priority states the \
  reason it matters, not just the task.
- `fyi` is for awareness-only context. Max six bullets, one sentence each.
- Deadlines: extract an ISO date (YYYY-MM-DD) when the source clearly states \
  one. Set urgency `high` for anything due within 3 days or flagged urgent by \
  the sender; `medium` for the rest of this week; `low` otherwise.
- Cross-week tracking: when prior agendas are provided, mark each action item \
  and follow-up with a `status`: `new`, `carried_over` (open from a prior \
  week), `resolved` (recently closed — surface in week_summary, not in the \
  action list), or `stale` (open >= 3 weeks, may need to be dropped). The \
  user is reading the status field to decide what to drop.
- VIP threads: when a thread is marked VIP, prefer to surface it even if the \
  content might otherwise read as routine. VIP is the user's explicit signal.
- Honor the mode-specific guidance at the top of the user turn — it shifts \
  what the agenda emphasizes, but the schema is identical across modes.

Quality bar:
- Every section may be empty if the inputs genuinely have nothing there. \
  Don't pad.
- Reference the source message subject in parentheses when an item isn't \
  self-explanatory.
- Use plain text in string fields. No markdown.

Linking back to the source:
- Every item that ties to a single source — priorities, meetings, action items, \
  follow-ups, promises — has a `web_link` field. Populate it with the URL \
  shown next to the source in this turn (the `web:` line on a thread, \
  calendar event, or sent message). Copy the URL verbatim. If the item \
  draws from multiple sources or no single source is identifiable, leave \
  `web_link` as an empty string. Do not invent URLs.
"""


MODE_NOTES = {
    "monday": (
        "MODE: Monday morning. Produce the full agenda for the week ahead. "
        "The user is planning; emphasize priorities and meetings. Use this "
        "week's calendar events as the spine."
    ),
    "wednesday": (
        "MODE: Mid-week check-in. The user is course-correcting. Emphasize "
        "items at risk of slipping, action items not yet started, follow-ups "
        "where the counterparty has gone quiet, and meetings later this week "
        "needing prep. Drop FYI unless something genuinely changed since "
        "Monday. The week_summary should answer: 'what should I redirect "
        "attention to by Friday?'"
    ),
    "friday": (
        "MODE: End-of-week recap. The user is closing out the week and "
        "previewing next week's open loops. Set status correctly: items "
        "resolved this week go into week_summary, items still open carry "
        "their original status. Priorities should describe what needs to "
        "land before Friday EOD; meetings should be empty unless a Friday "
        "meeting is still ahead. The week_summary should answer: 'did this "
        "week go the way I planned, and what carries forward?'"
    ),
}


AGENDA_SCHEMA = {
    "type": "object",
    "properties": {
        "week_summary": {
            "type": "string",
            "description": (
                "Two to four sentences. Shape of the week, what dominates, "
                "what closed (Friday mode), what's slipping (Wednesday mode)."
            ),
        },
        "priorities": {
            "type": "array",
            "description": "3-5 most consequential items, ranked.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "source_subject": {"type": "string"},
                    "urgency": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "web_link": {
                        "type": "string",
                        "description": "URL of the source thread/event, '' if unknown.",
                    },
                },
                "required": ["title", "reason", "source_subject", "urgency", "web_link"],
                "additionalProperties": False,
            },
        },
        "meetings": {
            "type": "array",
            "description": (
                "Scheduled or proposed meetings. Calendar events go here verbatim; "
                "email-proposed meetings only if a specific time is named."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "when": {"type": "string"},
                    "participants": {"type": "string"},
                    "prep_notes": {"type": "string"},
                    "source": {
                        "type": "string",
                        "enum": ["calendar", "email"],
                    },
                    "web_link": {
                        "type": "string",
                        "description": "URL of the calendar event or source email, '' if unknown.",
                    },
                },
                "required": ["subject", "when", "participants", "prep_notes", "source", "web_link"],
                "additionalProperties": False,
            },
        },
        "action_items": {
            "type": "array",
            "description": "Concrete tasks the user (or a named owner) must complete.",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "owner": {"type": "string"},
                    "due": {
                        "type": "string",
                        "description": "Due date as written, or 'this week', or 'unspecified'.",
                    },
                    "due_date": {
                        "type": "string",
                        "description": "Best-effort ISO date YYYY-MM-DD, '' if unknown.",
                    },
                    "urgency": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "source_subject": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["new", "carried_over", "resolved", "stale"],
                    },
                    "web_link": {
                        "type": "string",
                        "description": "URL of the source thread, '' if unknown.",
                    },
                },
                "required": [
                    "task", "owner", "due", "due_date", "urgency",
                    "source_subject", "status", "web_link",
                ],
                "additionalProperties": False,
            },
        },
        "follow_ups": {
            "type": "array",
            "description": "Things the user is waiting on from someone else.",
            "items": {
                "type": "object",
                "properties": {
                    "thread": {"type": "string"},
                    "counterparty": {"type": "string"},
                    "ask": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["new", "carried_over", "resolved", "stale"],
                    },
                    "weeks_open": {
                        "type": "integer",
                        "description": "Best-effort count of weeks this has been open.",
                    },
                    "web_link": {
                        "type": "string",
                        "description": "URL of the source thread, '' if unknown.",
                    },
                },
                "required": ["thread", "counterparty", "ask", "status", "weeks_open", "web_link"],
                "additionalProperties": False,
            },
        },
        "promises_made": {
            "type": "array",
            "description": (
                "Things the user committed to in *sent* mail this week. "
                "Empty list if no sent mail provided or nothing committed."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "commitment": {"type": "string"},
                    "to": {"type": "string"},
                    "by": {"type": "string"},
                    "source_subject": {"type": "string"},
                    "web_link": {
                        "type": "string",
                        "description": "URL of the sent message, '' if unknown.",
                    },
                },
                "required": ["commitment", "to", "by", "source_subject", "web_link"],
                "additionalProperties": False,
            },
        },
        "fyi": {
            "type": "array",
            "description": "Awareness-only items, one sentence each. Max 6.",
            "items": {"type": "string"},
        },
    },
    "required": [
        "week_summary", "priorities", "meetings", "action_items",
        "follow_ups", "promises_made", "fyi",
    ],
    "additionalProperties": False,
}
