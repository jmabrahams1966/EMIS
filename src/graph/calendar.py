"""Microsoft Graph calendar — fetch events for next week, create/update the
weekly-plan event with the agenda in its body.

Endpoint reference:
  https://learn.microsoft.com/en-us/graph/api/user-list-calendarview
  https://learn.microsoft.com/en-us/graph/api/user-post-events
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
WEEKLY_PLAN_SUBJECT = "EMIS Weekly Plan"


@dataclass
class CalendarEvent:
    id: str
    subject: str
    start: datetime
    end: datetime
    is_all_day: bool
    location: str
    organizer: str
    attendees: list[str] = field(default_factory=list)
    body_preview: str = ""
    web_link: str = ""
    importance: str = "normal"


def _parse_dt(raw: str, tz: str | None = None) -> datetime:
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def list_events_in_range(
    *,
    access_token: str,
    start: datetime,
    end: datetime,
    max_events: int = 200,
) -> list[CalendarEvent]:
    """List calendar events between ``start`` and ``end`` (UTC bounds)."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Prefer": 'outlook.timezone="UTC"',
    }
    params = {
        "startDateTime": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "$orderby": "start/dateTime",
        "$select": (
            "id,subject,start,end,isAllDay,location,organizer,attendees,"
            "bodyPreview,webLink,importance"
        ),
        "$top": "100",
    }
    url = f"{GRAPH_BASE}/me/calendarView"

    out: list[CalendarEvent] = []
    async with httpx.AsyncClient(timeout=60) as client:
        while url and len(out) < max_events:
            resp = await client.get(
                url, headers=headers,
                params=params if "startDateTime" in (params or {}) else None,
            )
            resp.raise_for_status()
            payload = resp.json()
            for item in payload.get("value", []):
                out.append(_to_event(item))
                if len(out) >= max_events:
                    break
            url = payload.get("@odata.nextLink")
            params = None
    logger.info("fetched %d calendar events", len(out))
    return out


def _to_event(item: dict) -> CalendarEvent:
    organizer = (item.get("organizer") or {}).get("emailAddress", {}) or {}
    location = (item.get("location") or {}).get("displayName", "")
    return CalendarEvent(
        id=item["id"],
        subject=item.get("subject") or "(no subject)",
        start=_parse_dt(item["start"]["dateTime"]),
        end=_parse_dt(item["end"]["dateTime"]),
        is_all_day=bool(item.get("isAllDay", False)),
        location=location,
        organizer=organizer.get("address", ""),
        attendees=[
            (a.get("emailAddress") or {}).get("address", "")
            for a in item.get("attendees", [])
        ],
        body_preview=item.get("bodyPreview", ""),
        web_link=item.get("webLink", ""),
        importance=item.get("importance", "normal"),
    )


# ── Weekly plan event ──────────────────────────────────────────────────────

async def find_weekly_plan_event(
    *,
    access_token: str,
    week_of: datetime,
) -> dict | None:
    """Find an existing weekly-plan event for this week, if any."""
    start = week_of.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    events = await list_events_in_range(
        access_token=access_token, start=start, end=end, max_events=100,
    )
    for ev in events:
        if ev.subject == WEEKLY_PLAN_SUBJECT:
            return {"id": ev.id, "start": ev.start, "end": ev.end}
    return None


async def upsert_weekly_plan_event(
    *,
    access_token: str,
    week_of: datetime,
    html_body: str,
) -> dict[str, Any]:
    """Create or update the weekly-plan event for the given week.

    The event sits Monday 08:00-08:30 UTC by default. If one already exists for
    this week (matched by subject), we PATCH its body instead of creating a
    duplicate.
    """
    monday = week_of - timedelta(days=week_of.weekday())
    start_dt = monday.replace(hour=8, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end_dt = start_dt + timedelta(minutes=30)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    body = {
        "subject": WEEKLY_PLAN_SUBJECT,
        "body": {"contentType": "HTML", "content": html_body},
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "UTC"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "UTC"},
        "showAs": "free",
        "isReminderOn": False,
        "categories": ["EMIS"],
    }

    existing = await find_weekly_plan_event(access_token=access_token, week_of=monday)
    async with httpx.AsyncClient(timeout=30) as client:
        if existing:
            resp = await client.patch(
                f"{GRAPH_BASE}/me/events/{existing['id']}",
                headers=headers, json=body,
            )
            action = "updated"
        else:
            resp = await client.post(
                f"{GRAPH_BASE}/me/events", headers=headers, json=body,
            )
            action = "created"
        resp.raise_for_status()
        result = resp.json()

    logger.info("weekly-plan event %s: %s", action, result.get("id"))
    return {"action": action, "event_id": result.get("id"), "web_link": result.get("webLink")}
