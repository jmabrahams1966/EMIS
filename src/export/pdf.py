"""Render the agenda as a PDF using fpdf2 (pure Python, lightweight).

fpdf2's default Helvetica is latin-1 only — any em-dash, ellipsis, bullet,
or accented character outside latin-1 makes ``cell()`` raise. Rather than
ship a Unicode TTF (~700KB), we sanitize all strings on the way in: replace
common Unicode punctuation with ASCII equivalents and drop the rest.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fpdf import FPDF

_PAGE_W = 190  # A4 - margins

_UNICODE_REPLACEMENTS = {
    "—": "-",      # em-dash
    "–": "-",      # en-dash
    "…": "...",    # horizontal ellipsis
    "•": "*",      # bullet
    "↻": "(c)",    # clockwise open circle arrow (carried_over marker)
    "✓": "[ok]",   # check mark (resolved marker)
    "⚠": "(!)",    # warning sign (stale marker)
    "→": "->",     # rightwards arrow
    "←": "<-",     # leftwards arrow
    "“": '"',      # left double quote
    "”": '"',      # right double quote
    "‘": "'",      # left single quote
    "’": "'",      # right single quote
}


def _ascii_safe(text: str) -> str:
    """Best-effort sanitize a string for fpdf2's latin-1 Helvetica."""
    if not text:
        return text
    for k, v in _UNICODE_REPLACEMENTS.items():
        text = text.replace(k, v)
    # Strip anything else outside latin-1 rather than crashing.
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _sanitize(obj: Any) -> Any:
    """Recursively sanitize every string in an agenda dict / list."""
    if isinstance(obj, str):
        return _ascii_safe(obj)
    if isinstance(obj, list):
        return [_sanitize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    return obj


class _AgendaPDF(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(150)
        self.cell(0, 5, "EMIS - E-Mail Ingestor and Scheduler", align="R")
        self.ln(8)
        self.set_text_color(0)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(150)
        self.cell(0, 4, f"Page {self.page_no()}", align="C")


def _heading(pdf: FPDF, text: str, size: int = 14) -> None:
    pdf.set_font("Helvetica", "B", size)
    pdf.cell(0, 8, text)
    pdf.ln(8)


def _para(pdf: FPDF, text: str, size: int = 11) -> None:
    pdf.set_font("Helvetica", "", size)
    pdf.multi_cell(0, 5, text)
    pdf.ln(2)


def _bullet(pdf: FPDF, head: str, sub: str = "", link: str = "") -> None:
    pdf.set_font("Helvetica", "B", 10)
    pdf.multi_cell(_PAGE_W, 5, f"*  {head}", link=link or "")
    if sub:
        pdf.set_font("Helvetica", "", 10)
        pdf.set_x(pdf.get_x() + 6)
        pdf.multi_cell(_PAGE_W - 6, 5, sub)
    pdf.ln(1)


def render(agenda: dict[str, Any], week_start: datetime, week_end: datetime, mode: str) -> bytes:
    # Sanitize every string in the agenda once, up front. Cheaper than
    # wrapping every cell()/multi_cell() call and catches any non-latin-1
    # character Claude emitted (em-dashes, ellipses, arrows, accents).
    agenda = _sanitize(agenda)

    title = {
        "monday": "Weekly Agenda",
        "wednesday": "Mid-Week Check-in",
        "friday": "End-of-Week Recap",
    }.get(mode, "Agenda")

    pdf = _AgendaPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, title)
    pdf.ln(11)
    pdf.set_font("Helvetica", "I", 10)
    pdf.set_text_color(100)
    pdf.cell(0, 5, f"Week of {week_start.date()} - {week_end.date()}")
    pdf.ln(8)
    pdf.set_text_color(0)

    _para(pdf, agenda.get("week_summary", ""))

    _heading(pdf, "Priorities")
    for p in agenda.get("priorities", []):
        head = f"{p.get('title', '')}  [{p.get('urgency', 'medium')}]"
        sub = p.get("reason", "")
        if p.get("source_subject"):
            sub += f"   (source: {p['source_subject']})"
        _bullet(pdf, head, sub, link=p.get("web_link", ""))

    _heading(pdf, "Meetings")
    if not agenda.get("meetings"):
        _para(pdf, "(none)")
    for m in agenda.get("meetings", []):
        head = f"{m.get('subject', '')} - {m.get('when', '')}"
        sub_lines = [f"With: {m.get('participants', '')}",
                     f"Source: {m.get('source', '?')}"]
        if m.get("prep_notes"):
            sub_lines.append(f"Prep: {m['prep_notes']}")
        _bullet(pdf, head, "\n".join(sub_lines), link=m.get("web_link", ""))

    _heading(pdf, "Action items")
    for a in agenda.get("action_items", []):
        head = (f"{a.get('task', '')}   "
                f"[{a.get('owner', '?')}, due {a.get('due', '')}, "
                f"{a.get('urgency', 'medium')}, {a.get('status', 'new')}]")
        sub = f"Source: {a['source_subject']}" if a.get("source_subject") else ""
        _bullet(pdf, head, sub, link=a.get("web_link", ""))

    _heading(pdf, "Follow-ups")
    for f in agenda.get("follow_ups", []):
        head = f"{f.get('thread', '')} - {f.get('counterparty', '')}"
        weeks = f.get("weeks_open", 0)
        weeks_txt = f", open {weeks}w" if weeks else ""
        sub = f"{f.get('ask', '')}   ({f.get('status', 'new')}{weeks_txt})"
        _bullet(pdf, head, sub, link=f.get("web_link", ""))

    if agenda.get("promises_made"):
        _heading(pdf, "Promises you made")
        for p in agenda["promises_made"]:
            head = f"{p.get('commitment', '')}"
            sub = f"To {p.get('to', '')} by {p.get('by', '')}"
            if p.get("source_subject"):
                sub += f"   (source: {p['source_subject']})"
            _bullet(pdf, head, sub, link=p.get("web_link", ""))

    _heading(pdf, "FYI")
    for x in agenda.get("fyi", []):
        _bullet(pdf, x)

    return bytes(pdf.output())
