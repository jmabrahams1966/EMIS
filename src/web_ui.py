"""Web UI for browsing EMIS agendas — Phase 2 multi-tenant.

Routes:

    GET  /                            → dashboard (latest week, signed-in user)
    GET  /?week=…&mode=…              → specific archived agenda
    GET  /login                       → redirect to Microsoft authorize
    GET  /auth/callback?code=…&state=… → finish OAuth, set session cookie
    GET  /logout                      → clear session cookie
    POST / (JSON body)                → closure/snooze/note/pin actions

Auth (Phase 2):
- Microsoft OAuth PKCE flow against the same Azure app the agenda Lambda uses.
- After successful sign-in, the browser holds a 7-day HMAC-signed session
  cookie (``emis_session``) containing the user_id. All reads + writes scope
  to ``users/{user_id}/`` in S3.
- Legacy compatibility: if a request arrives with ``?token=<WEB_UI_TOKEN>``
  AND no session cookie, fall back to the Phase 1 stopgap user_id from
  ``EMIS_DEFAULT_USER_ID``. This keeps existing email banner links working
  until the next agenda generates with the new banner format.

State source: the S3 bucket EMIS writes to. The Lambda needs read access to
``users/`` and write access for ``users/*/state/*``.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import secrets as _secrets
from datetime import datetime, timezone
from html import escape
from typing import Any
from urllib.parse import urlencode

import boto3
import httpx

from .agenda.memory import load_prior_agendas
from .email.dashboard import render_dashboard_html
from . import session as session_mod
from . import audit as audit_mod
from .snooze import (
    DoneRecord, DropRecord, SnoozeRecord,
    load_closures, save_closures,
)
from .state import store
from .state.store import (
    load_notes, save_notes, load_pins, save_pins,
)
from .users import load_user, list_active_users, update_settings, delete_user

logger = logging.getLogger("emis.web_ui")

AUTHORIZE_BASE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
TOKEN_BASE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
GRAPH_ME = "https://graph.microsoft.com/v1.0/me"
# Request the same scopes as enrollment so /login re-auth produces a usable
# Graph refresh token in addition to identity claims. Subsequent sign-ins
# don't re-prompt for consent once scopes are granted.
LOGIN_SCOPES = (
    "openid profile email offline_access "
    "Mail.Read Mail.ReadWrite "
    "Calendars.Read "
    "Tasks.ReadWrite Files.ReadWrite "
    "MailboxSettings.Read"
)


def _graph_secret() -> dict[str, str]:
    """Return ``{client_id, tenant_id, ...}`` from emis/graph."""
    sid = os.environ["GRAPH_SECRET_ID"]
    sm = boto3.client("secretsmanager")
    resp = sm.get_secret_value(SecretId=sid)
    return json.loads(resp.get("SecretString") or "{}")


def _resp(
    body: str,
    status: int = 200,
    content_type: str = "text/html; charset=utf-8",
    extra_cookies: list[str] | None = None,
) -> dict[str, Any]:
    headers = {
        "Content-Type": content_type,
        "Cache-Control": "private, no-store",
        "Referrer-Policy": "no-referrer",
    }
    out: dict[str, Any] = {"statusCode": status, "headers": headers, "body": body}
    if extra_cookies:
        # Lambda Function URLs honor a top-level "cookies" array for
        # Set-Cookie. (multiValueHeaders is used by some gateways but not
        # Function URLs.)
        out["cookies"] = list(extra_cookies)
    return out


def _redirect(url: str, extra_cookies: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "statusCode": 302,
        "headers": {"Location": url, "Cache-Control": "no-store"},
        "body": "",
    }
    if extra_cookies:
        out["cookies"] = list(extra_cookies)
    return out


def _self_origin(event: dict[str, Any]) -> str:
    """Reconstruct ``https://<host>`` from request headers."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    host = headers.get("host", "")
    proto = headers.get("x-forwarded-proto", "https")
    return f"{proto}://{host}"


def _legacy_token_ok(event: dict[str, Any]) -> bool:
    """Phase 1 fallback: was the request authorized by the shared token?"""
    expected = os.getenv("WEB_UI_TOKEN") or ""
    if not expected:
        return False
    qs = (event.get("queryStringParameters") or {}) or {}
    given = qs.get("token", "") or ""
    if not given:
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        auth = headers.get("authorization", "") or ""
        if auth.lower().startswith("bearer "):
            given = auth[7:].strip()
    return bool(given) and hmac.compare_digest(given, expected)


