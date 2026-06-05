"""Weekly admin digest — Saturday morning operational summary.

Triggered by EventBridge once a week. Builds a single HTML email summarizing
the past 7 days for every admin user:

  - audit event counts by type
  - per-user runs + cost + errors
  - users currently in needs_reauth / paused / removed state
  - any users at >80% of their monthly cost cap

Sent via SES to every user where ``role=="admin"`` (so multiple admins all
get the same digest). Best-effort; SES failures log a warning but don't
crash, and per-admin sends are independent (one failure doesn't block the
others).
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

import boto3

from .audit import list_recent
from .email.sender import send_via_ses
from .telemetry import (
    current_month_cost_for_user, load_runs, summarize_per_user,
)
from .users import list_active_users

logger = logging.getLogger("emis.digest")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    bucket = os.environ["STATE_BUCKET"]
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=7)

    # ── Gather data ────────────────────────────────────────────────────
    users = _scan_all_users()
    admins = [u for u in users if u.get("role") == "admin"]
    if not admins:
        logger.info("digest: no admin users; nothing to send")
        return {"status": "no_admins"}

    runs = load_runs(bucket)
    per_user_cost = summarize_per_user(runs, days=7)
    audit_events = list_recent(days=7, limit=1000)
    event_counts = Counter(e.get("event", "") for e in audit_events)

    needs_reauth = [u for u in users if u.get("status") == "needs_reauth"]
    paused = [u for u in users if u.get("status") == "paused"]
    cap_warnings = []
    for u in users:
        cap = int(u.get("monthly_cost_cap_usd") or 0)
        if cap <= 0:
            continue
        spend = current_month_cost_for_user(runs, u.get("user_id", ""), now)
        if spend >= 0.8 * cap:
            cap_warnings.append({
                "email": u.get("email", ""),
                "spend": spend, "cap": cap,
                "pct": int(100 * spend / cap),
            })

    # ── Render ─────────────────────────────────────────────────────────
    html, text = _render(
        users=users, admins=admins, runs=runs, per_user_cost=per_user_cost,
        event_counts=event_counts, needs_reauth=needs_reauth, paused=paused,
        cap_warnings=cap_warnings, since=since, now=now,
    )

    # ── Send to every admin ────────────────────────────────────────────
    sent: list[dict] = []
    failed: list[dict] = []
    sender = os.environ["AGENDA_SENDER"]
    subject = f"EMIS weekly admin digest — {since.date()} – {now.date()}"
    for a in admins:
        recipient = a.get("email", "")
        if not recipient:
            continue
        try:
            send_via_ses(sender=sender, recipient=recipient,
                         subject=subject, html=html, text=text)
            sent.append({"email": recipient})
        except Exception as exc:
            logger.warning("digest send to %s failed: %s", recipient, exc)
            failed.append({"email": recipient, "error": str(exc)})

    return {
        "status": "sent" if sent else "failed",
        "sent": len(sent), "failed": len(failed),
        "details": {"sent": sent, "failed": failed},
    }


def _scan_all_users() -> list[dict[str, Any]]:
    table = boto3.resource("dynamodb").Table(os.environ["USERS_TABLE"])
    out: list[dict[str, Any]] = []
    paginator = table.meta.client.get_paginator("scan")
    for page in paginator.paginate(TableName=os.environ["USERS_TABLE"]):
        out.extend(page.get("Items", []))
    return out


def _render(*, users, admins, runs, per_user_cost, event_counts,
            needs_reauth, paused, cap_warnings, since, now) -> tuple[str, str]:
    total_runs = sum(s["runs"] for s in per_user_cost.values())
    total_cost = round(sum(s["cost_usd"] for s in per_user_cost.values()), 2)
    total_errors = sum(s["errors"] for s in per_user_cost.values())

    # Per-user table
    user_rows: list[str] = []
    by_uid = {u.get("user_id", ""): u for u in users}
    for uid, stats in sorted(per_user_cost.items(), key=lambda kv: -kv[1]["cost_usd"]):
        u = by_uid.get(uid, {})
        email = u.get("email", "") or "(system)"
        user_rows.append(
            f"<tr><td style='padding:6px 8px'>{escape(email)}</td>"
            f"<td style='padding:6px 8px;text-align:right'>{stats['runs']}</td>"
            f"<td style='padding:6px 8px;text-align:right'>${stats['cost_usd']:.2f}</td>"
            f"<td style='padding:6px 8px;text-align:right;color:{'#b04141' if stats['errors'] else '#888'}'>{stats['errors']}</td>"
            f"</tr>"
        )

    # Events
    event_rows = "".join(
        f"<li>{escape(ev)}: <strong>{n}</strong></li>"
        for ev, n in event_counts.most_common()
    ) or "<li style='color:#888'>No events this week.</li>"

    # Issues block
    issues_html = ""
    if needs_reauth:
        issues_html += (
            f"<p style='color:#b04141'><strong>{len(needs_reauth)} need re-auth:</strong> "
            + ", ".join(escape(u.get("email", "")) for u in needs_reauth)
            + "</p>"
        )
    if paused:
        issues_html += (
            f"<p style='color:#a55a00'><strong>{len(paused)} paused:</strong> "
            + ", ".join(escape(u.get("email", "")) for u in paused)
            + "</p>"
        )
    if cap_warnings:
        warn_lines = "".join(
            f"<li>{escape(w['email'])}: ${w['spend']:.2f} / ${w['cap']} "
            f"({w['pct']}%)</li>"
            for w in cap_warnings
        )
        issues_html += (
            f"<p><strong>{len(cap_warnings)} near cost cap:</strong></p>"
            f"<ul>{warn_lines}</ul>"
        )
    if not issues_html:
        issues_html = "<p style='color:#1b5e20'>✓ No issues to flag.</p>"

    html = f"""\
