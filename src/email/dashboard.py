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

_STATUS_LABEL = {
    # Display text used in row metadata. "new" is suppressed (the • icon
    # already implies it); "carried_over" reads more naturally as "last week".
    "new": "",
    "carried_over": "last week",
    "resolved": "resolved",
    "stale": "stale",
}


def _status_label(status: str) -> str:
    return _STATUS_LABEL.get(status, status or "")
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
    summary = agenda.get("week_summary", "")
    if isinstance(summary, list):
        items = "".join(
            f"<li style='margin-bottom:6px'>{escape(str(s))}</li>"
            for s in summary if str(s).strip()
        )
        if not items:
            return "<p style='font-size:15px;line-height:1.6'>(no summary)</p>"
        return f"<ul style='font-size:15px;line-height:1.6;padding-left:20px'>{items}</ul>"
    text = escape(str(summary or "") or "(no summary)")
    return f"<p style='font-size:15px;line-height:1.6'>{text}</p>"


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
                    f"{escape(a.get('owner', '?'))}"
                    + (f" · {escape(_status_label(a.get('status', 'new')))}" if _status_label(a.get('status', 'new')) else "")
                    + "</li>"
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


def _render_priorities_panel(agenda: dict, pinned_items: set[str] | None = None) -> str:
    items = agenda.get("priorities", [])
    if not items:
        return "<p style='color:#aaa'>No priorities this week.</p>"
    pinned_items = pinned_items or set()
    has_cats = any(p.get("category") for p in items)
    rows = []
    for p in items:
        title_text = p.get("title", "")
        is_pinned = bool(p.get("pinned")) or title_text in pinned_items
        title = _linked(f"<strong>{escape(title_text)}</strong>", p.get("web_link", ""))
        why = (
            f"<div style='color:#888;font-size:12px;font-style:italic;margin-top:2px'>"
            f"Why now: {escape(p['why_now'])}</div>"
            if p.get("why_now") else ""
        )
        src = (
            f"<em style='color:#888;font-size:12px'>({escape(p['source_subject'])})</em>"
            if p.get("source_subject") else ""
        )
        cat_tag = _category_tag(p.get("category", ""))
        pin_marker = ""
        if is_pinned:
            pin_marker = (
                "<span title='Pinned by you' style='color:#a55a00;"
                "margin-right:4px;vertical-align:middle'>📌</span>"
            )
        cat = p.get("category", "")
        cat_attr = f" data-cat='{escape(cat)}'" if cat else ""
        rows.append(
            f"<li class='emis-priority-row'{cat_attr} style='margin-bottom:10px'>"
            f"{pin_marker}{_urgency_badge(p.get('urgency', 'medium'))} {cat_tag}{title} — "
            f"{escape(p.get('reason', ''))} {src}"
            f"{_pin_button(title_text, is_pinned)}"
            f"{why}</li>"
        )
    filter_bar = (
        _category_filter_bar("#emis-panel-priorities .emis-priority-row")
        if has_cats else ""
    )
    return f"{filter_bar}<ul style='line-height:1.6'>{''.join(rows)}</ul>"


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


def _closure_buttons(item_text: str) -> str:
    """Tiny ✓ / snooze / ✕ button row injected when the dashboard is rendered
    interactively (Web UI). Hidden when the same panel renders inside an email."""
    safe = escape(item_text).replace("'", "&#39;")
    return (
        f"<span class='emis-closure' data-item='{safe}' "
        f"style='display:none;margin-left:8px;font-size:11px'>"
        f"<button class='emis-act' data-act='done' title='Mark done' "
        f"style='border:1px solid #cfe0ff;background:#eef4ff;color:#2c6cdf;"
        f"border-radius:4px;padding:2px 6px;cursor:pointer;margin-right:2px'>"
        f"✓ done</button>"
        f"<button class='emis-act' data-act='snooze' data-days='1' "
        f"style='border:1px solid #eee;background:#fafafa;color:#666;"
        f"border-radius:4px;padding:2px 6px;cursor:pointer;margin-right:2px'>"
        f"1d</button>"
        f"<button class='emis-act' data-act='snooze' data-days='7' "
        f"style='border:1px solid #eee;background:#fafafa;color:#666;"
        f"border-radius:4px;padding:2px 6px;cursor:pointer;margin-right:2px'>"
        f"1w</button>"
        f"<button class='emis-act' data-act='snooze' data-days='next-mon' "
        f"style='border:1px solid #eee;background:#fafafa;color:#666;"
        f"border-radius:4px;padding:2px 6px;cursor:pointer;margin-right:2px'>"
        f"Mon</button>"
        f"<button class='emis-act' data-act='drop' title='Drop — not relevant' "
        f"style='border:1px solid #f3d6d6;background:#fdf2f2;color:#b04141;"
        f"border-radius:4px;padding:2px 6px;cursor:pointer'>"
        f"✕</button>"
        f"</span>"
    )


