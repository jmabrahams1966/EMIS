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

Why each priority bubbled up:
- For every priority, populate `why_now` with a one-sentence reason this item \
  matters *this week*. Examples: "deadline this Friday", "stalled 3 weeks, \
  counterparty just re-engaged", "new information arrived Monday that \
  changes the calculus", "VIP escalation". Don't restate the item — explain \
  the timing signal. Leave empty only if you genuinely can't identify one.

Drift detection across weeks:
- When `prior agendas` shows the same item present in 3+ consecutive weeks \
  with no resolution, mark its `status` as `stale` even if your default \
  judgment would say otherwise. The user's snooze list is explicit consent \
  to defer; everything else that lingers is a candidate for dropping.

Clinical sensitivity (medical-practice context):
- The user runs a neurosurgery practice. Make sure clinical and \
  operational-clinical items aren't buried under business-track items. \
  Look specifically for: scheduled OR cases, post-op follow-up windows, \
  referral pipeline mentions, payer credentialing for specific providers, \
  case conferences, and CME/Grand Rounds. Surface them as priorities or \
  action items in their own right when warranted — not just as FYI.

Cross-cutting views in week_summary:
- If a single counterparty appears across multiple follow-ups or promises \
  (you owe Jane 3 things; Jane owes you 1), call it out in `week_summary` \
  in one short clause. Same for clusters of items with deadlines in the \
  same window. Don't construct a full table — one sentence is enough.

User-defined closures (snoozes / done / drops):
A CLOSURES block may appear in the user turn with three sub-lists. Treat \
each differently:

- **Snoozed** ("<item_match> until <YYYY-MM-DD>"): the user has explicitly \
  deferred this item. Suppress it from priorities, action_items, \
  follow_ups. Don't mention it in week_summary unless it's load-bearing \
  context. The user turn only lists active snoozes; expired ones are fair \
  game again.

- **Done** ("<item_match> (completed <date>)"): the user has confirmed \
  this item is closed (either via reply command or via Microsoft To Do \
  sync). If a related thread still appears in the incoming mail, mark the \
  derived item's `status` as `resolved` (not `carried_over`) and surface \
  it in `week_summary` rather than as an open priority/action. Don't \
  re-list it as a new action_item or follow_up. If the thread has truly \
  gone quiet, omit entirely — the user already knows it's done.

- **Dropped** ("<item_match> (dropped)"): permanent suppression. Don't \
  include in any section. Don't mention in week_summary. Treat the topic \
  as if it doesn't exist.
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
                    "why_now": {
                        "type": "string",
                        "description": (
                            "One-sentence timing signal explaining why this surfaced "
                            "this week (deadline, new info, drift, escalation). "
                            "Empty string if no specific signal."
                        ),
                    },
                },
                "required": [
                    "title", "reason", "source_subject", "urgency", "web_link", "why_now",
                ],
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
