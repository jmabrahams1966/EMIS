"""Per-user enrollment records (multi-tenant data model).

Schema (DynamoDB ``UsersTable``)::

    user_id        S  HASH  — Azure object_id (stable across email changes)
    email          S        — display + lookup
    refresh_token  B        — KMS-encrypted Microsoft Graph refresh token
    channels       SS       — {"email", "sms"} — which delivery to enable
    categories    SS       — subset of {clinical, business, admin, personal}
    schedules      M        — {"monday": "06:00", "wednesday": ..., ...}
    status         S        — "active" | "paused" | "removed"
    role           S        — "user" | "admin" (admin can view enrollment list)
    enrolled_at    S        — ISO timestamp
    last_run_at    S        — ISO timestamp (best-effort)
    last_error     S        — last failure message, "" on success
    sender_email   S        — From: address for this user's agenda emails

Refresh tokens are encrypted at the application layer with the customer-managed
KMS key (``TOKEN_KMS_KEY_ID`` env var) so every Decrypt call lands in CloudTrail
for audit. DDB's at-rest encryption is also enabled (SSE) as defense in depth.
"""
from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


@dataclass
class User:
    user_id: str
    email: str
    refresh_token: str            # plaintext in-memory; encrypted at rest
    channels: set[str] = field(default_factory=lambda: {"email"})
    categories: set[str] = field(
        default_factory=lambda: {"clinical", "business", "admin", "personal"}
    )
    schedules: dict[str, str] = field(
        default_factory=lambda: {
            "monday": "06:00", "wednesday": "08:00", "friday": "15:00",
            "morning": "06:30",
        }
    )
    status: str = "active"        # active | paused | needs_reauth | removed
    role: str = "user"            # user | admin
    enrolled_at: str = ""
    last_run_at: str = ""
    last_error: str = ""
    sender_email: str = ""        # From: address; defaults to email
    monthly_cost_cap_usd: int = 0  # 0 = no cap; integer dollars

    def to_item(self, encrypt_token) -> dict[str, Any]:
        """Convert to a DDB item dict. ``encrypt_token(plaintext) -> bytes``
        encrypts the refresh token with KMS."""
        encrypted = encrypt_token(self.refresh_token) if self.refresh_token else b""
        return {
            "user_id": self.user_id,
            "email": self.email,
            "refresh_token": encrypted,
            "channels": set(self.channels) if self.channels else {"email"},
            "categories": set(self.categories) if self.categories else {"clinical"},
            "schedules": self.schedules,
            "status": self.status,
            "role": self.role,
            "enrolled_at": self.enrolled_at or datetime.now(timezone.utc).isoformat(),
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "sender_email": self.sender_email or self.email,
        }


# ── KMS helpers ──────────────────────────────────────────────────────────

def _kms_client():
    return boto3.client("kms")


def encrypt_token(plaintext: str) -> bytes:
    key_id = os.environ["TOKEN_KMS_KEY_ID"]
    resp = _kms_client().encrypt(
        KeyId=key_id, Plaintext=plaintext.encode("utf-8"),
    )
    return resp["CiphertextBlob"]


def decrypt_token(ciphertext: bytes) -> str:
    resp = _kms_client().decrypt(CiphertextBlob=ciphertext)
    return resp["Plaintext"].decode("utf-8")


# ── DDB helpers ──────────────────────────────────────────────────────────

def _table():
    return boto3.resource("dynamodb").Table(os.environ["USERS_TABLE"])


def _from_item(item: dict[str, Any]) -> User:
    raw_token = item.get("refresh_token")
    # boto3's resource interface returns binary attributes as
    # ``boto3.dynamodb.types.Binary`` (has a ``.value`` attribute that's bytes).
    # The client interface returns raw bytes. CLI/JSON returns base64 strings.
    token = ""
    if raw_token is None or raw_token == b"":
        token = ""
    elif hasattr(raw_token, "value"):
        token = decrypt_token(bytes(raw_token.value))
    elif isinstance(raw_token, (bytes, bytearray)):
        token = decrypt_token(bytes(raw_token))
    elif isinstance(raw_token, str) and raw_token:
        token = decrypt_token(base64.b64decode(raw_token))
    return User(
        user_id=item["user_id"],
        email=item.get("email", ""),
        refresh_token=token,
        channels=set(item.get("channels") or {"email"}),
        categories=set(item.get("categories") or {"clinical", "business", "admin", "personal"}),
        schedules=dict(item.get("schedules") or {}),
        status=item.get("status", "active"),
        role=item.get("role", "user"),
        enrolled_at=item.get("enrolled_at", ""),
        last_run_at=item.get("last_run_at", ""),
        last_error=item.get("last_error", ""),
        sender_email=item.get("sender_email", ""),
        monthly_cost_cap_usd=int(item.get("monthly_cost_cap_usd") or 0),
    )