def _resolve_user_id(event: dict[str, Any]) -> str | None:
    """Return the user_id for this request, or None if unauthenticated.

    Resolution order:
      1. Valid ``emis_session`` cookie (Phase 2 SSO).
      2. Legacy ``?token=<WEB_UI_TOKEN>`` + ``EMIS_DEFAULT_USER_ID`` env var.
    """
    uid = session_mod.session_user_id(event)
    if uid:
        return uid
    if _legacy_token_ok(event):
        return os.getenv("EMIS_DEFAULT_USER_ID", "") or None
    return None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    bucket = os.environ["STATE_BUCKET"]
    method = (
        (event.get("requestContext") or {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    ).upper()
    path = (
        (event.get("requestContext") or {}).get("http", {}).get("path")
        or event.get("rawPath") or "/"
    )

    # ── OAuth routes (no session required) ──────────────────────────────
    if method == "GET" and path == "/login":
        return _handle_login(event)
    if method == "GET" and path == "/auth/callback":
        return _handle_auth_callback(event, bucket)
    if method == "GET" and path == "/logout":
        return _redirect("/login", extra_cookies=[session_mod.clear_session_cookie()])

    # ── Everything else requires identity ───────────────────────────────
    user_id = _resolve_user_id(event)
    if user_id is None:
        if method == "GET":
            return _redirect("/login")
        return _resp('{"error":"unauthorized"}', status=401, content_type="application/json")

    # ── Settings (per-user preferences) ─────────────────────────────────
    if path == "/settings":
        if method == "GET":
            saved = bool((event.get("queryStringParameters") or {}).get("saved"))
            return _handle_settings_get(user_id, saved=saved)
        if method == "POST":
            return _handle_settings_post(event, user_id)
    if path == "/settings/unenroll" and method == "POST":
        return _handle_unenroll(event, user_id)

    # ── Admin (gated on role==admin) ────────────────────────────────────
    if path.startswith("/admin"):
        viewer = load_user(user_id)
        if viewer is None or viewer.role != "admin":
            return _resp(_chrome("EMIS — admin",
                "<h1>Admin only</h1><p>Your account doesn't have admin role.</p>"
            ), status=403)
        if method == "GET" and path == "/admin":
            return _handle_admin_index()
        if method == "GET" and path == "/admin/audit":
            return _handle_admin_audit()
        if method == "POST" and path == "/admin/action":
            return _handle_admin_action(event, viewer)

    if method == "POST":
        return _handle_closure_post(event, bucket, user_id)

    qs = (event.get("queryStringParameters") or {}) or {}
    week = qs.get("week")
    mode = qs.get("mode")
    view = qs.get("view", "")
    # The dashboard JS POSTs with whatever token the page was loaded with.
    # When SSO is in use we don't need a token in the JS — session cookie is
    # automatically sent. Pass the cookie marker so the dashboard JS knows
    # to omit the token query param.
    link_token = qs.get("token", "") or "session"

    if week and mode:
        agenda = store.load_agenda(bucket, week, mode, user_id=user_id)
        if not agenda:
            return _resp(_chrome("Not found", "<p>No agenda for that week / mode.</p>"), status=404)
        return _resp(_render_agenda_page(bucket, week, mode, agenda, link_token, user_id))

    if week:
        return _resp(_render_week_page(bucket, week, link_token, user_id))

    if view != "list":
        latest = _find_latest_agenda(bucket, user_id)
        if latest is not None:
            week_iso, mode_name, agenda = latest
            return _resp(_render_agenda_page(bucket, week_iso, mode_name, agenda, link_token, user_id))

    return _resp(_render_index_page(bucket, link_token, user_id))


# ── OAuth handlers ──────────────────────────────────────────────────────

def _handle_login(event: dict[str, Any]) -> dict[str, Any]:
    secret = _graph_secret()
    tenant_id = secret.get("tenant_id", "common")
    client_id = secret["client_id"]
    redirect_uri = f"{_self_origin(event)}/auth/callback"

    challenge, nonce, cookie = session_mod.make_login_state()
    qs = urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": LOGIN_SCOPES,
        "state": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    })
    return _redirect(
        AUTHORIZE_BASE.format(tenant=tenant_id) + "?" + qs,
        extra_cookies=[cookie],
    )


