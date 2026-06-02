"""Self-contained interactive HTML dashboard for the agenda.

Renders a single HTML document with:
  - Tab strip at the top (Week at a glance / Priorities / Meetings / Action
    items / Follow-ups / Promises / FYI / Coming up / By counterparty)
  - Vanilla JS to switch tabs (no external dependencies)
  - "+ Add to calendar" links on every dated item, implemented as ``data:``
    URLs that download a single-event ICS file — no backend required

Used in two places:
  - ``handler.py`` writes ``dashboard.{mode}.html`` to ``PREVIEW_DIR`` during
    dry-runs so the user can open it in a browser locally
  - ``web_ui.py`` serves it as the agenda detail page so the deployed Web UI
    is interactive instead of static
"""
from __future__ import annotations

import hashlib
import urllib.parse
from collections import defaultdict
from datetime import date, datetime, timedelta
from html import escape
from typing import Any

_STATUS_MARK = {"new": "•", "carried_over": "↻", "resolved": "✓", "stale": "⚠"}
_URGENCY_COLOR = {"high": "#c0392b", "medium": "#d68910", "low": "#7f8c8d"}
_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _linked(text_html: str, url: str) -> str:
    if not url:
        return text_html
    return (
        f"<a href='{escape(url)}' "
        f"style='color:inherit;text-decoration:underline;text-decoration-color:#bbb' "
        f"target='_blank'>{text_html}</a>"
    )


def _ics_url(*, summary: str, due_date_iso: str, description: str) -> str:
    """Return a ``data:text/calendar`` URL that downloads a one-event ICS.

    Clicking it opens the user's default calendar app (Outlook on macOS,
    Calendar on iOS, etc.) with the event ready to add. No backend hit.
    """
    if not due_date_iso:
        return ""
    try:
        d = date.fromisoformat(due_date_iso)
    except ValueError:
        return ""
    # All-day-ish: schedule the event 9:00–9:30 on the due date.
    dt_compact = d.strftime("%Y%m%d")
    uid = hashlib.md5(f"{summary}-{due_date_iso}".encode()).hexdigest()
    # Escape ICS-special chars: comma, semicolon, newline.
    def _esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
    ics = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//EMIS//Agenda//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}@emis",
        f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART:{dt_compact}T090000",
        f"DTEND:{dt_compact}T093000",
        f"SUMMARY:{_esc(summary)}",
        f"DESCRIPTION:{_esc(description)}",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ])
    return f"data:text/calendar;charset=utf-8,{urllib.parse.quote(ics)}"


def _add_to_cal_button(item: dict, source_label: str = "") -> str:
    """Tiny '+ Cal' link for an action item with a due_date."""
    due_iso = (item.get("due_date") or "").strip()
    if not due_iso:
        return ""
    summary = item.get("task") or item.get("title") or item.get("commitment") or ""
    desc_bits = []
    if item.get("source_subject"):
        desc_bits.append(f"Source: {item['source_subject']}")
    elif source_label:
        desc_bits.append(source_label)
    if item.get("web_link"):
        desc_bits.append(item["web_link"])
    url = _ics_url(
        summary=summary,
        due_date_iso=due_iso,
        description=" — ".join(desc_bits),
    )
    if not url:
        return ""
    return (
        f"<a href='{url}' download='{escape(summary[:40] or 'event')}.ics' "
        f"style='font-size:11px;color:#2c6cdf;text-decoration:none;margin-left:8px;"
        f"padding:1px 6px;border:1px solid #cfe0ff;border-radius:3px'>"
        f"+ Cal</a>"
    )


# ── Panel renderers ────────────────────────────────────────────────────────

def _urgency_badge(urgency: str) -> str:
    c = _URGENCY_COLOR.get(urgency, "#7f8c8d")
    return (
        f"<span style='color:{c};font-size:11px;text-transform:uppercase;"
        f"letter-spacing:0.5px'>{urgency}</span>"
    )


def _status_badge(status: str) -> str:
    mark = _STATUS_MARK.get(status, "•")
    return f"<span title='{escape(status)}' style='font-weight:bold;margin-right:4px'>{mark}</span>"