def load_user(user_id: str) -> User | None:
    """Return the User record for ``user_id``, or None if missing."""
    try:
        resp = _table().get_item(Key={"user_id": user_id})
    except ClientError as exc:
        logger.warning("DDB get_item failed for %s: %s", user_id, exc)
        return None
    item = resp.get("Item")
    return _from_item(item) if item else None


def save_user(user: User) -> None:
    """Persist a User record (overwrites). Encrypts the refresh token."""
    _table().put_item(Item=user.to_item(encrypt_token))


def update_refresh_token(user_id: str, new_token: str) -> None:
    """Rotate a user's refresh token without touching the rest of the record.

    Microsoft Graph rotates the refresh token on every token-exchange call;
    this lets the agenda Lambda persist the new one cheaply without round-
    tripping the full User record.
    """
    encrypted = encrypt_token(new_token)
    _table().update_item(
        Key={"user_id": user_id},
        UpdateExpression="SET refresh_token = :t, last_run_at = :n",
        ExpressionAttributeValues={
            ":t": encrypted,
            ":n": datetime.now(timezone.utc).isoformat(),
        },
    )


def update_settings(
    user_id: str,
    *,
    channels: set[str] | None = None,
    schedules: dict[str, str] | None = None,
    categories: set[str] | None = None,
    status: str | None = None,
    monthly_cost_cap_usd: int | None = None,
) -> None:
    """Partial-update a user's preferences. Only fields passed are touched."""
    parts: list[str] = []
    vals: dict[str, Any] = {}
    if channels is not None:
        parts.append("channels = :ch")
        vals[":ch"] = set(channels) if channels else {"email"}
    if schedules is not None:
        parts.append("schedules = :sc")
        vals[":sc"] = dict(schedules)
    if categories is not None:
        parts.append("categories = :ca")
        vals[":ca"] = set(categories) if categories else {"clinical"}
    if status is not None:
        parts.append("#st = :s")
        vals[":s"] = status
    if monthly_cost_cap_usd is not None:
        parts.append("monthly_cost_cap_usd = :cap")
        vals[":cap"] = max(0, int(monthly_cost_cap_usd))
    if not parts:
        return
    kwargs: dict[str, Any] = {
        "Key": {"user_id": user_id},
        "UpdateExpression": "SET " + ", ".join(parts),
        "ExpressionAttributeValues": vals,
    }
    if status is not None:
        kwargs["ExpressionAttributeNames"] = {"#st": "status"}
    _table().update_item(**kwargs)


def delete_user(user_id: str) -> None:
    """Hard-delete a user record. Use sparingly — see also `update_settings(status="removed")`
    for a soft-delete that preserves history."""
    _table().delete_item(Key={"user_id": user_id})


def record_run_outcome(user_id: str, *, ok: bool, error: str = "") -> None:
    """Update last_run_at and last_error after a per-user run."""
    _table().update_item(
        Key={"user_id": user_id},
        UpdateExpression="SET last_run_at = :n, last_error = :e",
        ExpressionAttributeValues={
            ":n": datetime.now(timezone.utc).isoformat(),
            ":e": "" if ok else error[:1000],
        },
    )


def list_active_users() -> Iterable[User]:
    """Yield every user with status == 'active'.

    Uses Scan + filter — fine for N < a few thousand. If we ever grow past
    that, add a GSI on status.
    """
    paginator = _table().meta.client.get_paginator("scan")
    for page in paginator.paginate(
        TableName=os.environ["USERS_TABLE"],
        FilterExpression=Attr("status").eq("active"),
    ):
        for item in page.get("Items", []):
            try:
                yield _from_item(item)
            except Exception as exc:
                logger.warning(
                    "skipping user %s: deserialize failed: %s",
                    item.get("user_id"), exc,
                )
