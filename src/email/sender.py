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
    """Build per-counterparty buckets of {owed_to_you, owed_by_you, joint}.

    - follow_ups → things owed to you
    - promises_made → things you owe them
    - action_items with non-self owner → things they owe you
    """
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
        f"style='color:inherit;text-decoration:underline;text-decoration-color:#aaa'>"
        f"{title_html}</a>"
    )


def render_html(agenda: dict[str, Any], week_start: datetime, week_end: datetime, mode: str = "monday") -> str:
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
        title = _linked(f"<strong>{escape(a['task'])}</strong>", a.get("web_link", ""))
        src = f" <em style='color:#666'>({escape(a['source_subject'])})</em>" if a.get("source_subject") else ""
        return (
            f"{status_badge(a.get('status', 'new'))} {title} "
            f"<span style='color:#666;font-size:12px'>"
            f"({escape(a.get('owner', '?'))}, due {escape(a.get('due', ''))}, "
            f"{urgency_badge(a.get('urgency', 'medium'))})</span>{src}"
        )

    def _followup_li(f: dict) -> str:
        title = _linked(f"<strong>{escape(f['thread'])}</strong>", f.get("web_link", ""))
        weeks = f" <span style='color:#888'>(open {f['weeks_open']}w)</span>" if f.get("weeks_open") else ""
        return f"{status_badge(f.get('status', 'new'))} {title} — waiting on {escape(f['counterparty'])}: {escape(f['ask'])}{weeks}"

    def _promise_li(p: dict) -> str:
        title = _linked(f"<strong>{escape(p['commitment'])}</strong>", p.get("web_link", ""))
        return f"{title} <span style='color:#666'>to {escape(p['to'])} by {escape(p['by'])}</span>"

    priorities = ul([_priority_li(p) for p in agenda.get("priorities", [])])
    meetings = ul([_meeting_li(m) for m in agenda.get("meetings", [])])
    actions = ul([_action_li(a) for a in agenda.get("action_items", [])])
    follow_ups = ul([_followup_li(f) for f in agenda.get("follow_ups", [])])
    promises = ul([_promise_li(p) for p in agenda.get("promises_made", [])])
    fyi = ul([escape(x) for x in agenda.get("fyi", [])])

    body_html = "".join([
        section("Priorities", priorities),
        section("Meetings", meetings),
        section("Action items", actions),
        section("Follow-ups (waiting on others)", follow_ups),
    ])
    if agenda.get("promises_made"):
        body_html += section("Promises you made", promises)

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

    return f"""\
<!doctype html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:680px;margin:auto;padding:24px;color:#222">
  <h1 style="margin-bottom:4px">{escape(title)}</h1>
  <p style="color:#666;margin-top:0">{week_start.date()} – {week_end.date()}</p>
  <p style="font-size:15px;line-height:1.55">{escape(agenda.get('week_summary', ''))}</p>
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

    lines = [
        title,
        f"{week_start.date()} – {week_end.date()}",
        "",
        agenda.get("week_summary", ""),
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
        src = f" ({a['source_subject']})" if a.get("source_subject") else ""
        lines.append(
            f"  {mark} [{a.get('owner', '?')}, due {a.get('due', '')}, "
            f"{a.get('urgency', 'medium')}, {a.get('status', 'new')}] {a['task']}{src}"
        )
        if a.get("web_link"):
            lines.append(f"      {a['web_link']}")

    lines += ["", "FOLLOW-UPS"]
    for f in agenda.get("follow_ups", []):
        mark = _STATUS_MARK.get(f.get("status", "new"), "•")
        weeks = f" (open {f['weeks_open']}w)" if f.get("weeks_open") else ""
        lines.append(f"  {mark} {f['thread']} — waiting on {f['counterparty']}: {f['ask']}{weeks}")
        if f.get("web_link"):
            lines.append(f"      {f['web_link']}")

    if agenda.get("promises_made"):
        lines += ["", "PROMISES MADE"]
        for p in agenda["promises_made"]:
            lines.append(f"  • {p['commitment']} → {p['to']} by {p['by']}")
            if p.get("web_link"):
                lines.append(f"      {p['web_link']}")

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


def render_briefs_html(briefs: list[dict[str, Any]], when: datetime) -> str:
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
    return f"""\
<!doctype html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:680px;margin:auto;padding:24px;color:#222">
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