def _source_tag(source: str, to_party: str = "") -> str:
    if source == "promised":
        to = (" → " + escape(to_party)) if to_party else ""
        return (
            f"<span style='background:#fff4e0;color:#a55a00;font-size:10px;"
            f"padding:1px 5px;border-radius:3px;margin-right:6px;"
            f"text-transform:uppercase;letter-spacing:0.4px;vertical-align:middle'>"
            f"promised{to}</span>"
        )
    if source == "asked":
        return (
            f"<span style='background:#eef4ff;color:#2c6cdf;font-size:10px;"
            f"padding:1px 5px;border-radius:3px;margin-right:6px;"
            f"text-transform:uppercase;letter-spacing:0.4px;vertical-align:middle'>"
            f"asked</span>"
        )
    return ""


_CATEGORY_COLORS = {
    "clinical": ("#e8f5e9", "#1b5e20"),
    "business": ("#f3e8ff", "#5b21b6"),
    "admin": ("#fff4e0", "#a55a00"),
    "personal": ("#fce4ec", "#880e4f"),
}

_CATEGORIES = ("clinical", "business", "admin", "personal")


def _category_filter_bar(panel_scope: str) -> str:
    """Render a row of filter chips that show/hide items by category.

    `panel_scope` is a CSS selector identifying the rows the chips should
    toggle (e.g. ``#emis-panel-actions .emis-action-row``).
    """
    chips = [
        f"<button class='emis-cat-chip emis-cat-active' "
        f"data-scope=\"{escape(panel_scope)}\" data-filter='all'>All</button>"
    ]
    for cat in _CATEGORIES:
        bg, fg = _CATEGORY_COLORS[cat]
        chips.append(
            f"<button class='emis-cat-chip' style='background:{bg};color:{fg}' "
            f"data-scope=\"{escape(panel_scope)}\" data-filter='{cat}'>{cat}</button>"
        )
    return (
        f"<div class='emis-cat-filter' style='margin-bottom:12px'>"
        f"<span style='color:#888;font-size:12px;margin-right:8px'>filter:</span>"
        f"{' '.join(chips)}"
        f"</div>"
    )


def _category_tag(category: str) -> str:
    if not category:
        return ""
    bg, fg = _CATEGORY_COLORS.get(category, ("#eee", "#666"))
    return (
        f"<span class='emis-cat-tag' data-cat='{escape(category)}' "
        f"style='background:{bg};color:{fg};font-size:10px;"
        f"padding:1px 5px;border-radius:3px;margin-right:6px;"
        f"text-transform:uppercase;letter-spacing:0.4px;vertical-align:middle'>"
        f"{escape(category)}</span>"
    )


def _note_button(task_text: str, existing_note: str) -> str:
    safe = escape(task_text).replace("'", "&#39;")
    safe_note = escape(existing_note).replace("'", "&#39;")
    label = "📝 note" if not existing_note else "📝 edit"
    return (
        f"<span class='emis-note-control' data-item='{safe}' "
        f"data-note='{safe_note}' style='display:none;margin-left:6px;"
        f"font-size:11px'>"
        f"<button class='emis-note-edit' style='border:1px solid #eee;"
        f"background:#fafafa;color:#666;border-radius:4px;padding:2px 6px;"
        f"cursor:pointer'>{label}</button>"
        f"</span>"
    )


def _pin_button(task_text: str, is_pinned: bool) -> str:
    safe = escape(task_text).replace("'", "&#39;")
    label = "📌 pinned" if is_pinned else "📌 pin"
    bg = "#fff4e0" if is_pinned else "#fafafa"
    color = "#a55a00" if is_pinned else "#666"
    return (
        f"<span class='emis-pin-control' data-item='{safe}' "
        f"data-pinned='{1 if is_pinned else 0}' style='display:none;"
        f"margin-left:6px;font-size:11px'>"
        f"<button class='emis-pin-toggle' style='border:1px solid #eee;"
        f"background:{bg};color:{color};border-radius:4px;padding:2px 6px;"
        f"cursor:pointer'>{label}</button>"
        f"</span>"
    )


