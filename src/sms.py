"""Optional SMS delivery channel via Twilio.

Sends a short notification to the user's phone summarizing the agenda plus
a dashboard link, so the user can read it on mobile without opening email.

Disabled by default. Activates when ``EMIS_TWILIO_SECRET_ID`` is set and the
named secret contains ``account_sid``, ``auth_token``, ``from_number``, and
``to_number``. Send failures log a warning but never block the pipeline.

Note: Twilio sends standard SMS, not iMessage. On iPhone the message will
appear as a green-bubble SMS (not the blue iMessage style). For US numbers
Twilio requires either 10DLC registration (for A2P short-codes) or a
toll-free number — for personal use, a toll-free number is the simplest path.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

import boto3
import httpx

logger = logging.getLogger(__name__)


def _load_twilio_secret() -> dict[str, str] | None:
    sid = os.getenv("EMIS_TWILIO_SECRET_ID", "").strip()
    if not sid:
        return None
    try:
        sm = boto3.client("secretsmanager")
        resp = sm.get_secret_value(SecretId=sid)
        raw = resp.get("SecretString") or base64.b64decode(resp.get("SecretBinary", b""))
        secret = json.loads(raw)
    except Exception as exc:
        logger.warning("twilio secret load failed: %s", exc)
        return None
    needed = {"account_sid", "auth_token", "from_number", "to_number"}
    if not needed.issubset(secret):
        logger.warning("twilio secret missing one of %s", needed)
        return None
    return secret


def _compose_body(agenda: dict[str, Any], mode: str, dashboard_url: str) -> str:
    """Compose a short SMS body. Caps at ~280 chars to stay inside 2 segments."""
    label = {
        "monday": "Mon agenda",
        "wednesday": "Wed check-in",
        "friday": "Fri recap",
    }.get(mode, "Agenda")

    priorities = agenda.get("priorities") or []
    actions = agenda.get("action_items") or []
    follow_ups = agenda.get("follow_ups") or []

    top_titles = [
        (p.get("title") or "").strip()
        for p in priorities[:2] if (p.get("title") or "").strip()
    ]
    top_str = "; ".join(top_titles) if top_titles else ""

    counts = (
        f"{len(priorities)} pri · {len(actions)} act · {len(follow_ups)} f/u"
    )
    parts = [f"EMIS {label}: {counts}"]
    if top_str:
        parts.append(f"Top: {top_str}")
    if dashboard_url:
        parts.append(dashboard_url)
    body = "\n".join(parts)
    if len(body) > 320:
        body = body[:317] + "..."
    return body


def send_agenda_sms(
    *,
    agenda: dict[str, Any],
    mode: str,
    dashboard_url: str,
) -> dict[str, Any]:
    """Send a one-shot SMS summarizing this agenda. Best-effort.

    Returns ``{"status": "skipped"|"sent", "sid": ...}``. Never raises.
    """
    secret = _load_twilio_secret()
    if not secret:
        return {"status": "skipped", "reason": "no_twilio_secret"}

    body = _compose_body(agenda, mode, dashboard_url)
    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/"
        f"{secret['account_sid']}/Messages.json"
    )
    try:
        resp = httpx.post(
            url,
            data={
                "From": secret["from_number"],
                "To": secret["to_number"],
                "Body": body,
            },
            auth=(secret["account_sid"], secret["auth_token"]),
            timeout=15,
        )
        resp.raise_for_status()
        sid = resp.json().get("sid", "")
        logger.info("twilio SMS sent sid=%s", sid)
        return {"status": "sent", "sid": sid}
    except Exception as exc:
        logger.warning("twilio SMS failed: %s", exc)
        return {"status": "error", "error": str(exc)}
