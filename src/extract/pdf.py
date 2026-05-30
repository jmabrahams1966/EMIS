from __future__ import annotations

from io import BytesIO

from pypdf import PdfReader


def extract(data: bytes) -> str:
    reader = PdfReader(BytesIO(data))
    parts: list[str] = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(p.strip() for p in parts if p.strip())