def _render_actions_panel(agenda: dict, pinned_items: set[str] | None = None) -> str:
    items = agenda.get("action_items", [])
    if not items:
        return "<p style='color:#aaa'>No action items.</p>"
    pinned_items = pinned_items or set()
    has_cats = any(a.get("category") for a in items)
    rows = []
    for a in items:
        task_text = a.get("task", "")
        is_pinned = task_text in pinned_items
        title = _linked(f"<strong>{escape(task_text)}</strong>", a.get("web_link", ""))
        source_tag = _source_tag(a.get("source", ""), a.get("to_party", ""))
        cat_tag = _category_tag(a.get("category", ""))
        meta_bits = [
            _urgency_badge(a.get("urgency", "medium")),
            escape(a.get("owner", "?")),
            f"due {escape(a.get('due', ''))}" if a.get("due") else "",
            escape(_status_label(a.get("status", "new"))),
        ]
        meta = " · ".join(b for b in meta_bits if b)
        src = (
            f" <em style='color:#888'>({escape(a['source_subject'])})</em>"
            if a.get("source_subject") else ""
        )
        note_text = a.get("user_note", "")
        note_line = ""
        if note_text:
            note_line = (
                f"<li class='emis-note-display' style='color:#2c6cdf;"
                f"font-style:italic'>📝 {escape(note_text)}</li>"
            )
        cat = a.get("category", "")
        cat_attr = f" data-cat='{escape(cat)}'" if cat else ""
        pin_marker = (
            "<span title='Pinned by you' style='color:#a55a00;"
            "margin-right:4px;vertical-align:middle'>📌</span>"
            if is_pinned else ""
        )
        rows.append(
            f"<li class='emis-action-row'{cat_attr} style='margin-bottom:8px'>"
            f"{pin_marker}{source_tag}{cat_tag}{title}"
            f"{_add_to_cal_button(a)}"
            f"{_closure_buttons(task_text)}"
            f"{_note_button(task_text, note_text)}"
            f"{_pin_button(task_text, is_pinned)}"
            f"<ul style='margin:2px 0 0 0;padding-left:18px;color:#666;font-size:12px'>"
            f"<li>{meta}{src}</li>{note_line}</ul></li>"
        )
    filter_bar = (
        _category_filter_bar("#emis-panel-actions .emis-action-row")
        if has_cats else ""
    )
    return f"{filter_bar}<ul style='line-height:1.6'>{''.join(rows)}</ul>"


def _render_followups_panel(agenda: dict) -> str:
    items = agenda.get("follow_ups", [])
    if not items:
        return "<p style='color:#aaa'>Nothing pending from others.</p>"
    rows = []
    for f in items:
        title = _linked(f"<strong>{escape(f['thread'])}</strong>", f.get("web_link", ""))
        meta_bits = [
            f"waiting on {escape(f['counterparty'])}",
            escape(_status_label(f.get("status", "new"))),
            f"open {f['weeks_open']}w" if f.get("weeks_open") else "",
        ]
        meta = " · ".join(b for b in meta_bits if b)
        rows.append(
            f"<li style='margin-bottom:8px'>{title} — {escape(f['ask'])}"
            f"<ul style='margin:2px 0 0 0;padding-left:18px;color:#666;font-size:12px'>"
            f"<li>{meta}</li></ul></li>"
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
    ("backlog", "Backlog"),
    ("history", "History"),
    ("fyi", "FYI"),
]


