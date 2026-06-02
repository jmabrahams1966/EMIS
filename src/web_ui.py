"""Lightweight web UI for browsing past EMIS agendas.

Deployed as a separate Lambda behind a Function URL. Single route:

    GET /?token=…                 → index (list of weeks + modes)
    GET /?token=…&week=2026-W22   → expanded view of that week's modes
    GET /?token=…&week=…&mode=monday  → full agenda for that week+mode

Auth: a single shared-secret token compared against ``WEB_UI_TOKEN`` (env
var) using a constant-time compare. The token can be supplied either as
``Authorization: Bearer …`` (preferred — doesn't leak into CloudWatch
access logs or browser history) or as the ``token`` query string parameter
(for browser navigation). Responses set ``Referrer-Policy: no-referrer`` so
the token doesn't leak via the Referer header when the user clicks an
outbound link.

For single-user personal use this is adequate; not appropriate for
multi-tenant deployment.

State source: the S3 bucket EMIS writes to. The Lambda needs read-only S3
access to ``runs/`` under that bucket.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from datetime import datetime
from html import escape
from typing import Any
from urllib.parse import urlencode

import boto3

from .agenda.memory import load_prior_agendas
from .email.dashboard import render_dashboard_html
from .snooze import load_closures
from .state import store

logger = logging.getLogger("emis.web_ui")


def _resp(body: str, status: int = 200, content_type: str = "text/html; charset=utf-8") -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": content_type,
            "Cache-Control": "private, no-store",
            "Referrer-Policy": "no-referrer",
        },
        "body": body,
    }


def _extract_token(event: dict[str, Any]) -> str:
    """Prefer ``Authorization: Bearer …``; fall back to ``?token=`` for browser use."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization", "") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    qs = (event.get("queryStringParameters") or {}) or {}
    return qs.get("token", "") or ""


def _check_auth(event: dict[str, Any]) -> str | None:
    expected = os.getenv("WEB_UI_TOKEN") or ""
    if not expected:
        return "Server misconfigured: WEB_UI_TOKEN is not set."
    given = _extract_token(event)
    if not given or not hmac.compare_digest(given, expected):
        return "Unauthorized."
    return None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    err = _check_auth(event)
    if err is not None:
        return _resp(_chrome("EMIS", f"<p>{escape(err)}</p>"), status=403)

    bucket = os.environ["STATE_BUCKET"]
    qs = (event.get("queryStringParameters") or {}) or {}
    week = qs.get("week")
    mode = qs.get("mode")
    view = qs.get("view", "")
    link_token = qs.get("token", "") or ""

    if week and mode:
        agenda = store.load_agenda(bucket, week, mode)
        if not agenda:
            return _resp(_chrome("Not found", "<p>No agenda for that week / mode.</p>"), status=404)
        return _resp(_render_agenda_page(bucket, week, mode, agenda, link_token))

    if week:
        return _resp(_render_week_page(bucket, week, link_token))

    # Default landing: jump straight to the latest week's Monday agenda so
    # opening the bookmark mid-week shows current state rather than a list.
    # Pass `&view=list` to override and see the index instead.
    if view != "list":
        latest = _find_latest_agenda(bucket)
        if latest is not None:
            week_iso, mode_name, agenda = latest
            return _resp(_render_agenda_page(bucket, week_iso, mode_name, agenda, link_token))

    return _resp(_render_index_page(bucket, link_token))


def _find_latest_agenda(bucket: str) -> tuple[str, str, dict] | None:
    """Return (week_iso, mode, agenda) for the most recent stored agenda.

    Preference order: most recent week with Monday agenda (canonical); fall
    back to whatever mode exists for that week. Returns None if no agendas
    are persisted yet.
    """
    weeks = store.list_weeks(bucket, limit=1)
    if not weeks:
        return None
    week_iso = weeks[0]
    for mode_name in ("monday", "wednesday", "friday"):
        agenda = store.load_agenda(bucket, week_iso, mode_name)
        if agenda:
            return (week_iso, mode_name, agenda)
    return None


