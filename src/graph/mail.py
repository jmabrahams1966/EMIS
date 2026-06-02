"""Read messages and attachments from Microsoft Graph.

Endpoint reference: https://learn.microsoft.com/en-us/graph/api/user-list-messages
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import httpx

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


@dataclass
class Attachment:
    name: str
    content_type: str
    size: int
    content_bytes: bytes


@dataclass
class Message:
    id: str
    subject: str
    sender: str
    sender_email: str
    received_at: datetime
    importance: str
    is_read: bool
    web_link: str
    preview: str            # bodyPreview (~255 chars)
    body_text: str          # text/plain content if available
    to_recipients: list[str] = field(default_factory=list)
    cc_recipients: list[str] = field(default_factory=list)
    has_attachments: bool = False
    conversation_id: str = ""
    attachments: list[Attachment] = field(default_factory=list)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


SYSTEM_FOLDER_WELL_KNOWN_NAMES = (
    # Folders we want to exclude from the broad mailbox scan. SentItems is
    # excluded here because list_sent_messages_since() handles it separately
    # for promises-made tracking; Drafts/Outbox/Junk/Deleted should never feed
    # the agenda.
    "drafts", "sentitems", "deleteditems", "junkemail", "outbox",
)


async def _get_excluded_folder_ids(*, access_token: str) -> set[str]:
    """Fetch IDs for the system folders we never want to scan into the agenda."""
    headers = {"Authorization": f"Bearer {access_token}"}
    ids: set[str] = set()
    async with httpx.AsyncClient(timeout=30) as client:
        for name in SYSTEM_FOLDER_WELL_KNOWN_NAMES:
            try:
                resp = await client.get(
                    f"{GRAPH_BASE}/me/mailFolders/{name}",
                    headers=headers, params={"$select": "id"},
                )
                if resp.status_code == 200:
                    ids.add(resp.json()["id"])
                else:
                    logger.debug("system folder %s lookup returned %d", name, resp.status_code)
            except Exception as exc:
                logger.debug("system folder %s lookup failed: %s", name, exc)
    return ids


async def list_messages_since(
    *,
    access_token: str,
    since: datetime,
    max_messages: int = 1000,
) -> list[Message]:
    """Pull mailbox messages from across all folders received >= ``since``.

    Scans the entire mailbox (Inbox + Archive + custom folders + subfolders)
    so threads that have already been filed still feed the agenda. Excludes
    Drafts, SentItems, DeletedItems, JunkEmail, and Outbox by parentFolderId.
    Returns up to ``max_messages`` messages, newest first.
    """
    excluded = await _get_excluded_folder_ids(access_token=access_token)

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Prefer": 'outlook.body-content-type="text"',
    }
    # Push the system-folder exclusion server-side so Graph doesn't return
    # ~5x as many messages (most of a typical mailbox lives in Sent / Junk /
    # Deleted). Multiple parentFolderId clauses combined with `and` is the
    # only portable way to express "not in {ids}" — OData has no IN op.
    filter_clauses = [f"receivedDateTime ge {_iso(since)}"]
    for fid in sorted(excluded):
        filter_clauses.append(f"parentFolderId ne '{fid}'")
    params = {
        "$top": "100",
        "$filter": " and ".join(filter_clauses),
        "$orderby": "receivedDateTime desc",
        "$select": (
            "id,subject,from,toRecipients,ccRecipients,receivedDateTime,"
            "bodyPreview,body,hasAttachments,importance,isRead,webLink,"
            "conversationId,parentFolderId"
        ),
    }
    url = f"{GRAPH_BASE}/me/messages"

    out: list[Message] = []
    async with httpx.AsyncClient(timeout=60) as client:
        while url and len(out) < max_messages:
            resp = await client.get(url, headers=headers, params=params if "$filter" in (params or {}) else None)
            resp.raise_for_status()
            payload = resp.json()
            for item in payload.get("value", []):
                # Belt-and-suspenders: still drop anything in excluded folders
                # in case Graph returned it (some tenants ignore $filter on
                # parentFolderId for shared folders).
                if item.get("parentFolderId") in excluded:
                    continue
                out.append(_to_message(item))
                if len(out) >= max_messages:
                    break
            url = payload.get("@odata.nextLink")
            params = None

    logger.info(
        "Fetched %d messages across folders since %s",
        len(out), since.isoformat(),
    )
    return out


def _to_message(item: dict) -> Message:
    sender = item.get("from", {}).get("emailAddress", {}) or {}
    return Message(
        id=item["id"],
        subject=item.get("subject") or "(no subject)",
        sender=sender.get("name", ""),
        sender_email=sender.get("address", ""),
        received_at=_parse_dt(item["receivedDateTime"]),
        importance=item.get("importance", "normal"),
        is_read=bool(item.get("isRead", False)),
        web_link=item.get("webLink", ""),
        preview=item.get("bodyPreview", ""),
        body_text=(item.get("body") or {}).get("content", "") or "",
        to_recipients=[
            r["emailAddress"]["address"]
            for r in item.get("toRecipients", []) if r.get("emailAddress")
        ],
        cc_recipients=[
            r["emailAddress"]["address"]
            for r in item.get("ccRecipients", []) if r.get("emailAddress")
        ],
        has_attachments=bool(item.get("hasAttachments", False)),
        conversation_id=item.get("conversationId", ""),
    )


async def list_sent_messages_since(
    *,
    access_token: str,
    since: datetime,
    max_messages: int = 200,
) -> list[Message]:
    """Pull messages from the Sent Items folder, newest first.

    Same shape as ``list_messages_since`` but reads SentItems. Useful for
    detecting promises the user made to other people during the week.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Prefer": 'outlook.body-content-type="text"',
    }
    params = {
        "$top": "100",
        "$filter": f"sentDateTime ge {_iso(since)}",
        "$orderby": "sentDateTime desc",
        "$select": (
            "id,subject,from,toRecipients,ccRecipients,receivedDateTime,sentDateTime,"
            "bodyPreview,body,hasAttachments,importance,isRead,webLink,conversationId"
        ),
    }
    url = f"{GRAPH_BASE}/me/mailFolders/SentItems/messages"
    out: list[Message] = []
    async with httpx.AsyncClient(timeout=60) as client:
        while url and len(out) < max_messages:
            resp = await client.get(
                url, headers=headers,
                params=params if "$filter" in (params or {}) else None,
            )
            resp.raise_for_status()
            payload = resp.json()
            for item in payload.get("value", []):
                # sentDateTime is the relevant timestamp for sent mail; fall
                # back to receivedDateTime if SentItems is being weird.
                if not item.get("receivedDateTime") and item.get("sentDateTime"):
                    item["receivedDateTime"] = item["sentDateTime"]
                out.append(_to_message(item))
                if len(out) >= max_messages:
                    break
            url = payload.get("@odata.nextLink")
            params = None
    logger.info("Fetched %d sent messages since %s", len(out), since.isoformat())
    return out


async def fetch_attachments(
    *,
    access_token: str,
    message_id: str,
    max_bytes: int = 10 * 1024 * 1024,
) -> list[Attachment]:
    """Fetch file attachments for a single message. Skips item attachments and
    anything larger than ``max_bytes``."""
    url = f"{GRAPH_BASE}/me/messages/{message_id}/attachments"
    headers = {"Authorization": f"Bearer {access_token}"}

    out: list[Attachment] = []
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        for att in resp.json().get("value", []):
            if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
                continue
            size = int(att.get("size", 0))
            if size > max_bytes:
                logger.info("Skipping oversized attachment %s (%d bytes)", att.get("name"), size)
                continue
            content = att.get("contentBytes")
            if not content:
                continue
            out.append(Attachment(
                name=att.get("name", "attachment"),
                content_type=att.get("contentType", "application/octet-stream"),
                size=size,
                content_bytes=base64.b64decode(content),
            ))
    return out


def default_since(lookback_days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=lookback_days)