def _render_backlog_panel(agenda: dict, prior_agendas: list[dict] | None) -> str:
    """Items that have lingered: status==carried_over or weeks_open >= 3.

    Pulls from the current agenda plus any priors passed in (memory.py output).
    """
    seen: set[str] = set()
    rows: list[str] = []

    def _add(label: str, key: str, why: str):
        sig = f"{label}::{key}"
        if sig in seen:
            return
        seen.add(sig)
        rows.append(f"<li style='margin-bottom:6px'>{label} <span style='color:#888;font-size:12px'>— {why}</span></li>")

    for a in agenda.get("action_items", []):
        if a.get("status") in ("carried_over", "stale"):
            title = _linked(f"<strong>{escape(a['task'])}</strong>", a.get("web_link", ""))
            label = _status_label(a.get("status", ""))
            suffix = f" · {escape(label)}" if label else ""
            _add(title, a.get("task", ""), f"action item{suffix}")
    for f in agenda.get("follow_ups", []):
        weeks = f.get("weeks_open", 0)
        if f.get("status") in ("carried_over", "stale") or weeks >= 3:
            title = _linked(f"<strong>{escape(f['thread'])}</strong>", f.get("web_link", ""))
            why = f"follow-up · waiting on {escape(f.get('counterparty', ''))} · open {weeks}w"
            _add(title, f.get("thread", ""), why)

    # Also pull from prior agendas if provided (memory across weeks)
    for entry in (prior_agendas or []):
        a = entry.get("agenda", {})
        iso = entry.get("iso_week", "")
        for it in a.get("action_items", []):
            if it.get("status") in ("carried_over", "stale"):
                title = _linked(f"<strong>{escape(it.get('task', ''))}</strong>", it.get("web_link", ""))
                _add(title, it.get("task", ""), f"from week {iso} · still open")
        for it in a.get("follow_ups", []):
            if it.get("status") in ("carried_over", "stale") or it.get("weeks_open", 0) >= 3:
                title = _linked(f"<strong>{escape(it.get('thread', ''))}</strong>", it.get("web_link", ""))
                _add(title, it.get("thread", ""), f"from week {iso} · still waiting")

    if not rows:
        return "<p style='color:#aaa'>Backlog is empty — nice.</p>"
    return f"<ul style='line-height:1.6'>{''.join(rows)}</ul>"


def _render_history_panel(closures: dict[str, list[dict[str, str]]] | None) -> str:
    """Done items grouped by month (recent month first)."""
    if not closures:
        return "<p style='color:#aaa'>No completion history yet — reply 'done with X' to start tracking.</p>"
    done = closures.get("done", []) or []
    if not done:
        return "<p style='color:#aaa'>No completion history yet — reply 'done with X' to start tracking.</p>"

    # Group by YYYY-MM, sort newest first within each group.
    by_month: dict[str, list[dict[str, str]]] = {}
    for d in done:
        key = (d.get("completed_at") or "")[:7]
        if not key:
            continue
        by_month.setdefault(key, []).append(d)

    parts: list[str] = []
    for ym in sorted(by_month.keys(), reverse=True):
        items = sorted(by_month[ym], key=lambda d: d.get("completed_at", ""), reverse=True)
        try:
            label = datetime.strptime(ym, "%Y-%m").strftime("%B %Y")
        except ValueError:
            label = ym
        rows = "".join(
            f"<li style='margin-bottom:4px'>"
            f"<strong>{escape(d['item_match'])}</strong> "
            f"<span style='color:#888;font-size:12px'>"
            f"— {escape((d.get('completed_at') or '')[:10])}"
            f"</span></li>"
            for d in items
        )
        parts.append(
            f"<div style='margin-bottom:18px'>"
            f"<div style='font-weight:600;color:#444;margin-bottom:6px'>"
            f"{label} <span style='color:#888;font-weight:400;font-size:12px'>"
            f"({len(items)} completed)</span></div>"
            f"<ul style='line-height:1.55;margin-top:0'>{rows}</ul></div>"
        )
    return "".join(parts)


