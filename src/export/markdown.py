"""Render the agenda as Markdown for OneDrive archival."""
from __future__ import annotations

from datetime import datetime
from typing import Any


def _md_link(text: str, url: str) -> str:
    return f"[{text}]({url})" if url else text


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
        out.append(
            f"- {marker} {title} [{owner}, due {due}, {urgency}, {status}]{src}"
        )
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

    out.append("## FYI")
    for x in agenda.get("fyi", []):
        out.append(f"- {x}")
    out.append("")
    return "\n".join(out)