def _render_summary_panel(agenda: dict, week_start: datetime, week_end: datetime) -> str:
    summary = escape(agenda.get("week_summary", "") or "(no summary)")
    return (
        f"<p style='font-size:15px;line-height:1.6'>{summary}</p>"
    )


def _render_week_at_a_glance(agenda: dict, week_start: datetime) -> str:
    """Group meetings + action_items by weekday into 7 buckets."""
    by_day: dict[int, dict[str, list]] = defaultdict(lambda: {"meetings": [], "actions": []})
    # Meetings: parse a date hint from `when` if present, but the schema
    # doesn't carry an ISO date, so we fall back to source_subject heuristics.
    # Practical approach: render meetings under the day that appears in `when`.
    for m in agenda.get("meetings", []):
        when = m.get("when", "")
        idx = _guess_weekday_from_text(when, week_start)
        if idx is None:
            idx = 0  # default to Monday if we can't tell
        by_day[idx]["meetings"].append(m)
    # Action items: bucket by due_date ISO if present.
    for a in agenda.get("action_items", []):
        raw = (a.get("due_date") or "").strip()
        try:
            d = date.fromisoformat(raw)
        except ValueError:
            continue
        delta = (d - week_start.date()).days
        if 0 <= delta < 7:
            by_day[delta]["actions"].append(a)

    cards = []
    for i in range(7):
        day_date = (week_start.date() + timedelta(days=i))
        slot = by_day.get(i, {"meetings": [], "actions": []})
        if not slot["meetings"] and not slot["actions"]:
            inner = "<p style='color:#aaa;margin:0'>(nothing scheduled)</p>"
        else:
            rows = []
            for m in slot["meetings"]:
                title = _linked(f"<strong>{escape(m['subject'])}</strong>", m.get("web_link", ""))
                rows.append(
                    f"<li>📅 {title} <span style='color:#666;font-size:12px'>"
                    f"— {escape(m.get('when', ''))}</span></li>"
                )
            for a in slot["actions"]:
                title = _linked(f"<strong>{escape(a['task'])}</strong>", a.get("web_link", ""))
                rows.append(
                    f"<li>✓ {title} {_add_to_cal_button(a)}"
                    f"<ul style='margin:2px 0 0 0;padding-left:18px;color:#666;font-size:12px'>"
                    f"<li>{_urgency_badge(a.get('urgency', 'medium'))} · "
                    f"{escape(a.get('owner', '?'))} · {escape(a.get('status', 'new'))}</li>"
                    f"</ul></li>"
                )
            inner = f"<ul style='margin:0;padding-left:18px;line-height:1.6'>{''.join(rows)}</ul>"
        cards.append(
            f"<div style='border:1px solid #e6e6e6;border-radius:6px;padding:12px 14px;margin-bottom:10px'>"
            f"<div style='font-weight:600;font-size:13px;color:#444;margin-bottom:6px'>"
            f"{_DAY_NAMES[i]} {day_date.strftime('%b %d')}</div>"
            f"{inner}</div>"
        )
    return "".join(cards)


def _guess_weekday_from_text(text: str, week_start: datetime) -> int | None:
    """Best-effort: find a weekday name or ISO date in ``text`` and return 0-6."""
    if not text:
        return None
    low = text.lower()
    names = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    for k, idx in names.items():
        if k in low:
            return idx
    # ISO date?
    for token in text.split():
        try:
            d = date.fromisoformat(token.strip(",;()"))
            delta = (d - week_start.date()).days
            if 0 <= delta < 7:
                return delta
        except ValueError:
            continue
    return None


def _render_priorities_panel(agenda: dict) -> str:
    items = agenda.get("priorities", [])
    if not items:
        return "<p style='color:#aaa'>No priorities this week.</p>"
    rows = []
    for p in items:
        title = _linked(f"<strong>{escape(p['title'])}</strong>", p.get("web_link", ""))
        why = (
            f"<div style='color:#888;font-size:12px;font-style:italic;margin-top:2px'>"
            f"Why now: {escape(p['why_now'])}</div>"
            if p.get("why_now") else ""
        )
        src = (
            f"<em style='color:#888;font-size:12px'>({escape(p['source_subject'])})</em>"
            if p.get("source_subject") else ""
        )
        rows.append(
            f"<li style='margin-bottom:10px'>"
            f"{_urgency_badge(p.get('urgency', 'medium'))} {title} — "
            f"{escape(p.get('reason', ''))} {src}{why}</li>"
        )
    return f"<ul style='line-height:1.6'>{''.join(rows)}</ul>"