def render_dashboard_html(
    agenda: dict[str, Any],
    week_start: datetime,
    week_end: datetime,
    mode: str = "monday",
    closures: dict[str, list[dict[str, str]]] | None = None,
    prior_agendas: list[dict[str, Any]] | None = None,
    closure_token: str | None = None,
    pinned_items: set[str] | None = None,
    nav_html: str = "",
) -> str:
    """Render an interactive single-page dashboard for the agenda."""
    title = {
        "monday": "Weekly Dashboard",
        "wednesday": "Mid-Week Dashboard",
        "friday": "End-of-Week Dashboard",
    }.get(mode, "Agenda Dashboard")

    pins = pinned_items or set()
    panels = {
        "summary": _render_summary_panel(agenda, week_start, week_end)
                   + _render_week_at_a_glance(agenda, week_start),
        "priorities": _render_priorities_panel(agenda, pins),
        "meetings": _render_meetings_panel(agenda),
        "actions": _render_actions_panel(agenda, pins),
        "followups": _render_followups_panel(agenda),
        "backlog": _render_backlog_panel(agenda, prior_agendas),
        "history": _render_history_panel(closures),
        "fyi": _render_fyi_panel(agenda),
    }

    tab_buttons = "".join(
        f"<button class='emis-tab' data-target='{tid}'>{escape(label)}</button>"
        for tid, label in _TABS
    )
    _labels = dict(_TABS)
    panel_divs = "".join(
        f"<div class='emis-panel' id='emis-panel-{tid}' "
        f"data-print-label='{escape(_labels.get(tid, tid))}'>{html}</div>"
        for tid, html in panels.items()
    )

    import json as _json
    closure_token_js = _json.dumps(closure_token) if closure_token else "null"

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
  .emis-undo {{ margin-left: 8px; font-size: 11px; color: #2c6cdf;
    cursor: pointer; text-decoration: underline; }}
  .emis-cat-chip {{ font-size: 11px; padding: 3px 9px; border-radius: 12px;
    border: 1px solid #ddd; background: #f5f5f5; color: #666;
    cursor: pointer; margin-right: 4px; font-family: inherit;
    text-transform: uppercase; letter-spacing: 0.4px; }}
  .emis-cat-chip:hover {{ border-color: #aaa; }}
  .emis-cat-chip.emis-cat-active {{ outline: 2px solid #2c6cdf; outline-offset: -1px; }}
  .emis-note-editor textarea {{ width: 95%; min-height: 50px; font-family: inherit;
    font-size: 12px; padding: 4px; border: 1px solid #cfe0ff; border-radius: 4px;
    margin-top: 4px; }}
  .emis-print-link {{ float: right; font-size: 12px; color: #888; cursor: pointer; }}
  @media (max-width: 600px) {{
    body {{ padding: 12px; }}
    .emis-tab {{ padding: 8px 10px; font-size: 12px; }}
  }}
  @media print {{
    .emis-tabs, .emis-closure, .emis-note-control, .emis-pin-control,
    .emis-add-cal, .emis-print-link {{ display: none !important; }}
    .emis-panel {{ display: block !important; page-break-inside: avoid; }}
    .emis-panel::before {{ content: attr(data-print-label); display: block;
      font-size: 16px; font-weight: 700; margin: 18px 0 6px; color: #222; }}
    body {{ background: white; max-width: none; }}
    h1 {{ font-size: 20px; }}
    a {{ color: #222; text-decoration: none; }}
  }}
</style>
</head>
<body>
  {nav_html}
  <h1>{escape(title)} <span class="emis-print-link" onclick="window.print()">🖨 print view</span></h1>
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

  // ─── Category filter chips (works without a token, in both views) ──────
  document.querySelectorAll('.emis-cat-chip').forEach(chip => {{
    chip.addEventListener('click', (ev) => {{
      ev.preventDefault();
      const scope = chip.dataset.scope;
      const filter = chip.dataset.filter;
      // Toggle active state on sibling chips
      const bar = chip.parentElement;
      bar.querySelectorAll('.emis-cat-chip').forEach(c => c.classList.remove('emis-cat-active'));
      chip.classList.add('emis-cat-active');
      // Show/hide rows in scope
      document.querySelectorAll(scope).forEach(row => {{
        const cat = row.dataset.cat || '';
        const show = (filter === 'all') || (cat === filter);
        row.style.display = show ? '' : 'none';
      }});
    }});
  }});

  const closureToken = {closure_token_js!s};
  if (!closureToken) return;  // email render — no buttons, no POSTs

  async function post(body) {{
    const resp = await fetch(window.location.pathname + '?token=' + encodeURIComponent(closureToken), {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(body),
    }});
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    return resp.json();
  }}

  // Reveal all interactive controls now that we have a token.
  document.querySelectorAll('.emis-closure, .emis-note-control, .emis-pin-control')
    .forEach(el => el.style.display = 'inline-block');

  // ─── Closure buttons (done / snooze / drop) with inline Undo ────────────
  document.querySelectorAll('.emis-act').forEach(btn => {{
    btn.addEventListener('click', async (ev) => {{
      ev.preventDefault();
      const row = btn.closest('.emis-action-row, .emis-priority-row');
      const span = btn.closest('.emis-closure');
      const action = btn.dataset.act;
      const item = span ? span.dataset.item : '';
      let untilIso = null;
      if (action === 'snooze') {{
        const today = new Date();
        if (btn.dataset.days === 'next-mon') {{
          const day = today.getDay();
          const delta = ((1 - day) + 7) % 7 || 7;
          today.setDate(today.getDate() + delta);
        }} else {{
          today.setDate(today.getDate() + parseInt(btn.dataset.days, 10));
        }}
        untilIso = today.toISOString().slice(0, 10);
      }}
      if (row) {{ row.style.opacity = '0.4'; row.style.textDecoration = 'line-through'; }}
      try {{
        await post({{ action: action, item_match: item, until_iso: untilIso }});
      }} catch (err) {{
        if (row) {{ row.style.opacity = '1'; row.style.textDecoration = 'none'; }}
        alert('Could not save: ' + err.message);
        return;
      }}
      // Inline Undo link, auto-hides after 8s
      const undo = document.createElement('span');
      undo.className = 'emis-undo';
      undo.textContent = '↶ undo';
      undo.addEventListener('click', async () => {{
        try {{
          await post({{ action: 'undo', item_match: item, original_action: action }});
          if (row) {{ row.style.opacity = '1'; row.style.textDecoration = 'none'; }}
          undo.remove();
        }} catch (err) {{ alert('Undo failed: ' + err.message); }}
      }});
      span.appendChild(undo);
      setTimeout(() => undo.remove(), 8000);
    }});
  }});

  // ─── Note edit/save ──────────────────────────────────────────────────────
  document.querySelectorAll('.emis-note-edit').forEach(btn => {{
    btn.addEventListener('click', (ev) => {{
      ev.preventDefault();
      const ctl = btn.closest('.emis-note-control');
      const row = btn.closest('.emis-action-row');
      const item = ctl.dataset.item;
      const existing = ctl.dataset.note || '';
      if (ctl.querySelector('textarea')) return;  // already editing
      const wrap = document.createElement('div');
      wrap.className = 'emis-note-editor';
      const ta = document.createElement('textarea');
      ta.value = existing;
      ta.placeholder = 'Add a note that feeds the next agenda…';
      const save = document.createElement('button');
      save.textContent = 'save';
      save.style.cssText = 'margin-top:4px;margin-right:4px;border:1px solid #2c6cdf;background:#2c6cdf;color:white;border-radius:4px;padding:3px 10px;cursor:pointer;font-size:12px';
      const cancel = document.createElement('button');
      cancel.textContent = 'cancel';
      cancel.style.cssText = 'margin-top:4px;border:1px solid #ccc;background:white;color:#666;border-radius:4px;padding:3px 10px;cursor:pointer;font-size:12px';
      wrap.appendChild(ta);
      wrap.appendChild(document.createElement('br'));
      wrap.appendChild(save);
      wrap.appendChild(cancel);
      row.appendChild(wrap);
      ta.focus();
      cancel.addEventListener('click', () => wrap.remove());
      save.addEventListener('click', async () => {{
        const note = ta.value.trim();
        try {{
          await post({{ action: 'set_note', item_match: item, note: note }});
          ctl.dataset.note = note;
          btn.textContent = note ? '📝 edit' : '📝 note';
          // Update or insert displayed note
          let disp = row.querySelector('.emis-note-display');
          if (note) {{
            if (!disp) {{
              disp = document.createElement('li');
              disp.className = 'emis-note-display';
              disp.style.cssText = 'color:#2c6cdf;font-style:italic';
              row.querySelector('ul').appendChild(disp);
            }}
            disp.textContent = '📝 ' + note;
          }} else if (disp) {{
            disp.remove();
          }}
          wrap.remove();
        }} catch (err) {{ alert('Save failed: ' + err.message); }}
      }});
    }});
  }});

  // ─── Pin toggle ──────────────────────────────────────────────────────────
  document.querySelectorAll('.emis-pin-toggle').forEach(btn => {{
    btn.addEventListener('click', async (ev) => {{
      ev.preventDefault();
      const ctl = btn.closest('.emis-pin-control');
      const item = ctl.dataset.item;
      const isPinned = ctl.dataset.pinned === '1';
      const action = isPinned ? 'unpin' : 'pin';
      try {{
        await post({{ action: action, item_match: item }});
        ctl.dataset.pinned = isPinned ? '0' : '1';
        btn.textContent = isPinned ? '📌 pin' : '📌 pinned';
        btn.style.background = isPinned ? '#fafafa' : '#fff4e0';
        btn.style.color = isPinned ? '#666' : '#a55a00';
      }} catch (err) {{ alert('Pin failed: ' + err.message); }}
    }});
  }});
}})();
</script>
</body></html>
"""
