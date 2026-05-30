"""Attachment → plain-text dispatcher.

Each extractor returns a (possibly empty) string. Unknown content types
return an empty string so downstream summarization just skips them.
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import pdf, docx, xlsx, html

logger = logging.getLogger(__name__)


def extract(name: str, content_type: str, data: bytes, max_chars: int = 20_000) -> str:
    """Dispatch based on content type / file extension. Truncates output."""
    ct = (content_type or "").lower()
    suffix = Path(name).suffix.lower()

    try:
        if ct == "application/pdf" or suffix == ".pdf":
            text = pdf.extract(data)
        elif suffix in (".docx",) or "wordprocessingml" in ct:
            text = docx.extract(data)
        elif suffix in (".xlsx", ".xlsm") or "spreadsheetml" in ct:
            text = xlsx.extract(data)
        elif suffix in (".html", ".htm") or ct.startswith("text/html"):
            text = html.extract(data)
        elif ct.startswith("text/") or suffix in (".txt", ".csv", ".md"):
            text = data.decode("utf-8", errors="ignore")
        else:
            logger.info("Skipping attachment %s (%s) — no extractor", name, ct)
            return ""
    except Exception as exc:
        logger.warning("Extraction failed for %s: %s", name, exc)
        return ""

    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…[truncated]"
    return text
