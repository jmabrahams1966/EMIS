from __future__ import annotations

from bs4 import BeautifulSoup


def extract(data: bytes) -> str:
    soup = BeautifulSoup(data, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)
