"""Enrollment Lambda — self-service signup for new users.

Three routes:

    GET /                  → landing page with "Enroll" button
    GET /start             → 302 to Azure authorize URL
    GET /callback?code=…   → exchange code, write User record to DDB, show success

Reads the Azure app client_id + tenant_id from the same ``emis/graph`` secret
the agenda Lambda uses (single app registration for the whole tenant). The
redirect URI must match what's registered on the Azure app — we expose this
Lambda via Function URL and tell IT to add ``<url>/callback`` as a redirect.

Self-service note: anyone with the enrollment URL who can log into the M365
tenant can enroll. For a small office that's appropriate; in larger orgs
you'd front this with SSO group check or admin invitation.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timezone
from html import escape
from typing import Any
from urllib.parse import urlencode

import boto3
import httpx

from .users import User, save_user, load_user
from . import audit as audit_mod

logger = logging.getLogger("emis.enrollment")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

AUTHORIZE_BASE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
TOKEN_BASE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
GRAPH_ME = "https://graph.microsoft.com/v1.0/me"

# Scopes match what the original `email-digest` app was bootstrapped with
# in the nybrainspine.com tenant, so users can self-enroll without hitting
# the "admin approval required" gate. Scopes the original bootstrap omitted
# (Calendars.ReadWrite, Mail.Send) are also dropped here — features that
# rely on them (weekly-plan calendar event creation, nudge draft replies)
# will silently no-op for staff. The original founder/admin user can still
# use them because they personally consented to them earlier.
SCOPES = (
    "openid profile email offline_access "
    "Mail.Read Mail.ReadWrite "
    "Calendars.Read "
    "Tasks.ReadWrite Files.ReadWrite "
    "MailboxSettings.Read"
)


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


def _redirect(url: str) -> dict[str, Any]:
    return {"statusCode": 302, "headers": {"Location": url, "Cache-Control": "no-store"}, "body": ""}


def _chrome(title: str, body: str) -> str:
    return f"""\
