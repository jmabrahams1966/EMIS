"""Friday retrospective — landed vs slipped vs carried.

Deterministic comparison between the Monday agenda for the same week and the
Friday end-of-week state (current agenda + closures since Monday). No LLM
call — fast, free, deterministic.

Output shape::

    {
        "landed":  [{"title": "...", "kind": "priority"|"action"|"follow_up"}, ...],
        "slipped": [{"title": "...", "kind": "...", "reason": "stale"|"carried_over"}, ...],
        "carried": [{"title": "...", "kind": "..."}, ...],
    }
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _normalize(s: str) -> str:
    return (s or "").strip().lower()


def _monday_items(monday_agenda: dict) -> list[dict]:
    """Flatten Monday's tracked items into ``[{title, kind}]`` records."""
    out: list[dict] = []
    for p in monday_agenda.get("priorities", []):
        t = (p.get("title") or "").strip()
        if t:
            out.append({"title": t, "kind": "priority"})
    for a in monday_agenda.get("action_items", []):
        t = (a.get("task") or "").strip()
        if t:
            out.append({"title": t, "kind": "action"})
    for f in monday_agenda.get("follow_ups", []):
        t = (f.get("thread") or "").strip()
        if t:
            out.append({"title": t, "kind": "follow_up"})
    return out


def _closures_index(closures: dict[str, list[dict[str, str]]], since_iso: str) -> set[str]:
    """Return normalized item_match values closed since ``since_iso``."""
    closed: set[str] = set()
    for d in closures.get("done", []):
        when = d.get("completed_at", "")
        if when and when >= since_iso:
            closed.add(_normalize(d.get("item_match", "")))
    for d in closures.get("drops", []):
        when = d.get("dropped_at", "")
        if when and when >= since_iso:
            closed.add(_normalize(d.get("item_match", "")))
    return {x for x in closed if x}


def _friday_status_index(friday_agenda: dict) -> dict[str, str]:
    """Return ``{normalized title: status}`` for Friday's tracked items."""
    idx: dict[str, str] = {}
    for p in friday_agenda.get("priorities", []):
        # priorities don't carry a status field in our schema; treat presence
        # as "still active" — we can't tell slipped vs landed from this alone
        idx[_normalize(p.get("title", ""))] = "active"
    for a in friday_agenda.get("action_items", []):
        idx[_normalize(a.get("task", ""))] = a.get("status", "new")
    for f in friday_agenda.get("follow_ups", []):
        idx[_normalize(f.get("thread", ""))] = f.get("status", "new")
    idx.pop("", None)
    return idx


def build_retrospective(
    *,
    monday_agenda: dict | None,
    friday_agenda: dict,
    closures: dict[str, list[dict[str, str]]],
    monday_iso: str,
) -> dict[str, list[dict[str, str]]]:
    """Compare Monday's plan to Friday's reality.

    ``monday_iso`` is the ISO datetime of the Monday agenda's generation —
    closures completed after this timestamp count as "landed this week."

    Returns a structured retrospective. Empty lists if Monday agenda is
    missing (e.g. first week with EMIS).
    """
    if not monday_agenda:
        return {"landed": [], "slipped": [], "carried": []}

    monday = _monday_items(monday_agenda)
    closed = _closures_index(closures, monday_iso)
    friday_status = _friday_status_index(friday_agenda)

    landed: list[dict] = []
    slipped: list[dict] = []
    carried: list[dict] = []

    for item in monday:
        norm = _normalize(item["title"])
        if norm in closed:
            landed.append(item)
            continue
        status = friday_status.get(norm)
        if status in ("resolved",):
            landed.append(item)
        elif status in ("stale",):
            slipped.append({**item, "reason": "stale"})
        elif status in ("carried_over",):
            slipped.append({**item, "reason": "carried_over"})
        elif status is None:
            # Disappeared from agenda without explicit closure — could be the
            # LLM judging it irrelevant. Don't flag as slipped, treat as
            # quietly carried out.
            carried.append({**item, "reason": "dropped_silently"})
        else:
            # status == "new" means it reappeared as new; surface as carried
            carried.append({**item, "reason": status})

    return {"landed": landed, "slipped": slipped, "carried": carried}
