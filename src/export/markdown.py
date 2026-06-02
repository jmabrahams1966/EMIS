"""Render the agenda as Markdown for OneDrive archival."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Any


def _md_link(text: str, url: str) -> str:
    return f"[{text}]({url})" if url else text


def _bucket_by_due(action_items: list[dict[str, Any]], today: date) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for a in action_items:
        raw = (a.get("due_date") or "").strip()
        try:
            due = date.fromisoformat(raw)
        except ValueError:
            buckets["undated"].append(a)
            continue
        days = (due - today).days
        if days <= 7:
            buckets["this week"].append(a)
        elif days <= 30:
            buckets["this month"].append(a)
        elif days <= 90:
            buckets["this quarter"].append(a)
        else:
            buckets["later"].append(a)
    return buckets


def _counterparty_rollup(agenda: dict[str, Any]) -> dict[str, dict[str, list]]:
    by_party: dict[str, dict[str, list]] = defaultdict(lambda: {"owed_to_you": [], "owed_by_you": []})
    for f in agenda.get("follow_ups", []):
        cp = (f.get("counterparty") or "").strip()
        if cp:
            by_party[cp]["owed_to_you"].append(f)
    for p in agenda.get("promises_made", []):
        to = (p.get("to") or "").strip()
        if to:
            by_party[to]["owed_by_you"].append(p)
    for a in agenda.get("action_items", []):
        owner = (a.get("owner") or "").strip()
        if owner and owner.lower() != "you":
            by_party[owner]["owed_to_you"].append(a)
    return {
        cp: b for cp, b in by_party.items()
        if len(b["owed_to_you"]) + len(b["owed_by_you"]) >= 2
    }


def render(agenda: dict[str, Any], week_start: datetime, week_end: datetime, mode: str) -> str:
    out: list[str] = []
    title = {
        "monday": "Weekly Agenda",
        "wednesday": "Mid-Week Check-in",
        "friday": "End-of-Week Recap",
    }.get(mode, "Agenda")
    out.append(f"# {title}")
    out.append(f"*Week of {week_start.date()} – {week_end.date()}*")
    out.append("")
    out.append(agenda.get("week_summary", ""))
    out.append("")

    out.append("## Priorities")
    for p in agenda.get("priorities", []):
        title = _md_link(f"**{p.get('title', '')}**", p.get("web_link", ""))
        bullet = f"- {title} ({p.get('urgency', 'medium')})"
        if p.get("source_subject"):
            bullet += f" — *{p['source_subject']}*"
        out.append(bullet)
        if p.get("reason"):
            out.append(f"    - {p['reason']}")
        if p.get("why_now"):
            out.append(f"    - *Why now: {p['why_now']}*")
    out.append("")

    out.append("## Meetings")
    if not agenda.get("meetings"):
        out.append("_(none)_")
    for m in agenda.get("meetings", []):
        title = _md_link(f"**{m.get('subject', '')}**", m.get("web_link", ""))
        src = f" [{m.get('source', '?')}]"
        out.append(f"- {title}{src} — {m.get('when', '')} with {m.get('participants', '')}")
        if m.get("prep_notes"):
            out.append(f"    - Prep: {m['prep_notes']}")
    out.append("")

    out.append("## Action items")
    for a in agenda.get("action_items", []):
        status = a.get("status", "new")
        urgency = a.get("urgency", "medium")
        due = a.get("due", "")
        owner = a.get("owner", "?")
        src = f" — *{a['source_subject']}*" if a.get("source_subject") else ""
        marker = {"new": "•", "carried_over": "↻", "resolved": "✓", "stale": "⚠"}.get(status, "•")
        title = _md_link(f"**{a.get('task', '')}**", a.get("web_link", ""))
        # Two-bullet style: task on top, metadata as nested sub-bullet.
        out.append(f"- {marker} {title}")
        meta = " · ".join(filter(None, [
            f"[{urgency}]", owner, f"due {due}" if due else "", status,
        ]))
        out.append(f"    - {meta}{src}")
    out.append("")

    out.append("## Follow-ups (waiting on others)")
    for f in agenda.get("follow_ups", []):
        status = f.get("status", "new")
        marker = {"new": "•", "carried_over": "↻", "resolved": "✓", "stale": "⚠"}.get(status, "•")
        weeks = f.get("weeks_open", 0)
        weeks_txt = f" — open {weeks}w" if weeks else ""
        title = _md_link(f"**{f.get('thread', '')}**", f.get("web_link", ""))
        out.append(
            f"- {marker} {title} — *{f.get('counterparty', '')}*: "
            f"{f.get('ask', '')} ({status}{weeks_txt})"
        )
    out.append("")

    if agenda.get("promises_made"):
        out.append("## Promises you made")
        for p in agenda["promises_made"]:
            title = _md_link(f"**{p.get('commitment', '')}**", p.get("web_link", ""))
            out.append(f"- {title} to {p.get('to', '')} by {p.get('by', '')}")
        out.append("")

    # Computed sections — derived from existing data.
    today = week_end.date()
    buckets = _bucket_by_due(agenda.get("action_items", []), today)
    bucket_order = ("this week", "this month", "this quarter", "later")
    coming_up_blocks = [
        f"### {label.title()}\n" + "\n".join(
            f"- {_md_link(a.get('task', ''), a.get('web_link', ''))} "
            f"({a.get('due', '') or a.get('due_date', '')}, {a.get('owner', '?')})"
            for a in buckets.get(label, [])
        )
        for label in bucket_order if buckets.get(label)
    ]
    if coming_up_blocks:
        out.append("## Coming up (by due date)")
        out.extend(coming_up_blocks)
        out.append("")

    rollup = _counterparty_rollup(agenda)
    if rollup:
        out.append("## By counterparty")
        for cp, b in sorted(rollup.items(), key=lambda kv: -(len(kv[1]["owed_to_you"]) + len(kv[1]["owed_by_you"]))):
            out.append(
                f"- **{cp}** — {len(b['owed_to_you'])} waiting on them, "
                f"{len(b['owed_by_you'])} you owe"
            )
        out.append("")

    out.append("## FYI")
    for x in agenda.get("fyi", []):
        out.append(f"- {x}")
    out.append("")
    return "\n".join(out)