<!doctype html><html><head><meta charset="utf-8">
<title>{escape(title)} — EMIS</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          max-width: 540px; margin: 60px auto; padding: 0 24px; color: #222;
          line-height: 1.55; }}
  h1 {{ margin-bottom: 8px; }}
  .btn {{ display: inline-block; background: #2c6cdf; color: white;
          text-decoration: none; font-weight: 600; padding: 10px 18px;
          border-radius: 6px; margin-top: 16px; }}
  .ok {{ color: #1b5e20; }}
  .err {{ color: #b04141; }}
  .meta {{ color: #666; font-size: 13px; margin-top: 24px; }}
</style></head><body>{body}</body></html>
"""


def _load_graph_secret() -> dict[str, str]:
    sid = os.environ["GRAPH_SECRET_ID"]
    sm = boto3.client("secretsmanager")
    resp = sm.get_secret_value(SecretId=sid)
    return json.loads(resp.get("SecretString") or "{}")


def _self_url(event: dict[str, Any]) -> str:
    """Reconstruct the Lambda Function URL base from the event."""
    host = (event.get("headers") or {}).get("host", "")
    proto = (event.get("headers") or {}).get("x-forwarded-proto", "https")
    return f"{proto}://{host}"


def _authorize_url(client_id: str, tenant_id: str, redirect_uri: str, state: str) -> str:
    qs = urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": SCOPES,
        "state": state,
        "prompt": "select_account",
    })
    return AUTHORIZE_BASE.format(tenant=tenant_id) + "?" + qs


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    method = (
        (event.get("requestContext") or {}).get("http", {}).get("method")
        or event.get("httpMethod") or "GET"
    ).upper()
    path = (
        (event.get("requestContext") or {}).get("http", {}).get("path")
        or event.get("rawPath") or "/"
    )

    if method != "GET":
        return _resp(_chrome("EMIS", "<p>Method not allowed.</p>"), status=405)

    graph = _load_graph_secret()
    tenant_id = graph.get("tenant_id", "common")
    client_id = graph["client_id"]
    client_secret = graph.get("client_secret", "")
    redirect_uri = f"{_self_url(event)}/callback"

    if path == "/start":
        # Stateless state token = random hex; we don't verify on callback
        # (small-office self-service). For a tighter setup, store the state
        # in DDB with a 10-min TTL and check on return.
        state = base64.urlsafe_b64encode(os.urandom(16)).decode().rstrip("=")
        return _redirect(_authorize_url(client_id, tenant_id, redirect_uri, state))

    if path == "/callback":
        qs = event.get("queryStringParameters") or {}
        code = qs.get("code")
        if not code:
            err = qs.get("error_description") or qs.get("error") or "missing code"
            return _resp(_chrome("EMIS — enrollment failed",
                f"<h1 class='err'>Enrollment failed</h1><p>{escape(err)}</p>"
            ), status=400)
        try:
            user = _exchange_and_save(code, tenant_id, client_id, client_secret, redirect_uri)
        except Exception as exc:
            logger.exception("enrollment failed")
            return _resp(_chrome("EMIS — enrollment failed",
                f"<h1 class='err'>Enrollment failed</h1><p>{escape(str(exc))}</p>"
            ), status=500)
        audit_mod.record_event(
            "enrollment",
            user_id=user.user_id, email=user.email, request=event,
        )
        body = (
            f"<h1 class='ok'>You're enrolled!</h1>"
            f"<p>Welcome, {escape(user.email)}. You'll get your first agenda "
            f"on the next scheduled run (Monday 6:00 AM ET).</p>"
            f"<p class='meta'>user_id: {escape(user.user_id)}</p>"
        )
        return _resp(_chrome("EMIS — enrolled", body))

    # Landing page
    return _resp(_chrome("EMIS — enroll",
        f"<h1>EMIS — enroll</h1>"
        f"<p>Sign in with your work Microsoft 365 account to start receiving "
        f"weekly + mid-week + Friday agendas summarized from your inbox, "
        f"calendar, and sent mail.</p>"
        f"<p>You'll be redirected to Microsoft to grant access. EMIS reads "
        f"your mailbox, calendar, and tasks; it does not send mail on your "
        f"behalf or modify your data without your action.</p>"
        f"<a class='btn' href='/start'>Enroll with Microsoft 365 →</a>"
        f"<p class='meta'>You can opt out any time by replying STOP to your "
        f"agenda email or asking IT to remove your record.</p>"
    ))


def _exchange_and_save(
    code: str, tenant_id: str, client_id: str, client_secret: str, redirect_uri: str,
) -> User:
    """Exchange the auth code for tokens, fetch /me, persist User."""
    token_resp = httpx.post(
        TOKEN_BASE.format(tenant=tenant_id),
        data={
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_secret": client_secret,
            "scope": SCOPES,
        },
        timeout=30,
    )
    token_resp.raise_for_status()
    tokens = token_resp.json()
    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token", "")
    if not refresh_token:
        raise RuntimeError("no refresh_token in token response — offline_access scope missing?")

    me_resp = httpx.get(
        GRAPH_ME,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    me_resp.raise_for_status()
    me = me_resp.json()
    user_id = me.get("id") or ""           # Azure object_id
    email = (
        me.get("mail")
        or me.get("userPrincipalName")
        or ""
    )
    if not user_id or not email:
        raise RuntimeError(f"could not extract user_id/email from /me: {me!r}")

    # Preserve existing User record's preferences if re-enrolling.
    existing = load_user(user_id)
    user = User(
        user_id=user_id,
        email=email,
        refresh_token=refresh_token,
        channels=(existing.channels if existing else {"email"}),
        categories=(existing.categories if existing else {"clinical", "business", "admin", "personal"}),
        schedules=(existing.schedules if existing else {
            "monday": "06:00", "wednesday": "08:00", "friday": "15:00",
            "morning": "06:30",
        }),
        status="active",
        role=(existing.role if existing else "user"),
        enrolled_at=(existing.enrolled_at if existing else datetime.now(timezone.utc).isoformat()),
        last_run_at=(existing.last_run_at if existing else ""),
        last_error="",
        sender_email=(existing.sender_email if existing else email),
    )
    save_user(user)
    logger.info("enrolled user_id=%s email=%s", user_id, email)
    return user
