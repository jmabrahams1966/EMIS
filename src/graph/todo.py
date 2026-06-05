"""Microsoft To Do — create tasks for action items.

Lists are addressed by displayName. If the configured list doesn't exist we
create it. Tasks are deduped by title within the current run (the agent may
emit the same title twice across modes).

Endpoint reference: https://learn.microsoft.com/en-us/graph/api/todo-list-lists
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


async def list_completed_tasks(
    access_token: str, list_id: str, since: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return tasks in the list with status=completed (optionally since a date).

    Used by the weekly agenda flow to detect items the user marked complete
    in the Microsoft To Do app or Outlook Tasks, so we can record them as
    `done` closures and stop resurfacing them.

    Each returned dict has at least: ``id``, ``title``, ``completedDateTime``
    (a dict with ``dateTime`` and ``timeZone`` keys per Graph schema).
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    filt = "status eq 'completed'"
    if since is not None:
        iso = since.astimezone().strftime("%Y-%m-%dT%H:%M:%S.000000")
        filt += f" and completedDateTime/dateTime ge '{iso}'"
    params = {
        "$filter": filt,
        "$top": "200",
        "$select": "id,title,status,completedDateTime",
    }
    out: list[dict[str, Any]] = []
    url = f"{GRAPH_BASE}/me/todo/lists/{list_id}/tasks"
    async with httpx.AsyncClient(timeout=30) as client:
        while url:
            resp = await client.get(
                url, headers=headers,
                params=params if "$filter" in (params or {}) else None,
            )
            resp.raise_for_status()
            payload = resp.json()
            out.extend(payload.get("value", []))
            url = payload.get("@odata.nextLink")
            params = None
    return out


async def ensure_list(access_token: str, list_name: str) -> str:
    """Return the To Do list ID for ``list_name``, creating it if missing."""
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{GRAPH_BASE}/me/todo/lists", headers=headers)
        resp.raise_for_status()
        for item in resp.json().get("value", []):
            if item.get("displayName") == list_name:
                return item["id"]

        # Create.
        create = await client.post(
            f"{GRAPH_BASE}/me/todo/lists",
            headers={**headers, "Content-Type": "application/json"},
            json={"displayName": list_name},
        )
        create.raise_for_status()
        new_id = create.json()["id"]
        logger.info("created To Do list %r (%s)", list_name, new_id)
        return new_id


async def list_existing_titles(access_token: str, list_id: str) -> set[str]:
    """Return the set of open task titles in the list (case-folded).

    Used to dedupe so we don't keep recreating the same task week after week.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    # Graph To Do rejects `status ne 'completed'` with 400, so fetch all and
    # filter completed ones out client-side.
    params = {"$top": "200", "$select": "title,status"}
    titles: set[str] = set()
    url = f"{GRAPH_BASE}/me/todo/lists/{list_id}/tasks"
    async with httpx.AsyncClient(timeout=30) as client:
        while url:
            resp = await client.get(
                url, headers=headers,
                params=params if "$top" in (params or {}) else None,
            )
            resp.raise_for_status()
            payload = resp.json()
            for t in payload.get("value", []):
                if t.get("status") == "completed":
                    continue
                if t.get("title"):
                    titles.add(t["title"].strip().lower())
            url = payload.get("@odata.nextLink")
            params = None
    return titles


async def create_task(
    *,
    access_token: str,
    list_id: str,
    title: str,
    body: str = "",
    due_iso: str | None = None,
    importance: str = "normal",
) -> dict[str, Any]:
    """Create a single task. ``due_iso`` is a YYYY-MM-DD string if known."""
    payload: dict[str, Any] = {
        "title": title,
        "importance": importance,
    }
    if body:
        payload["body"] = {"content": body, "contentType": "text"}
    if due_iso:
        payload["dueDateTime"] = {"dateTime": f"{due_iso}T00:00:00", "timeZone": "UTC"}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/me/todo/lists/{list_id}/tasks",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


async def sync_action_items(
    *,
    access_token: str,
    list_name: str,
    action_items: list[dict],
) -> dict[str, Any]:
    """Create one task per action item, skipping any that already exist.

    Returns counts for logging.
    """
    list_id = await ensure_list(access_token, list_name)
    existing = await list_existing_titles(access_token, list_id)

    created = 0
    skipped = 0
    errors: list[str] = []
    for item in action_items:
        title = (item.get("task") or "").strip()
        if not title:
            continue
        if title.lower() in existing:
            skipped += 1
            continue
        owner = item.get("owner", "")
        source = item.get("source_subject", "")
        due = item.get("due_date")  # YYYY-MM-DD if extractor populated it
        importance = "high" if item.get("urgency") == "high" else "normal"
        body_parts = []
        if owner and owner.lower() != "you":
            body_parts.append(f"Owner: {owner}")
        if source:
            body_parts.append(f"Source: {source}")
        body = "\n".join(body_parts)
        try:
            await create_task(
                access_token=access_token, list_id=list_id, title=title,
                body=body, due_iso=due, importance=importance,
            )
            created += 1
            existing.add(title.lower())
        except Exception as exc:
            errors.append(f"{title}: {exc}")

    return {
        "list_id": list_id, "created": created, "skipped": skipped,
        "errors": errors,
    }