def _render_meetings_panel(agenda: dict) -> str:
    items = agenda.get("meetings", [])
    if not items:
        return "<p style='color:#aaa'>No meetings.</p>"
    rows = []
    for m in items:
        title = _linked(f"<strong>{escape(m['subject'])}</strong>", m.get("web_link", ""))
        prep = (
            f"<div style='color:#555;font-size:12px;margin-top:2px'>"
            f"Prep: {escape(m['prep_notes'])}</div>"
            if m.get("prep_notes") else ""
        )
        rows.append(
            f"<li style='margin-bottom:10px'>{title} "
            f"<span style='color:#888;font-size:11px'>[{escape(m.get('source', '?'))}]</span><br>"
            f"<span style='color:#666;font-size:12px'>{escape(m['when'])} · "
            f"{escape(m['participants'])}</span>{prep}</li>"
        )
    return f"<ul style='line-height:1.6'>{''.join(rows)}</ul>"


def _render_actions_panel(agenda: dict) -> str:
    items = agenda.get("action_items", [])
    if not items:
        return "<p style='color:#aaa'>No action items.</p>"
    rows = []
    for a in items:
        title = _linked(f"<strong>{escape(a['task'])}</strong>", a.get("web_link", ""))
        meta_bits = [
            _urgency_badge(a.get("urgency", "medium")),
            escape(a.get("owner", "?")),
            f"due {escape(a.get('due', ''))}" if a.get("due") else "",
            escape(a.get("status", "new")),
        ]
        meta = " · ".join(b for b in meta_bits if b)
        src = (
            f" <em style='color:#888'>({escape(a['source_subject'])})</em>"
            if a.get("source_subject") else ""
        )
        rows.append(
            f"<li style='margin-bottom:8px'>{_status_badge(a.get('status', 'new'))}{title}"
            f"{_add_to_cal_button(a)}"
            f"<ul style='margin:2px 0 0 0;padding-left:18px;color:#666;font-size:12px'>"
            f"<li>{meta}{src}</li></ul></li>"
        )
    return f"<ul style='line-height:1.6'>{''.join(rows)}</ul>"


def _render_followups_panel(agenda: dict) -> str:
    items = agenda.get("follow_ups", [])
    if not items:
        return "<p style='color:#aaa'>Nothing pending from others.</p>"
    rows = []
    for f in items:
        title = _linked(f"<strong>{escape(f['thread'])}</strong>", f.get("web_link", ""))
        weeks = (
            f" <span style='color:#888'>(open {f['weeks_open']}w)</span>"
            if f.get("weeks_open") else ""
        )
        rows.append(
            f"<li style='margin-bottom:8px'>{_status_badge(f.get('status', 'new'))}{title} — "
            f"waiting on {escape(f['counterparty'])}: {escape(f['ask'])}{weeks}</li>"
        )
    return f"<ul style='line-height:1.6'>{''.join(rows)}</ul>"


def _render_promises_panel(agenda: dict) -> str:
    items = agenda.get("promises_made", [])
    if not items:
        return "<p style='color:#aaa'>No commitments this week.</p>"
    rows = []
    for p in items:
        title = _linked(f"<strong>{escape(p['commitment'])}</strong>", p.get("web_link", ""))
        rows.append(
            f"<li style='margin-bottom:8px'>{title} {_add_to_cal_button(p)}"
            f"<ul style='margin:2px 0 0 0;padding-left:18px;color:#666;font-size:12px'>"
            f"<li>to {escape(p['to'])} by {escape(p['by'])}</li></ul></li>"
        )
    return f"<ul style='line-height:1.6'>{''.join(rows)}</ul>"