def _handle_auth_callback(event: dict[str, Any], bucket: str) -> dict[str, Any]:
    qs = (event.get("queryStringParameters") or {}) or {}
    code = qs.get("code")
    state_nonce = qs.get("state", "") or ""
    if not code:
        err = qs.get("error_description") or qs.get("error") or "missing code"
        return _resp(_chrome("Sign-in failed", f"<p>{escape(err)}</p>"), status=400)

    verifier = session_mod.consume_login_state(event, state_nonce)
    if not verifier:
        return _resp(_chrome("Sign-in failed",
            "<p>Login state expired or was tampered with. "
            "<a href='/login'>Try again</a>.</p>"
        ), status=400)

    secret = _graph_secret()
    tenant_id = secret.get("tenant_id", "common")
    client_id = secret["client_id"]
    client_secret = secret.get("client_secret", "")
    redirect_uri = f"{_self_origin(event)}/auth/callback"

    # The Azure app is registered as a "Web" platform, so Microsoft requires
    # the client_secret on the token endpoint even though we're using PKCE.
    # If the redirect URIs are later moved to "Single-page application", the
    # client_secret can be dropped (PKCE alone suffices there).
    form_data = {
        "client_id": client_id,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "scope": LOGIN_SCOPES,
        "code_verifier": verifier,
    }
    if client_secret:
        form_data["client_secret"] = client_secret

    try:
        token_resp = httpx.post(
            TOKEN_BASE.format(tenant=tenant_id),
            data=form_data,
            timeout=30,
        )
        if token_resp.status_code >= 400:
            # Surface Azure's actual error code so we can diagnose. The
            # body is JSON like {"error":"invalid_client","error_description":"..."}
            logger.error(
                "Azure token endpoint %d body=%s",
                token_resp.status_code, token_resp.text[:800],
            )
            err_json = {}
            try:
                err_json = token_resp.json()
            except Exception:
                pass
            azure_err = err_json.get("error", "unknown")
            azure_desc = err_json.get("error_description", token_resp.text[:300])
            return _resp(_chrome("Sign-in failed",
                f"<h1>Azure token exchange failed</h1>"
                f"<p><strong>{escape(azure_err)}</strong></p>"
                f"<pre style='white-space:pre-wrap;background:#f5f5f5;padding:12px;border-radius:6px;font-size:12px'>"
                f"{escape(azure_desc)}</pre>"
                f"<p><a href='/login'>Try again</a></p>"
            ), status=500)
        tokens = token_resp.json()
        access_token = tokens["access_token"]
        new_refresh = tokens.get("refresh_token", "")
        me = httpx.get(
            GRAPH_ME,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        me.raise_for_status()
        me_data = me.json()
    except Exception as exc:
        logger.exception("OAuth token exchange failed")
        return _resp(_chrome("Sign-in failed",
            f"<p>{escape(str(exc))}</p><p><a href='/login'>Try again</a></p>"
        ), status=500)

    user_id = me_data.get("id") or ""
    email = me_data.get("mail") or me_data.get("userPrincipalName") or ""
    if not user_id:
        return _resp(_chrome("Sign-in failed", "<p>No object_id on /me.</p>"), status=500)

    user = load_user(user_id)
    if user is None:
        return _resp(_chrome("Not enrolled",
            f"<h1>Not enrolled</h1>"
            f"<p>{escape(email)} is signed in to Microsoft 365, but isn't "
            f"enrolled in EMIS. Ask your admin for the enrollment link.</p>"
        ), status=403)
    # SSO returned a fresh refresh_token with full Graph scopes — store it so
    # the agenda Lambda picks up the new token on its next run. This is also
    # how needs_reauth users get back to active state.
    if new_refresh:
        try:
            from .users import update_refresh_token as _update_token
            _update_token(user_id, new_refresh)
        except Exception as exc:
            logger.warning("storing refreshed token failed: %s", exc)
    if user.status == "needs_reauth":
        update_settings(user_id, status="active")
        logger.info("user %s reactivated via SSO", user_id)
    elif user.status != "active":
        return _resp(_chrome("Account paused",
            f"<h1>Account paused</h1>"
            f"<p>{escape(email)}'s EMIS account is currently <strong>{escape(user.status)}</strong>. "
            f"Ask an admin to resume it.</p>"
        ), status=403)

    session_cookie = session_mod.make_session_cookie(user_id)
    clear_state = session_mod.clear_login_state_cookie()
    logger.info("SSO sign-in: user_id=%s email=%s", user_id, email)
    audit_mod.record_event(
        "login_success", user_id=user_id, email=email, request=event,
    )
    return _redirect("/", extra_cookies=[session_cookie, clear_state])


_CLOSURE_ACTIONS = {"done", "drop", "snooze"}
_OTHER_ACTIONS = {"undo", "set_note", "pin", "unpin"}


# ── Settings page ──────────────────────────────────────────────────────

_ALL_CHANNELS = ("email", "sms")
_ALL_MODES = ("monday", "wednesday", "friday", "morning")
_ALL_CATS = ("clinical", "business", "admin", "personal")


def _settings_chrome(body: str, saved: bool = False) -> str:
    msg = (
        "<p style='color:#1b5e20;background:#e8f5e9;padding:8px 12px;"
        "border-radius:6px;margin:0 0 16px'>✓ Saved</p>"
        if saved else ""
    )
    return _chrome("Settings", f"""\
<div style='display:flex;justify-content:space-between;align-items:center'>
  <h1 style='margin:0'>Settings</h1>
  <div style='font-size:13px'><a href='/'>← dashboard</a> · <a href='/logout'>sign out</a></div>
</div>
{msg}
{body}
""")


def _handle_settings_get(user_id: str, saved: bool = False) -> dict[str, Any]:
    user = load_user(user_id)
    if user is None:
        return _resp(_chrome("Not found", "<p>User record missing.</p>"), status=404)

    def _checkbox(name: str, value: str, label: str, checked: bool) -> str:
        chk = "checked" if checked else ""
        return (
            f"<label style='display:inline-block;margin-right:14px;font-size:14px'>"
            f"<input type='checkbox' name='{name}' value='{escape(value)}' {chk}> "
            f"{escape(label)}</label>"
        )

    channel_html = "".join(
        _checkbox("channels", c, c.upper(), c in user.channels)
        for c in _ALL_CHANNELS
    )
    schedule_html = "".join(
        _checkbox(
            "schedules", m, m.title(),
            m in (user.schedules or {}),
        )
        for m in _ALL_MODES
    )
    category_html = "".join(
        _checkbox("categories", c, c.title(), c in user.categories)
        for c in _ALL_CATS
    )

    cap_options = ["0", "5", "10", "25", "50", "100"]
    cap_current = str(user.monthly_cost_cap_usd or 0)
    cap_html = "".join(
        f"<option value='{v}' {'selected' if v == cap_current else ''}>"
        f"{'No cap' if v == '0' else '$' + v + ' / month'}</option>"
        for v in cap_options
    )

    body = f"""\
<form method="post" action="/settings" style="background:white;border:1px solid #eee;border-radius:8px;padding:24px">
  <p style='color:#666;font-size:13px;margin-top:0'>
    Signed in as <strong>{escape(user.email)}</strong>
    {f"&nbsp;·&nbsp; role: <strong>{escape(user.role)}</strong>" if user.role == "admin" else ""}
  </p>

  <h2 style='font-size:15px;margin:18px 0 6px'>Delivery channels</h2>
  <div>{channel_html}</div>
  <p style='color:#888;font-size:12px;margin:4px 0 0'>
    SMS requires Twilio to be enabled at the system level — currently off in this deployment.
  </p>

  <h2 style='font-size:15px;margin:18px 0 6px'>Schedules</h2>
  <div>{schedule_html}</div>
  <p style='color:#888;font-size:12px;margin:4px 0 0'>
    Uncheck a mode to stop receiving that run. Morning = pre-meeting briefs.
  </p>

  <h2 style='font-size:15px;margin:18px 0 6px'>Categories to surface</h2>
  <div>{category_html}</div>
  <p style='color:#888;font-size:12px;margin:4px 0 0'>
    Admin staff often don't need "clinical". Uncheck what doesn't apply.
  </p>

  <h2 style='font-size:15px;margin:18px 0 6px'>Monthly spend cap</h2>
  <select name='cost_cap' style='font-size:14px;padding:6px 10px;border:1px solid #ccc;border-radius:4px'>
    {cap_html}
  </select>
  <p style='color:#888;font-size:12px;margin:4px 0 0'>
    When this month's Bedrock cost exceeds the cap, scheduled runs skip with a
    warning email. Resets on the 1st.
  </p>

  <div style='margin-top:24px'>
    <button type="submit" style="background:#2c6cdf;color:white;border:0;padding:10px 22px;
      border-radius:6px;font-weight:600;cursor:pointer">Save</button>
  </div>
</form>

<div style='background:#fff8f8;border:1px solid #f3d6d6;border-radius:8px;padding:24px;margin-top:24px'>
  <h2 style='font-size:15px;margin:0 0 6px;color:#b04141'>Danger zone</h2>
  <p style='color:#666;font-size:13px;margin:0 0 12px'>
    Delete your enrollment. Your future scheduled runs stop immediately and
    your stored agendas + closures + notes + pins are removed from S3. Audit
    log entries are retained for compliance.
  </p>
  <form method='post' action='/settings/unenroll' onsubmit='return confirm("Delete your EMIS enrollment? This cannot be undone.");'>
    <input type='hidden' name='confirm' value='YES'>
    <button type='submit' style='background:white;color:#b04141;border:1px solid #f3d6d6;
      padding:8px 16px;border-radius:6px;font-size:13px;cursor:pointer'>Delete my account</button>
  </form>
</div>
"""
    return _resp(_settings_chrome(body, saved=saved))


def _handle_settings_post(event: dict[str, Any], user_id: str) -> dict[str, Any]:
    from urllib.parse import parse_qs
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    form = parse_qs(raw, keep_blank_values=False)

    new_channels = {c for c in form.get("channels", []) if c in _ALL_CHANNELS}
    if not new_channels:
        new_channels = {"email"}  # always at least email

    posted_modes = {m for m in form.get("schedules", []) if m in _ALL_MODES}
    # Preserve existing time strings; just include/exclude mode keys.
    user = load_user(user_id)
    existing = (user.schedules if user else {}) or {}
    defaults = {
        "monday": "06:00", "wednesday": "08:00",
        "friday": "15:00", "morning": "06:30",
    }
    new_schedules = {m: existing.get(m, defaults[m]) for m in posted_modes}

    new_cats = {c for c in form.get("categories", []) if c in _ALL_CATS}
    if not new_cats:
        new_cats = {"business"}

    cost_cap_raw = (form.get("cost_cap", ["0"])[0] or "0").strip()
    try:
        cost_cap = max(0, int(cost_cap_raw))
    except ValueError:
        cost_cap = 0

    update_settings(
        user_id,
        channels=new_channels,
        schedules=new_schedules,
        categories=new_cats,
        monthly_cost_cap_usd=cost_cap,
    )
    logger.info("user %s updated settings", user_id)
    return _redirect("/settings?saved=1")


def _handle_unenroll(event: dict[str, Any], user_id: str) -> dict[str, Any]:
    """Hard-delete the signed-in user's enrollment + S3 prefix. Audit log
    entries are preserved for compliance — only the user's record + state
    are removed."""
    from urllib.parse import parse_qs
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    form = parse_qs(raw)
    if (form.get("confirm", [""])[0] or "").upper() != "YES":
        return _resp(_chrome("Cancelled", "<p>No confirmation. <a href='/settings'>← back</a></p>"), status=400)

    user = load_user(user_id)
    email = user.email if user else ""

    # Delete the user's S3 prefix (runs/, state/, attachments/).
    try:
        bucket = os.environ["STATE_BUCKET"]
        s3 = boto3.client("s3")
        paginator = s3.get_paginator("list_objects_v2")
        prefix = f"users/{user_id}/"
        # Batch delete (1000 keys per call).
        batch: list[dict] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                batch.append({"Key": obj["Key"]})
                if len(batch) >= 1000:
                    s3.delete_objects(Bucket=bucket, Delete={"Objects": batch})
                    batch = []
        if batch:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": batch})
        logger.info("unenroll: cleared S3 prefix %s", prefix)
    except Exception as exc:
        logger.warning("unenroll S3 cleanup failed: %s", exc)

    # Delete the DDB record.
    try:
        delete_user(user_id)
    except Exception as exc:
        logger.warning("unenroll DDB delete failed: %s", exc)
        return _resp(_chrome("Unenroll failed",
            f"<p>S3 was cleaned but DDB delete failed: {escape(str(exc))}</p>"
        ), status=500)

    audit_mod.record_event(
        "unenroll", user_id=user_id, email=email, request=event,
    )

    return _redirect(
        "/login?unenrolled=1",
        extra_cookies=[session_mod.clear_session_cookie()],
    )


# ── Admin pages ────────────────────────────────────────────────────────

def _handle_admin_index() -> dict[str, Any]:
    import os as _os
    bucket = _os.environ["STATE_BUCKET"]
    table = boto3.resource("dynamodb").Table(_os.environ["USERS_TABLE"])
    all_items: list[dict] = []
    paginator = table.meta.client.get_paginator("scan")
    for page in paginator.paginate(TableName=_os.environ["USERS_TABLE"]):
        all_items.extend(page.get("Items", []))

    # Per-user cost rollup (last 30 days)
    from .telemetry import load_runs, summarize_per_user
    try:
        runs = load_runs(bucket)
        cost_by_user = summarize_per_user(runs, days=30)
    except Exception as exc:
        logger.warning("loading telemetry failed: %s", exc)
        cost_by_user = {}
    total_cost = round(sum(s["cost_usd"] for s in cost_by_user.values()), 2)

    rows: list[str] = []
    for it in sorted(all_items, key=lambda x: x.get("email", "")):
        uid = it.get("user_id", "")
        email = it.get("email", "")
        role = it.get("role", "user")
        status = it.get("status", "active")
        last_run = (it.get("last_run_at", "") or "")[:16].replace("T", " ")
        last_err = (it.get("last_error", "") or "")[:60]
        err_html = (
            f"<div style='color:#b04141;font-size:11px;margin-top:2px'>"
            f"⚠ {escape(last_err)}</div>"
            if last_err else ""
        )
        cost = cost_by_user.get(uid, {})
        cost_cell = (
            f"${cost.get('cost_usd', 0):.2f}"
            f"<div style='color:#888;font-size:11px;margin-top:2px'>"
            f"{cost.get('runs', 0)} run{'s' if cost.get('runs', 0) != 1 else ''}"
            + (f" · {cost.get('errors')} err" if cost.get("errors", 0) else "")
            + "</div>"
        )
        status_pill = (
            f"<span style='background:{'#e8f5e9' if status == 'active' else '#fce4ec'};"
            f"color:{'#1b5e20' if status == 'active' else '#880e4f'};"
            f"padding:2px 8px;border-radius:10px;font-size:11px;"
            f"text-transform:uppercase;letter-spacing:0.5px'>{escape(status)}</span>"
        )
        role_pill = (
            f"<span style='background:#fff4e0;color:#a55a00;"
            f"padding:2px 8px;border-radius:10px;font-size:11px;"
            f"text-transform:uppercase;letter-spacing:0.5px;margin-left:4px'>admin</span>"
            if role == "admin" else ""
        )
        toggle = "pause" if status == "active" else "resume"
        rows.append(f"""\
<tr style="border-top:1px solid #eee">
  <td style="padding:10px 8px">
    <strong>{escape(email)}</strong>{role_pill}<br>
    <span style="color:#888;font-size:11px;font-family:monospace">{escape(uid)}</span>
  </td>
  <td style="padding:10px 8px">{status_pill}</td>
  <td style="padding:10px 8px;font-size:12px;color:#666">{escape(last_run) or '—'}{err_html}</td>
  <td style="padding:10px 8px;font-size:13px">{cost_cell}</td>
  <td style="padding:10px 8px">
    <form method="post" action="/admin/action" style="display:inline">
      <input type="hidden" name="user_id" value="{escape(uid)}">
      <input type="hidden" name="action" value="{toggle}">
      <button type="submit" style="background:white;border:1px solid #ccc;padding:4px 10px;
        border-radius:4px;font-size:11px;cursor:pointer">{toggle}</button>
    </form>
    <form method="post" action="/admin/action" style="display:inline;margin-left:4px">
      <input type="hidden" name="user_id" value="{escape(uid)}">
      <input type="hidden" name="action" value="run_now">
      <input type="hidden" name="mode" value="monday">
      <button type="submit" style="background:#eef4ff;border:1px solid #cfe0ff;color:#2c6cdf;
        padding:4px 10px;border-radius:4px;font-size:11px;cursor:pointer">run now</button>
    </form>
  </td>
</tr>
""")

    body = f"""\
<div style='display:flex;justify-content:space-between;align-items:center'>
  <h1 style='margin:0'>EMIS Admin</h1>
  <div style='font-size:13px'>
    <a href='/admin/audit'>Audit log</a> ·
    <a href='/'>dashboard</a> ·
    <a href='/settings'>settings</a>
  </div>
</div>
<p style='color:#666'>
  {len(all_items)} enrolled users
  &nbsp;·&nbsp; ${total_cost:.2f} Bedrock spend (last 30d)
</p>

<div style='background:white;border:1px solid #eee;border-radius:8px;overflow:hidden;margin-top:12px'>
  <table style='width:100%;border-collapse:collapse;font-size:13px'>
    <thead style='background:#f7faff'>
      <tr>
        <th style='text-align:left;padding:10px 8px'>User</th>
        <th style='text-align:left;padding:10px 8px'>Status</th>
        <th style='text-align:left;padding:10px 8px'>Last run</th>
        <th style='text-align:left;padding:10px 8px'>Cost (30d)</th>
        <th style='text-align:left;padding:10px 8px'>Actions</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows) or '<tr><td colspan=5 style="padding:24px;text-align:center;color:#aaa">No users yet</td></tr>'}
    </tbody>
  </table>
</div>
"""
    return _resp(_chrome("EMIS Admin", body))


def _handle_admin_audit() -> dict[str, Any]:
    events = audit_mod.list_recent(days=14, limit=200)

    # Resolve user_ids to emails for readability — single scan of users table.
    import os as _os
    table = boto3.resource("dynamodb").Table(_os.environ["USERS_TABLE"])
    emails: dict[str, str] = {}
    try:
        paginator = table.meta.client.get_paginator("scan")
        for page in paginator.paginate(
            TableName=_os.environ["USERS_TABLE"],
            ProjectionExpression="user_id, email",
        ):
            for it in page.get("Items", []):
                emails[it.get("user_id", "")] = it.get("email", "")
    except Exception as exc:
        logger.warning("scan for emails failed: %s", exc)

    def _who(uid: str) -> str:
        if not uid:
            return ""
        return emails.get(uid, uid[:8])

    rows: list[str] = []
    for e in events:
        ts = (e.get("ts") or "")[:19].replace("T", " ")
        ev = e.get("event", "")
        ev_color = {
            "login_success": "#1b5e20",
            "admin_pause": "#a55a00",
            "admin_resume": "#1b5e20",
            "admin_force_run": "#2c6cdf",
            "enrollment": "#5b21b6",
        }.get(ev, "#666")
        actor = _who(e.get("actor_user_id", "")) or _who(e.get("user_id", ""))
        target = _who(e.get("target_user_id", ""))
        ip = e.get("ip", "")
        extra = e.get("extra") or {}
        extra_str = ", ".join(f"{k}={v}" for k, v in extra.items())
        rows.append(f"""\
<tr style="border-top:1px solid #eee">
  <td style="padding:6px 8px;font-family:monospace;font-size:12px;color:#666;white-space:nowrap">{escape(ts)}</td>
  <td style="padding:6px 8px;font-size:12px"><span style="color:{ev_color};font-weight:600">{escape(ev)}</span></td>
  <td style="padding:6px 8px;font-size:12px">{escape(actor)}</td>
  <td style="padding:6px 8px;font-size:12px">{escape(target)}</td>
  <td style="padding:6px 8px;font-size:11px;color:#888;font-family:monospace">{escape(ip)}</td>
  <td style="padding:6px 8px;font-size:11px;color:#888">{escape(extra_str)}</td>
</tr>
""")

    body = f"""\
<div style='display:flex;justify-content:space-between;align-items:center'>
  <h1 style='margin:0'>Audit log</h1>
  <div style='font-size:13px'><a href='/admin'>← admin</a> · <a href='/'>dashboard</a></div>
</div>
<p style='color:#666'>{len(events)} events, last 14 days</p>

<div style='background:white;border:1px solid #eee;border-radius:8px;overflow:hidden;margin-top:12px'>
  <table style='width:100%;border-collapse:collapse'>
    <thead style='background:#f7faff'>
      <tr>
        <th style='text-align:left;padding:10px 8px;font-size:12px'>When</th>
        <th style='text-align:left;padding:10px 8px;font-size:12px'>Event</th>
        <th style='text-align:left;padding:10px 8px;font-size:12px'>Actor</th>
        <th style='text-align:left;padding:10px 8px;font-size:12px'>Target</th>
        <th style='text-align:left;padding:10px 8px;font-size:12px'>IP</th>
        <th style='text-align:left;padding:10px 8px;font-size:12px'>Detail</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows) or '<tr><td colspan=6 style="padding:24px;text-align:center;color:#aaa">No events yet</td></tr>'}
    </tbody>
  </table>
</div>
"""
    return _resp(_chrome("Audit log", body))


def _handle_admin_action(event: dict[str, Any], viewer) -> dict[str, Any]:
    from urllib.parse import parse_qs
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    form = parse_qs(raw, keep_blank_values=False)
    target_uid = (form.get("user_id", [""])[0] or "").strip()
    action = (form.get("action", [""])[0] or "").strip()

    if not target_uid or action not in ("pause", "resume", "run_now"):
        return _resp(_chrome("Bad request", "<p>Invalid action.</p>"), status=400)

    if action == "pause":
        update_settings(target_uid, status="paused")
        logger.info("admin %s paused user %s", viewer.user_id, target_uid)
        audit_mod.record_event(
            "admin_pause",
            actor_user_id=viewer.user_id, target_user_id=target_uid,
            request=event,
        )
    elif action == "resume":
        update_settings(target_uid, status="active")
        logger.info("admin %s resumed user %s", viewer.user_id, target_uid)
        audit_mod.record_event(
            "admin_resume",
            actor_user_id=viewer.user_id, target_user_id=target_uid,
            request=event,
        )
    elif action == "run_now":
        mode = (form.get("mode", ["monday"])[0] or "monday").strip()
        try:
            lambda_client = boto3.client("lambda")
            lambda_client.invoke(
                FunctionName=os.environ["AGENDA_FUNCTION_NAME"],
                InvocationType="Event",
                Payload=json.dumps({"mode": mode, "user_id": target_uid}).encode("utf-8"),
            )
            logger.info("admin %s force-ran %s/%s", viewer.user_id, target_uid, mode)
            audit_mod.record_event(
                "admin_force_run",
                actor_user_id=viewer.user_id, target_user_id=target_uid,
                request=event, extra={"mode": mode},
            )
        except Exception as exc:
            logger.warning("force-run failed: %s", exc)
            return _resp(_chrome("Run failed",
                f"<p>Failed to invoke agenda Lambda: {escape(str(exc))}</p>"
            ), status=500)

    return _redirect("/admin")


def _handle_closure_post(event: dict[str, Any], bucket: str, user_id: str | None = None) -> dict[str, Any]:
    """Apply a closure / undo / note / pin action from the dashboard."""
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _resp('{"error":"invalid_json"}', status=400, content_type="application/json")

    action = (payload.get("action") or "").lower()
    item_match = (payload.get("item_match") or "").strip()
    if not item_match or action not in (_CLOSURE_ACTIONS | _OTHER_ACTIONS):
        return _resp('{"error":"bad_request"}', status=400, content_type="application/json")

    now_iso = datetime.now(timezone.utc).isoformat()

    if action in _CLOSURE_ACTIONS:
        closures = load_closures(bucket, user_id=user_id)
        if action == "done":
            closures.done.append(DoneRecord(
                item_match=item_match, completed_at=now_iso,
                source="web_ui", source_id="",
            ))
        elif action == "drop":
            closures.drops.append(DropRecord(
                item_match=item_match, dropped_at=now_iso,
                source_message_id="",
            ))
        else:  # snooze
            until_iso = (payload.get("until_iso") or "").strip()
            if not until_iso:
                return _resp('{"error":"missing_until"}', status=400, content_type="application/json")
            closures.snoozes.append(SnoozeRecord(
                item_match=item_match, until_iso=until_iso,
                snoozed_at=now_iso, source_message_id="",
            ))
        save_closures(bucket, closures, user_id=user_id)
        logger.info("web_ui closure: %s %r", action, item_match)
        return _resp('{"ok":true}', status=200, content_type="application/json")

    if action == "undo":
        # Remove the most recent closure record matching item_match, optionally
        # filtered by which list (done/drop/snooze) was the original action.
        original = (payload.get("original_action") or "").lower()
        closures = load_closures(bucket, user_id=user_id)
        target_lists: list[list] = []
        if original == "done":
            target_lists = [closures.done]
        elif original == "drop":
            target_lists = [closures.drops]
        elif original == "snooze":
            target_lists = [closures.snoozes]
        else:
            target_lists = [closures.done, closures.drops, closures.snoozes]
        removed = False
        for lst in target_lists:
            for i in range(len(lst) - 1, -1, -1):
                if lst[i].item_match == item_match:
                    del lst[i]
                    removed = True
                    break
            if removed:
                break
        if removed:
            save_closures(bucket, closures, user_id=user_id)
        logger.info("web_ui undo (removed=%s): %r", removed, item_match)
        return _resp('{"ok":true}', status=200, content_type="application/json")

    if action == "set_note":
        note = (payload.get("note") or "").strip()
        notes = load_notes(bucket, user_id=user_id)
        if note:
            notes[item_match] = note
        else:
            notes.pop(item_match, None)
        save_notes(bucket, notes, user_id=user_id)
        logger.info("web_ui set_note: %r (%d chars)", item_match, len(note))
        return _resp('{"ok":true}', status=200, content_type="application/json")

    # pin / unpin
    pins = load_pins(bucket, user_id=user_id)
    if action == "pin" and item_match not in pins:
        pins.append(item_match)
    elif action == "unpin" and item_match in pins:
        pins.remove(item_match)
    save_pins(bucket, pins, user_id=user_id)
    logger.info("web_ui %s: %r", action, item_match)
    return _resp('{"ok":true}', status=200, content_type="application/json")


def _find_latest_agenda(bucket: str, user_id: str | None = None) -> tuple[str, str, dict] | None:
    """Return (week_iso, mode, agenda) for the most recent stored agenda.

    Preference order: most recent week with Monday agenda (canonical); fall
    back to whatever mode exists for that week. Returns None if no agendas
    are persisted yet.
    """
    weeks = store.list_weeks(bucket, limit=1, user_id=user_id)
    if not weeks:
        return None
    week_iso = weeks[0]
    for mode_name in ("monday", "wednesday", "friday"):
        agenda = store.load_agenda(bucket, week_iso, mode_name, user_id=user_id)
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


def _render_index_page(bucket: str, token: str, user_id: str | None = None) -> str:
    weeks = store.list_weeks(bucket, limit=52, user_id=user_id)
    if not weeks:
        return _chrome("EMIS", "<h1>EMIS</h1><p>No agendas yet.</p>")

    s3 = boto3.client("s3")
    rows: list[str] = []
    for w in weeks:
        modes = _modes_for_week(s3, bucket, w, user_id)
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


def _render_week_page(bucket: str, week: str, token: str, user_id: str | None = None) -> str:
    s3 = boto3.client("s3")
    modes = _modes_for_week(s3, bucket, week, user_id)
    if not modes:
        return _chrome(week, f"<h1>{escape(week)}</h1><p>No modes recorded.</p>")
    links = "<ul>" + "".join(
        f"<li><a href='?{urlencode({'token': token, 'week': week, 'mode': m})}'>{escape(m).title()}</a></li>"
        for m in modes
    ) + "</ul>"
    nav = f"<div class='nav'><a href='?{urlencode({'token': token})}'>← all weeks</a></div>"
    return _chrome(week, nav + f"<h1>Week {escape(week)}</h1>" + links)


def _build_nav_html(user_id: str | None) -> str:
    """Top-right nav with settings/admin/sign-out links for the logged-in user."""
    if not user_id:
        return ""
    user = load_user(user_id)
    if user is None:
        return ""
    admin_link = (
        " · <a href='/admin' style='color:#a55a00;font-weight:600'>Admin</a>"
        if user.role == "admin" else ""
    )
    reauth_banner = ""
    if user.status == "needs_reauth":
        reauth_banner = (
            "<div style='background:#fff4e0;border:1px solid #f3d6a0;"
            "color:#a55a00;padding:10px 14px;border-radius:6px;"
            "margin-bottom:12px;font-size:13px'>"
            "⚠ Your Microsoft 365 access has expired. Scheduled agendas are "
            "paused. <a href='/login' style='color:#a55a00;font-weight:600'>"
            "Sign in again →</a> to resume."
            "</div>"
        )
    return (
        f"{reauth_banner}"
        f"<div style='display:flex;justify-content:space-between;align-items:center;"
        f"font-size:12px;color:#888;margin-bottom:6px'>"
        f"<span>Signed in as <strong>{escape(user.email)}</strong></span>"
        f"<span>"
        f"<a href='/settings'>Settings</a>"
        f"{admin_link}"
        f" · <a href='/logout'>Sign out</a>"
        f"</span>"
        f"</div>"
    )


def _render_agenda_page(
    bucket: str, week: str, mode: str, agenda: dict, token: str,
    user_id: str | None = None,
) -> str:
    """Serve the interactive dashboard rendering of a stored agenda."""
    try:
        year, w = week.split("-W")
        week_start = datetime.fromisocalendar(int(year), int(w), 1)
        week_end = datetime.fromisocalendar(int(year), int(w), 7)
    except Exception:
        week_start = week_end = datetime.utcnow()
    # Pull closures + prior agendas so Backlog and History tabs are populated.
    try:
        closures = load_closures(bucket, user_id=user_id)
        closures_dict = {
            "snoozes": [s.to_dict() for s in closures.snoozes],
            "done": [d.to_dict() for d in closures.done],
            "drops": [d.to_dict() for d in closures.drops],
        }
    except Exception as exc:
        logger.warning("loading closures failed: %s", exc)
        closures_dict = None
    try:
        prior = load_prior_agendas(bucket, week_end, user_id=user_id)
    except Exception as exc:
        logger.warning("loading prior agendas failed: %s", exc)
        prior = []
    try:
        pinned = set(load_pins(bucket, user_id=user_id))
    except Exception as exc:
        logger.warning("loading pins failed: %s", exc)
        pinned = set()
    return render_dashboard_html(
        agenda, week_start, week_end, mode=mode,
        closures=closures_dict, prior_agendas=prior,
        closure_token=token, pinned_items=pinned,
        nav_html=_build_nav_html(user_id),
    )


def _modes_for_week(s3, bucket: str, week: str, user_id: str | None = None) -> list[str]:
    prefix = f"users/{user_id}/runs/{week}/" if user_id else f"runs/{week}/"
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
