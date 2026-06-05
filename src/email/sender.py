"""Render the agenda as HTML + plaintext and send via Amazon SES."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from html import escape
from typing import Any

import boto3

logger = logging.getLogger(__name__)

_STATUS_MARK = {"new": "•", "carried_over": "↻", "resolved": "✓", "stale": "⚠"}

_STATUS_LABEL = {
    "new": "",
    "carried_over": "last week",
    "resolved": "resolved",
    "stale": "stale",
}


def _status_label(status: str) -> str:
    return _STATUS_LABEL.get(status, status or "")
_URGENCY_COLOR = {"high": "#c0392b", "medium": "#d68910", "low": "#7f8c8d"}


def _bucket_by_due(action_items: list[dict[str, Any]], today: date) -> dict[str, list[dict]]:
    """Group action_items into 'this week / this month / this quarter / later'.

    Items without a parseable due_date land under 'undated'.
    """
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
    """Build per-counterparty buckets of {owed_to_you, owed_by_you}.

    - follow_ups → things owed to you
    - action_items (source=promised) → things you owe them
    - action_items with non-self owner → things they owe you
    """
    by_party: dict[str, dict[str, list]] = defaultdict(lambda: {"owed_to_you": [], "owed_by_you": []})
    for f in agenda.get("follow_ups", []):
        cp = (f.get("counterparty") or "").strip()
        if cp:
            by_party[cp]["owed_to_you"].append(f)
    for a in agenda.get("action_items", []):
        if a.get("source") == "promised":
            to = (a.get("to_party") or "").strip()
            if to:
                by_party[to]["owed_by_you"].append(a)
            continue
        owner = (a.get("owner") or "").strip()
        if owner and owner.lower() != "you":
            by_party[owner]["owed_to_you"].append(a)
    # Only return counterparties with >= 2 items across both buckets — single
    # mentions aren't worth a rollup section.
    return {
        cp: buckets for cp, buckets in by_party.items()
        if len(buckets["owed_to_you"]) + len(buckets["owed_by_you"]) >= 2
    }


def _linked(title_html: str, web_link: str) -> str:
    """Wrap a title fragment in a subtle link if web_link is non-empty."""
    if not web_link:
        return title_html
    return (
        f"<a href='{escape(web_link)}' "
        f"style='color:#2c6cdf;text-decoration:underline'>"
        f"{title_html}</a>"
    )


def _dashboard_banner(web_ui_url: str, web_ui_token: str, week_iso: str, mode: str) -> str:
    """Render a header bar with a link into the interactive dashboard.

    Returns an empty string if either piece of config is missing (e.g. on
    local dry-runs where there's no deployed Web UI to link to).
    """
    if not web_ui_url or not web_ui_token:
        return ""
    from urllib.parse import urlencode
    qs = urlencode({"token": web_ui_token, "week": week_iso, "mode": mode})
    url = f"{web_ui_url.rstrip('/')}/?{qs}"
    return (
        f"<div style='margin:-8px -24px 16px;padding:10px 24px;"
        f"background:#eef4ff;border-bottom:1px solid #cfe0ff;"
        f"font-size:13px;color:#2c6cdf'>"
        f"<a href='{url}' style='color:#2c6cdf;text-decoration:none;font-weight:600'>"
        f"View interactive dashboard →</a>"
        f"<span style='color:#666;font-weight:400'> "
        f"&nbsp;·&nbsp; tabs, calendar links, backlog &amp; history</span>"
        f"</div>"
    )


def render_retrospective_html(retro: dict[str, list[dict[str, str]]]) -> str:
    """Render the Friday retrospective as an HTML block. Empty string if no data."""
    landed = retro.get("landed", [])
    slipped = retro.get("slipped", [])
    carried = retro.get("carried", [])
    if not (landed or slipped or carried):
        return ""

    def _bullets(items: list[dict]) -> str:
        if not items:
            return "<li style='color:#aaa'>None</li>"
        return "".join(
            f"<li style='margin-bottom:4px'>"
            f"<span style='color:#888;font-size:11px;text-transform:uppercase;"
            f"margin-right:6px'>{escape(i.get('kind', ''))}</span>"
            f"{escape(i.get('title', ''))}"
            + (
                f" <em style='color:#888;font-size:11px'>({escape(i['reason'])})</em>"
                if i.get("reason") else ""
            )
            + "</li>"
            for i in items
        )

    return (
        f"<div style='background:#f7faff;border:1px solid #cfe0ff;"
        f"border-radius:8px;padding:14px 18px;margin-bottom:20px'>"
        f"<h2 style='margin:0 0 8px;font-size:15px;color:#2c6cdf'>"
        f"This week's retrospective</h2>"
        f"<div style='display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;font-size:13px'>"
        f"<div><strong style='color:#1b5e20'>✓ Landed ({len(landed)})</strong>"
        f"<ul style='margin:6px 0 0;padding-left:18px'>{_bullets(landed)}</ul></div>"
        f"<div><strong style='color:#b04141'>⚠ Slipped ({len(slipped)})</strong>"
        f"<ul style='margin:6px 0 0;padding-left:18px'>{_bullets(slipped)}</ul></div>"
        f"<div><strong style='color:#666'>→ Carries forward ({len(carried)})</strong>"
        f"<ul style='margin:6px 0 0;padding-left:18px'>{_bullets(carried)}</ul></div>"
        f"</div></div>"
    )


def render_retrospective_text(retro: dict[str, list[dict[str, str]]]) -> str:
    landed = retro.get("landed", [])
    slipped = retro.get("slipped", [])
    carried = retro.get("carried", [])
    if not (landed or slipped or carried):
        return ""
    lines = ["", "THIS WEEK'S RETROSPECTIVE", ""]
    lines.append(f"Landed ({len(landed)}):")
    for i in landed or [{"title": "(none)"}]:
        lines.append(f"  ✓ [{i.get('kind', '?')}] {i.get('title', '')}")
    lines.append("")
    lines.append(f"Slipped ({len(slipped)}):")
    for i in slipped or [{"title": "(none)"}]:
        reason = f" ({i['reason']})" if i.get("reason") else ""
        lines.append(f"  ⚠ [{i.get('kind', '?')}] {i.get('title', '')}{reason}")
    lines.append("")
    lines.append(f"Carries forward ({len(carried)}):")
    for i in carried or [{"title": "(none)"}]:
        lines.append(f"  → [{i.get('kind', '?')}] {i.get('title', '')}")
    return "\n".join(lines) + "\n"


def _summary_html(summary: Any) -> str:
    """Render week_summary as bullet list (new schema) or paragraph (legacy)."""
    if isinstance(summary, list):
        items = "".join(
            f"<li style='margin-bottom:6px'>{escape(str(s))}</li>"
            for s in summary if str(s).strip()
        )
        return f"<ul style='font-size:15px;line-height:1.55;padding-left:20px'>{items}</ul>"
    return f"<p style=\"font-size:15px;line-height:1.55\">{escape(str(summary or ''))}</p>"


def render_html(agenda: dict[str, Any], week_start: datetime, week_end: datetime, mode: str = "monday", web_ui_url: str = "", web_ui_token: str = "") -> str:
    title = {
        "monday": "Weekly Agenda",
        "wednesday": "Mid-Week Check-in",
        "friday": "End-of-Week Recap",
    }.get(mode, "Agenda")

    def section(name: str, body: str) -> str:
        return f"<h2 style='margin-top:24px;font-size:18px'>{escape(name)}</h2>{body}"

    def ul(items: list[str]) -> str:
        if not items:
            return "<p style='color:#888'>(none)</p>"
        return "<ul style='line-height:1.55'>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>"

    def status_badge(status: str) -> str:
        mark = _STATUS_MARK.get(status, "•")
        return f"<span title='{status}' style='font-weight:bold'>{mark}</span>"

    def urgency_badge(u: str) -> str:
        c = _URGENCY_COLOR.get(u, "#7f8c8d")
        return f"<span style='color:{c};font-size:11px;text-transform:uppercase'>{u}</span>"

    def _priority_li(p: dict) -> str:
        title = _linked(f"<strong>{escape(p['title'])}</strong>", p.get("web_link", ""))
        src = f" <em style='color:#666'>({escape(p['source_subject'])})</em>" if p.get("source_subject") else ""
        why = (
            f"<br><span style='color:#888;font-size:12px;font-style:italic'>"
            f"Why now: {escape(p['why_now'])}</span>"
            if p.get("why_now") else ""
        )
        return f"{urgency_badge(p.get('urgency', 'medium'))} {title} — {escape(p['reason'])}{src}{why}"

    def _meeting_li(m: dict) -> str:
        title = _linked(f"<strong>{escape(m['subject'])}</strong>", m.get("web_link", ""))
        prep = f"<br><span style='color:#555'>Prep: {escape(m['prep_notes'])}</span>" if m.get("prep_notes") else ""
        return (
            f"{title} <span style='color:#888;font-size:11px'>[{escape(m.get('source', '?'))}]</span> "
            f"— {escape(m['when'])} with {escape(m['participants'])}{prep}"
        )

    def _action_li(a: dict) -> str:
        # Two bullets per action: task on top, metadata as a nested sub-bullet.
        title = _linked(f"<strong>{escape(a['task'])}</strong>", a.get("web_link", ""))
        source = a.get("source", "")
        source_tag = ""
        if source == "promised":
            to = escape(a.get("to_party", ""))
            source_tag = (
                f"<span style='background:#fff4e0;color:#a55a00;font-size:10px;"
                f"padding:1px 5px;border-radius:3px;margin-right:4px;"
                f"text-transform:uppercase;letter-spacing:0.4px'>promised{f' → {to}' if to else ''}</span>"
            )
        elif source == "asked":
            source_tag = (
                f"<span style='background:#eef4ff;color:#2c6cdf;font-size:10px;"
                f"padding:1px 5px;border-radius:3px;margin-right:4px;"
                f"text-transform:uppercase;letter-spacing:0.4px'>asked</span>"
            )
        meta_bits = [
            urgency_badge(a.get("urgency", "medium")),
            escape(a.get("owner", "?")),
            f"due {escape(a.get('due', ''))}" if a.get("due") else "",
            escape(_status_label(a.get("status", "new"))),
        ]
        if a.get("category"):
            meta_bits.append(escape(a["category"]))
        meta = " · ".join(b for b in meta_bits if b)
        src = (
            f"<em style='color:#888'>({escape(a['source_subject'])})</em>"
            if a.get("source_subject") else ""
        )
        note = ""
        if a.get("user_note"):
            note = (
                f"<li style='color:#2c6cdf;font-size:12px;font-style:italic'>"
                f"📝 {escape(a['user_note'])}</li>"
            )
        sub_bullet = (
            f"<ul style='margin:2px 0 0 0;padding-left:18px;color:#666;font-size:12px'>"
            f"<li>{meta} {src}</li>{note}</ul>"
        )
        return f"{status_badge(a.get('status', 'new'))} {source_tag}{title}{sub_bullet}"

    def _followup_li(f: dict) -> str:
        title = _linked(f"<strong>{escape(f['thread'])}</strong>", f.get("web_link", ""))
        weeks = f" <span style='color:#888'>(open {f['weeks_open']}w)</span>" if f.get("weeks_open") else ""
        return f"{status_badge(f.get('status', 'new'))} {title} — waiting on {escape(f['counterparty'])}: {escape(f['ask'])}{weeks}"

    priorities = ul([_priority_li(p) for p in agenda.get("priorities", [])])
    meetings = ul([_meeting_li(m) for m in agenda.get("meetings", [])])
    actions = ul([_action_li(a) for a in agenda.get("action_items", [])])
    follow_ups = ul([_followup_li(f) for f in agenda.get("follow_ups", [])])
    fyi = ul([escape(x) for x in agenda.get("fyi", [])])

    body_html = "".join([
        section("Priorities", priorities),
        section("Meetings", meetings),
        section("Action items", actions),
        section("Follow-ups (waiting on others)", follow_ups),
    ])

    # Computed sections — derived from the data above, no extra LLM tokens.
    today = week_end.date()
    buckets = _bucket_by_due(agenda.get("action_items", []), today)
    bucket_order = ("this week", "this month", "this quarter", "later")
    coming_up_html = ""
    for label in bucket_order:
        items = buckets.get(label, [])
        if not items:
            continue
        rows = "".join(
            f"<li>{_linked(escape(a.get('task', '')), a.get('web_link', ''))} "
            f"<span style='color:#666;font-size:12px'>"
            f"({escape(a.get('due', '') or a.get('due_date', ''))}, "
            f"{escape(a.get('owner', '?'))})</span></li>"
            for a in items
        )
        coming_up_html += (
            f"<h3 style='margin:14px 0 4px;font-size:13px;color:#666'>"
            f"{label.title()}</h3>"
            f"<ul style='line-height:1.55;margin-top:0'>{rows}</ul>"
        )
    if coming_up_html:
        body_html += section("Coming up (by due date)", coming_up_html)

    rollup = _counterparty_rollup(agenda)
    if rollup:
        rows = ""
        for cp, b in sorted(rollup.items(), key=lambda kv: -(len(kv[1]["owed_to_you"]) + len(kv[1]["owed_by_you"]))):
            owed_to = len(b["owed_to_you"])
            owed_by = len(b["owed_by_you"])
            rows += (
                f"<li><strong>{escape(cp)}</strong> "
                f"<span style='color:#666;font-size:12px'>"
                f"({owed_to} waiting on them, {owed_by} you owe)"
                f"</span></li>"
            )
        body_html += section("By counterparty", f"<ul style='line-height:1.55'>{rows}</ul>")

    body_html += section("FYI", fyi)

    iso = week_start.isocalendar()
    week_iso = f"{iso.year:04d}-W{iso.week:02d}"
    banner = _dashboard_banner(web_ui_url, web_ui_token, week_iso, mode)
    return f"""\
<!doctype html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:680px;margin:auto;padding:24px;color:#222">
  {banner}
  <h1 style="margin-bottom:4px">{escape(title)}</h1>
  <p style="color:#666;margin-top:0">{week_start.date()} – {week_end.date()}</p>
  {_summary_html(agenda.get('week_summary', ''))}
  {body_html}
  <hr style="margin-top:32px;border:none;border-top:1px solid #eee">
  <p style="color:#aaa;font-size:11px">Generated by EMIS. Status: • new, ↻ carried over, ✓ resolved, ⚠ stale.</p>
</body></html>
"""


def render_text(agenda: dict[str, Any], week_start: datetime, week_end: datetime, mode: str = "monday") -> str:
    title = {
        "monday": "WEEKLY AGENDA",
        "wednesday": "MID-WEEK CHECK-IN",
        "friday": "END-OF-WEEK RECAP",
    }.get(mode, "AGENDA")

    summary = agenda.get("week_summary", "")
    if isinstance(summary, list):
        summary_lines = [f"  • {str(s).strip()}" for s in summary if str(s).strip()]
    else:
        summary_lines = [str(summary or "")]
    lines = [
        title,
        f"{week_start.date()} – {week_end.date()}",
        "",
        *summary_lines,
        "",
        "PRIORITIES",
    ]
    for p in agenda.get("priorities", []):
        src = f" ({p['source_subject']})" if p.get("source_subject") else ""
        link = f"\n      {p['web_link']}" if p.get("web_link") else ""
        lines.append(f"  • [{p.get('urgency', 'medium')}] {p['title']}{src}")
        lines.append(f"      {p['reason']}{link}")
        if p.get("why_now"):
            lines.append(f"      Why now: {p['why_now']}")

    lines += ["", "MEETINGS"]
    for m in agenda.get("meetings", []):
        lines.append(f"  • [{m.get('source', '?')}] {m['subject']} — {m['when']} with {m['participants']}")
        if m.get("prep_notes"):
            lines.append(f"      Prep: {m['prep_notes']}")
        if m.get("web_link"):
            lines.append(f"      {m['web_link']}")

    lines += ["", "ACTION ITEMS"]
    for a in agenda.get("action_items", []):
        mark = _STATUS_MARK.get(a.get("status", "new"), "•")
        tag = ""
        if a.get("source") == "promised":
            to = a.get("to_party", "")
            tag = f"[promised{(' → ' + to) if to else ''}] "
        elif a.get("source") == "asked":
            tag = "[asked] "
        lines.append(f"  {mark} {tag}{a['task']}")
        meta_bits = list(filter(None, [
            f"[{a.get('urgency', 'medium')}]",
            a.get("owner", "?"),
            f"due {a.get('due', '')}" if a.get("due") else "",
            _status_label(a.get("status", "new")),
            a.get("category", ""),
        ]))
        meta = " · ".join(meta_bits)
        src = f" ({a['source_subject']})" if a.get("source_subject") else ""
        lines.append(f"      - {meta}{src}")
        if a.get("user_note"):
            lines.append(f"      📝 {a['user_note']}")
        if a.get("web_link"):
            lines.append(f"        {a['web_link']}")

    lines += ["", "FOLLOW-UPS"]
    for f in agenda.get("follow_ups", []):
        mark = _STATUS_MARK.get(f.get("status", "new"), "•")
        weeks = f" (open {f['weeks_open']}w)" if f.get("weeks_open") else ""
        lines.append(f"  {mark} {f['thread']} — waiting on {f['counterparty']}: {f['ask']}{weeks}")
        if f.get("web_link"):
            lines.append(f"      {f['web_link']}")

    # Computed sections
    today = week_end.date()
    buckets = _bucket_by_due(agenda.get("action_items", []), today)
    bucket_order = ("this week", "this month", "this quarter", "later")
    coming_up_lines: list[str] = []
    for label in bucket_order:
        items = buckets.get(label, [])
        if not items:
            continue
        coming_up_lines.append(f"  {label.upper()}")
        for a in items:
            due = a.get("due", "") or a.get("due_date", "")
            coming_up_lines.append(f"    • {a.get('task', '')}  ({due}, {a.get('owner', '?')})")
    if coming_up_lines:
        lines += ["", "COMING UP (by due date)", *coming_up_lines]

    rollup = _counterparty_rollup(agenda)
    if rollup:
        lines += ["", "BY COUNTERPARTY"]
        for cp, b in sorted(rollup.items(), key=lambda kv: -(len(kv[1]["owed_to_you"]) + len(kv[1]["owed_by_you"]))):
            lines.append(
                f"  • {cp} — {len(b['owed_to_you'])} waiting on them, "
                f"{len(b['owed_by_you'])} you owe"
            )

    lines += ["", "FYI"]
    for x in agenda.get("fyi", []):
        lines.append(f"  • {x}")
    return "\n".join(lines)


def render_briefs_html(briefs: list[dict[str, Any]], when: datetime, web_ui_url: str = "", web_ui_token: str = "") -> str:
    """HTML render for the daily pre-meeting briefs email."""
    if not briefs:
        return (
            "<!doctype html><html><body>"
            "<p>No meetings on the calendar today.</p>"
            "</body></html>"
        )

    def _brief_block(b: dict) -> str:
        title = _linked(
            f"<strong>{escape(b.get('meeting_subject', ''))}</strong>",
            b.get("meeting_web_link", ""),
        )
        when_str = escape(b.get("meeting_time", ""))
        rows = []
        if b.get("last_commitments"):
            rows.append(
                f"<div><em style='color:#666'>Last commitments:</em> "
                f"{escape(b['last_commitments'])}</div>"
            )
        if b.get("open_asks"):
            rows.append(
                f"<div><em style='color:#666'>Open asks:</em> "
                f"{escape(b['open_asks'])}</div>"
            )
        if b.get("context"):
            rows.append(
                f"<div><em style='color:#666'>Context:</em> "
                f"{escape(b['context'])}</div>"
            )
        body = "".join(rows) or "<div style='color:#888'>(no recent context)</div>"
        return (
            f"<div style='margin:18px 0;padding-bottom:12px;border-bottom:1px solid #eee'>"
            f"<div style='font-size:15px'>{title}</div>"
            f"<div style='color:#666;font-size:12px;margin-bottom:6px'>{when_str}</div>"
            f"<div style='font-size:13px;line-height:1.55'>{body}</div>"
            f"</div>"
        )

    body_html = "".join(_brief_block(b) for b in briefs)
    iso = when.isocalendar()
    week_iso = f"{iso.year:04d}-W{iso.week:02d}"
    banner = _dashboard_banner(web_ui_url, web_ui_token, week_iso, "monday")
    return f"""\
<!doctype html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:680px;margin:auto;padding:24px;color:#222">
  {banner}
  <h1 style="margin-bottom:4px">Today's Briefs</h1>
  <p style="color:#666;margin-top:0">{when.date()} — {len(briefs)} meeting{"s" if len(briefs) != 1 else ""}</p>
  {body_html}
  <hr style="margin-top:32px;border:none;border-top:1px solid #eee">
  <p style="color:#aaa;font-size:11px">Generated by EMIS from the last 4 weeks of mail with each attendee.</p>
</body></html>
"""


def render_briefs_text(briefs: list[dict[str, Any]], when: datetime) -> str:
    """Plain-text render for the briefs email."""
    if not briefs:
        return f"TODAY'S BRIEFS — {when.date()}\n\n(no meetings on the calendar today)\n"
    lines = [
        f"TODAY'S BRIEFS — {when.date()}",
        f"{len(briefs)} meeting{'s' if len(briefs) != 1 else ''}",
        "",
    ]
    for b in briefs:
        lines.append(f"• {b.get('meeting_subject', '')}")
        lines.append(f"  {b.get('meeting_time', '')}")
        if b.get("last_commitments"):
            lines.append(f"  Last commitments: {b['last_commitments']}")
        if b.get("open_asks"):
            lines.append(f"  Open asks: {b['open_asks']}")
        if b.get("context"):
            lines.append(f"  Context: {b['context']}")
        if b.get("meeting_web_link"):
            lines.append(f"  {b['meeting_web_link']}")
        lines.append("")
    return "\n".join(lines)


def send_via_ses(
    *,
    sender: str,
    recipient: str,
    subject: str,
    html: str,
    text: str,
) -> dict[str, Any]:
    client = boto3.client("ses")
    response = client.send_email(
        Source=sender,
        Destination={"ToAddresses": [recipient]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {
                "Text": {"Data": text, "Charset": "UTF-8"},
                "Html": {"Data": html, "Charset": "UTF-8"},
            },
        },
    )
    logger.info("SES MessageId=%s", response["MessageId"])
    return response