# ── Rendering ──────────────────────────────────────────────────────────────

def _chrome(title: str, body: str) -> str:
    return f"""\
<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} — EMIS</title>
<style>
  body {{ font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif; max-width: 720px;
          margin: 0 auto; padding: 24px; color: #222; line-height: 1.5; }}
  h1 {{ margin-bottom: 4px; }}
  a {{ color: #2c6cdf; text-decoration: none; }} a:hover {{ text-decoration: underline; }}
  .row {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #eee; }}
  .row a {{ font-weight: 600; }}
  .modes {{ color: #888; font-size: 13px; }}
  .nav {{ margin-bottom: 24px; }}
</style></head><body>
{body}
</body></html>
"""


def _render_index_page(bucket: str, token: str) -> str:
    weeks = store.list_weeks(bucket, limit=52)
    if not weeks:
        return _chrome("EMIS", "<h1>EMIS</h1><p>No agendas yet.</p>")

    s3 = boto3.client("s3")
    rows: list[str] = []
    for w in weeks:
        modes = _modes_for_week(s3, bucket, w)
        modes_str = ", ".join(modes) if modes else "—"
        rows.append(
            f"<div class='row'>"
            f"<a href='?{urlencode({'token': token, 'week': w})}'>Week {escape(w)}</a>"
            f"<span class='modes'>{escape(modes_str)}</span>"
            f"</div>"
        )
    body = (
        "<h1>EMIS</h1><p>Weekly agendas, newest first.</p>"
        + "\n".join(rows)
    )
    return _chrome("EMIS", body)


def _render_week_page(bucket: str, week: str, token: str) -> str:
    s3 = boto3.client("s3")
    modes = _modes_for_week(s3, bucket, week)
    if not modes:
        return _chrome(week, f"<h1>{escape(week)}</h1><p>No modes recorded.</p>")
    links = "<ul>" + "".join(
        f"<li><a href='?{urlencode({'token': token, 'week': week, 'mode': m})}'>{escape(m).title()}</a></li>"
        for m in modes
    ) + "</ul>"
    nav = f"<div class='nav'><a href='?{urlencode({'token': token})}'>← all weeks</a></div>"
    return _chrome(week, nav + f"<h1>Week {escape(week)}</h1>" + links)


def _render_agenda_page(bucket: str, week: str, mode: str, agenda: dict, token: str) -> str:
    """Serve the interactive dashboard rendering of a stored agenda."""
    try:
        year, w = week.split("-W")
        week_start = datetime.fromisocalendar(int(year), int(w), 1)
        week_end = datetime.fromisocalendar(int(year), int(w), 7)
    except Exception:
        week_start = week_end = datetime.utcnow()
    # Pull closures + prior agendas so Backlog and History tabs are populated.
    try:
        closures = load_closures(bucket)
        closures_dict = {
            "snoozes": [s.to_dict() for s in closures.snoozes],
            "done": [d.to_dict() for d in closures.done],
            "drops": [d.to_dict() for d in closures.drops],
        }
    except Exception as exc:
        logger.warning("loading closures failed: %s", exc)
        closures_dict = None
    try:
        prior = load_prior_agendas(bucket, week_end)
    except Exception as exc:
        logger.warning("loading prior agendas failed: %s", exc)
        prior = []
    return render_dashboard_html(
        agenda, week_start, week_end, mode=mode,
        closures=closures_dict, prior_agendas=prior,
    )


def _modes_for_week(s3, bucket: str, week: str) -> list[str]:
    prefix = f"runs/{week}/"
    seen: set[str] = set()
    try:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                name = obj["Key"].split("/")[-1]
                if name == "agenda.json":
                    seen.add("monday")
                elif name.startswith("agenda.") and name.endswith(".json"):
                    seen.add(name[len("agenda."):-len(".json")])
    except Exception as exc:
        logger.warning("S3 list failed for %s: %s", prefix, exc)
    return sorted(seen)
