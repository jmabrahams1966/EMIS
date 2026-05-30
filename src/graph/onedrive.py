"""Upload agenda artifacts (Markdown + PDF) to OneDrive."""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


async def upload_file(
    *,
    access_token: str,
    path: str,
    data: bytes,
    content_type: str = "application/octet-stream",
) -> dict[str, Any]:
    """PUT bytes to OneDrive at ``/me/drive/root:/{path}:/content``.

    ``path`` should be a relative path like ``EMIS/2026-W22/agenda.md``.
    Parent folders are auto-created. Existing files are overwritten.
    """
    url = f"{GRAPH_BASE}/me/drive/root:/{path}:/content"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": content_type,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.put(url, content=data, headers=headers)
        resp.raise_for_status()
        result = resp.json()
    logger.info("OneDrive: uploaded %s (%d bytes)", path, len(data))
    return {"web_url": result.get("webUrl"), "id": result.get("id"), "path": path}