def _render_fyi_panel(agenda: dict) -> str:
    items = agenda.get("fyi", [])
    if not items:
        return "<p style='color:#aaa'>Nothing to flag.</p>"
    rows = "".join(f"<li style='margin-bottom:4px'>{escape(x)}</li>" for x in items)
    return f"<ul style='line-height:1.6'>{rows}</ul>"


# ── Top-level render ───────────────────────────────────────────────────────

_TABS = [
    ("summary", "Week at a glance"),
    ("priorities", "Priorities"),
    ("meetings", "Meetings"),
    ("actions", "Action items"),
    ("followups", "Follow-ups"),
    ("promises", "Promises"),
    ("fyi", "FYI"),
]


def render_dashboard_html(
    agenda: dict[str, Any],
    week_start: datetime,
    week_end: datetime,
    mode: str = "monday",
) -> str:
    """Render an interactive single-page dashboard for the agenda."""
    title = {
        "monday": "Weekly Dashboard",
        "wednesday": "Mid-Week Dashboard",
        "friday": "End-of-Week Dashboard",
    }.get(mode, "Agenda Dashboard")

    panels = {
        "summary": _render_summary_panel(agenda, week_start, week_end)
                   + _render_week_at_a_glance(agenda, week_start),
        "priorities": _render_priorities_panel(agenda),
        "meetings": _render_meetings_panel(agenda),
        "actions": _render_actions_panel(agenda),
        "followups": _render_followups_panel(agenda),
        "promises": _render_promises_panel(agenda),
        "fyi": _render_fyi_panel(agenda),
    }

    tab_buttons = "".join(
        f"<button class='emis-tab' data-target='{tid}'>{escape(label)}</button>"
        for tid, label in _TABS
    )
    panel_divs = "".join(
        f"<div class='emis-panel' id='emis-panel-{tid}'>{html}</div>"
        for tid, html in panels.items()
    )

    return f"""\
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} — EMIS</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    color: #222; max-width: 900px; margin: 0 auto; padding: 24px;
    background: #fafafa;
  }}
  h1 {{ margin: 0 0 4px 0; }}
  .emis-meta {{ color: #666; font-size: 13px; margin-bottom: 18px; }}
  .emis-tabs {{
    display: flex; flex-wrap: wrap; gap: 2px; border-bottom: 2px solid #ddd;
    margin-bottom: 18px; position: sticky; top: 0; background: #fafafa;
    padding-top: 8px; z-index: 10;
  }}
  .emis-tab {{
    background: transparent; border: 0; padding: 10px 14px; cursor: pointer;
    font-size: 13px; color: #555; font-family: inherit;
    border-bottom: 2px solid transparent; margin-bottom: -2px;
  }}
  .emis-tab:hover {{ color: #222; background: #f0f0f0; }}
  .emis-tab.active {{
    color: #2c6cdf; border-bottom-color: #2c6cdf; font-weight: 600;
  }}
  .emis-panel {{ display: none; padding: 4px 0 24px 0; }}
  .emis-panel.active {{ display: block; }}
  ul {{ padding-left: 22px; }}
  a {{ color: #2c6cdf; }}
  @media (max-width: 600px) {{
    body {{ padding: 12px; }}
    .emis-tab {{ padding: 8px 10px; font-size: 12px; }}
  }}
</style>
</head>
<body>
  <h1>{escape(title)}</h1>
  <div class="emis-meta">{week_start.date()} – {week_end.date()}</div>
  <div class="emis-tabs">{tab_buttons}</div>
  {panel_divs}
<script>
(function() {{
  const tabs = document.querySelectorAll('.emis-tab');
  const panels = document.querySelectorAll('.emis-panel');
  function activate(tid) {{
    tabs.forEach(t => t.classList.toggle('active', t.dataset.target === tid));
    panels.forEach(p => p.classList.toggle('active', p.id === 'emis-panel-' + tid));
    try {{ history.replaceState(null, '', '#' + tid); }} catch (e) {{}}
  }}
  tabs.forEach(t => t.addEventListener('click', () => activate(t.dataset.target)));
  const initial = (location.hash || '#summary').slice(1);
  activate(document.getElementById('emis-panel-' + initial) ? initial : 'summary');
}})();
</script>
</body></html>
"""