<!doctype html><html><body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:720px;margin:auto;padding:24px;color:#222">
  <h1 style="margin-bottom:4px">EMIS — Weekly admin digest</h1>
  <p style="color:#666;margin-top:0">{since.date()} – {now.date()}</p>

  <h2 style="font-size:15px;margin-top:24px">Headline</h2>
  <ul>
    <li><strong>{len(users)}</strong> enrolled · <strong>{len(admins)}</strong> admin</li>
    <li><strong>{total_runs}</strong> agenda runs this week</li>
    <li><strong>${total_cost:.2f}</strong> Bedrock spend · <strong style='color:{"#b04141" if total_errors else "#1b5e20"}'>{total_errors}</strong> errors</li>
  </ul>

  <h2 style="font-size:15px;margin-top:24px">Operational issues</h2>
  {issues_html}

  <h2 style="font-size:15px;margin-top:24px">Per-user activity (7d)</h2>
  <table style="border-collapse:collapse;width:100%;font-size:13px">
    <thead style="background:#f7faff">
      <tr>
        <th style="text-align:left;padding:6px 8px">User</th>
        <th style="text-align:right;padding:6px 8px">Runs</th>
        <th style="text-align:right;padding:6px 8px">Cost</th>
        <th style="text-align:right;padding:6px 8px">Errors</th>
      </tr>
    </thead>
    <tbody>
      {"".join(user_rows) or '<tr><td colspan=4 style="padding:12px;color:#aaa">No runs.</td></tr>'}
    </tbody>
  </table>

  <h2 style="font-size:15px;margin-top:24px">Audit events</h2>
  <ul>{event_rows}</ul>

  <hr style="margin-top:32px;border:none;border-top:1px solid #eee">
  <p style="color:#aaa;font-size:11px">Generated by EMIS. Full audit log at the admin dashboard.</p>
</body></html>"""

    text_lines = [
        f"EMIS Weekly Admin Digest — {since.date()} to {now.date()}",
        "",
        f"Users: {len(users)} enrolled, {len(admins)} admin",
        f"Activity: {total_runs} runs, ${total_cost:.2f}, {total_errors} errors",
        "",
        "Issues:",
    ]
    if needs_reauth:
        text_lines.append(f"  {len(needs_reauth)} need re-auth: "
                          + ", ".join(u.get("email", "") for u in needs_reauth))
    if paused:
        text_lines.append(f"  {len(paused)} paused: "
                          + ", ".join(u.get("email", "") for u in paused))
    if cap_warnings:
        text_lines.append(f"  {len(cap_warnings)} near cost cap")
    if not (needs_reauth or paused or cap_warnings):
        text_lines.append("  None")
    text_lines += ["", "Per-user activity:"]
    for uid, stats in sorted(per_user_cost.items(), key=lambda kv: -kv[1]["cost_usd"]):
        u = by_uid.get(uid, {})
        text_lines.append(
            f"  {u.get('email', '(system)')}: {stats['runs']} runs, "
            f"${stats['cost_usd']:.2f}, {stats['errors']} err"
        )
    text_lines += ["", "Audit events:"]
    for ev, n in event_counts.most_common():
        text_lines.append(f"  {ev}: {n}")
    text = "\n".join(text_lines)

    return html, text
