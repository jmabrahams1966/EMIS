"""Coordinator Lambda — schedule fan-out for multi-tenant EMIS.

Triggered by EventBridge on each agenda schedule (Monday, Wednesday, Friday,
Morning briefs). Enumerates active users in the Users table and invokes the
Agenda Lambda asynchronously once per user with ``{"mode": ..., "user_id": ...}``.

Fan-out is intentionally async — each per-user run can take 3-5 minutes and
they're independent. Coordinator finishes quickly; per-user runs land in
parallel via Lambda's async invoke queue (and respect the
``MaximumRetryAttempts=0`` setting on AgendaFunction so a per-user crash
doesn't re-fire emails).

Event shape (from EventBridge)::

    {"mode": "monday" | "wednesday" | "friday" | "morning"}
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3

from .users import list_active_users

logger = logging.getLogger("emis.coordinator")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    mode = (event or {}).get("mode") or "monday"
    target_function = os.environ["AGENDA_FUNCTION_NAME"]
    morning_only = mode == "morning"

    lambda_client = boto3.client("lambda")
    invoked: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for user in list_active_users():
        # Morning briefs are opt-in per user. Users can disable briefs by
        # removing "morning" from their schedules dict; the weekly modes
        # use Monday/Wednesday/Friday keys. (Default User has all four.)
        if mode in ("monday", "wednesday", "friday", "morning"):
            if mode not in (user.schedules or {}):
                skipped.append({"user_id": user.user_id, "reason": "mode_disabled"})
                continue

        payload = {"mode": mode, "user_id": user.user_id}
        try:
            lambda_client.invoke(
                FunctionName=target_function,
                InvocationType="Event",
                Payload=json.dumps(payload).encode("utf-8"),
            )
            invoked.append({"user_id": user.user_id, "email": user.email})
        except Exception as exc:
            logger.warning("invoke for %s failed: %s", user.user_id, exc)
            skipped.append({"user_id": user.user_id, "reason": str(exc)})

    logger.info(
        "coordinator mode=%s invoked=%d skipped=%d",
        mode, len(invoked), len(skipped),
    )
    return {
        "mode": mode,
        "invoked_count": len(invoked),
        "skipped_count": len(skipped),
        "invoked": invoked,
        "skipped": skipped,
    }
