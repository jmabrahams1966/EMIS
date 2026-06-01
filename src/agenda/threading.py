"""Group Graph messages by conversation thread.

Outlook surfaces every reply as a separate message. We collapse them into
threads keyed by ``conversationId`` so Claude sees one summary per topic
instead of N reply chains, cutting tokens and improving signal.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from ..graph.mail import Message
from .filters import is_vip

logger = logging.getLogger(__name__)


@dataclass
class Thread:
    conversation_id: str
    subject: str
    participants: list[str] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    has_attachments: bool = False
    latest_received: datetime | None = None
    is_vip: bool = False  # set by the caller if any participant is a VIP


def group_into_threads(
    messages: Iterable[Message],
    vip_patterns: list[str] | None = None,
) -> list[Thread]:
    """Group messages by ``conversationId``, newest thread first.

    A thread inherits VIP status if any message in it is from a VIP sender.
    """
    vip_patterns = vip_patterns or []
    buckets: OrderedDict[str, Thread] = OrderedDict()

    # Insert oldest-first so messages list is chronological; sort threads at end.
    for msg in sorted(messages, key=lambda m: m.received_at):
        key = msg.conversation_id or f"single::{msg.id}"
        if key not in buckets:
            buckets[key] = Thread(
                conversation_id=key, subject=msg.subject,
            )
        t = buckets[key]
        t.messages.append(msg)
        t.has_attachments = t.has_attachments or msg.has_attachments
        t.latest_received = msg.received_at
        for p in [msg.sender_email, *msg.to_recipients, *msg.cc_recipients]:
            if p and p not in t.participants:
                t.participants.append(p)
        if vip_patterns and is_vip(msg.sender_email, vip_patterns):
            t.is_vip = True

    threads = sorted(buckets.values(), key=lambda t: t.latest_received or datetime.min, reverse=True)
    logger.info("grouped %d messages into %d threads", sum(len(t.messages) for t in threads), len(threads))
    return threads
