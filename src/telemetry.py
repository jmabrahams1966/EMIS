"""Per-run cost + status telemetry, accumulated in S3.

Each agenda or briefs run calls ``record_run`` with the input/output token
counts and mode. The store is a single rolling list at
``s3://<bucket>/state/telemetry.json`` (newest first; auto-pruned after
``RETAIN_DAYS``).

The Friday agenda email's footer summarizes the last 7 days from this
store — runs, cost, errors — so the user gets a weekly health check
without a separate Lambda.

Pricing baked in for Opus 4.7 / 4.8 ($5/M input, $25/M output). Adjust
``COST_PER_INPUT_TOKEN`` / ``COST_PER_OUTPUT_TOKEN`` if you switch to a
cheaper model.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger("emis.telemetry")

RETAIN_DAYS = 90
COST_PER_INPUT_TOKEN = 5.0 / 1_000_000   # $5 per million for Opus
COST_PER_OUTPUT_TOKEN = 25.0 / 1_000_000


@dataclass
class RunRecord:
    timestamp: str          # ISO timestamp
    mode: str               # "monday" / "wednesday" / "friday" / "morning" / "snooze_poll"
    input_tokens: int
    output_tokens: int
    cost_usd: float
    status: str             # "ok" / "error"
    error: str = ""         # populated when status=="error"
    user_id: str = ""       # multi-tenant attribution; "" for legacy / system runs


def _key() -> str:
    return "state/telemetry.json"


def _s3():
    return boto3.client("s3")


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return round(
        input_tokens * COST_PER_INPUT_TOKEN
        + output_tokens * COST_PER_OUTPUT_TOKEN,
        4,
    )


def load_runs(bucket: str) -> list[RunRecord]:
    if not bucket:
        return []
    try:
        obj = _s3().get_object(Bucket=bucket, Key=_key())
        raw = json.loads(obj["Body"].read().decode("utf-8"))
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return []
        raise
    # Backfill missing user_id on legacy records ("" treated as system).
    out: list[RunRecord] = []
    for r in raw:
        r.setdefault("user_id", "")
        try:
            out.append(RunRecord(**r))
        except TypeError:
            # Ignore extra fields from forward migrations.
            known = {f.name for f in __import__("dataclasses").fields(RunRecord)}
            out.append(RunRecord(**{k: v for k, v in r.items() if k in known}))
    return out


def save_runs(bucket: str, runs: list[RunRecord]) -> None:
    if not bucket:
        return
    data = json.dumps([asdict(r) for r in runs], indent=2)
    _s3().put_object(
        Bucket=bucket, Key=_key(),
        Body=data.encode("utf-8"), ContentType="application/json",
    )


def record_run(
    *,
    bucket: str,
    mode: str,
    input_tokens: int,
    output_tokens: int,
    status: str = "ok",
    error: str = "",
    now: datetime | None = None,
    user_id: str = "",
) -> RunRecord:
    """Append a run record to the rolling telemetry store. No-op if no bucket."""
    now = now or datetime.now(timezone.utc)
    rec = RunRecord(
        timestamp=now.isoformat(),
        mode=mode,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=_estimate_cost(input_tokens, output_tokens),
        status=status,
        error=error[:500],  # cap error text
        user_id=user_id,
    )
    if not bucket:
        return rec
    runs = load_runs(bucket)
    runs.insert(0, rec)
    # Prune anything older than RETAIN_DAYS.
    cutoff = (now - timedelta(days=RETAIN_DAYS)).isoformat()
    runs = [r for r in runs if r.timestamp >= cutoff]
    save_runs(bucket, runs)
    return rec


def current_month_cost_for_user(runs: list[RunRecord], user_id: str, now: datetime | None = None) -> float:
    """Sum this calendar month's Bedrock cost for one user."""
    now = now or datetime.now(timezone.utc)
    month_prefix = f"{now.year:04d}-{now.month:02d}-"
    total = 0.0
    for r in runs:
        if r.user_id != user_id:
            continue
        if not r.timestamp.startswith(month_prefix):
            continue
        total += r.cost_usd
    return round(total, 4)


def summarize_per_user(
    runs: list[RunRecord], days: int = 30, now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Return ``{user_id: {runs, cost_usd, errors, last_run}}`` for the window."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat()
    by_user: dict[str, dict[str, Any]] = {}
    for r in runs:
        if r.timestamp < cutoff:
            continue
        uid = r.user_id or "(system)"
        slot = by_user.setdefault(uid, {
            "runs": 0, "cost_usd": 0.0, "errors": 0, "last_run": "",
        })
        slot["runs"] += 1
        slot["cost_usd"] = round(slot["cost_usd"] + r.cost_usd, 4)
        if r.status != "ok":
            slot["errors"] += 1
        if r.timestamp > (slot["last_run"] or ""):
            slot["last_run"] = r.timestamp
    return by_user


def summarize_last_n_days(
    runs: list[RunRecord], days: int, now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate per-mode metrics over a rolling window."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat()
    recent = [r for r in runs if r.timestamp >= cutoff]
    by_mode: dict[str, dict[str, Any]] = {}
    for r in recent:
        slot = by_mode.setdefault(r.mode, {"runs": 0, "input": 0, "output": 0, "cost": 0.0, "errors": 0})
        slot["runs"] += 1
        slot["input"] += r.input_tokens
        slot["output"] += r.output_tokens
        slot["cost"] += r.cost_usd
        if r.status != "ok":
            slot["errors"] += 1
    return {
        "since": cutoff[:10],
        "until": now.date().isoformat(),
        "total_runs": len(recent),
        "total_cost_usd": round(sum(s["cost"] for s in by_mode.values()), 2),
        "total_errors": sum(s["errors"] for s in by_mode.values()),
        "by_mode": by_mode,
    }


def render_telemetry_html(summary: dict[str, Any]) -> str:
    """Tiny HTML footer for the Friday agenda email."""
    if not summary or not summary.get("total_runs"):
        return ""
    rows = []
    for mode, slot in sorted(summary["by_mode"].items()):
        rows.append(
            f"<li><strong>{mode}</strong>: {slot['runs']} run{'s' if slot['runs'] != 1 else ''}, "
            f"${slot['cost']:.2f}"
            + (f", {slot['errors']} error{'s' if slot['errors'] != 1 else ''}" if slot['errors'] else "")
            + "</li>"
        )
    return (
        f"<hr style='margin-top:24px;border:none;border-top:1px solid #eee'>"
        f"<div style='color:#888;font-size:11px;margin-top:12px'>"
        f"<div><strong>EMIS health, last 7 days</strong> "
        f"&middot; {summary['since']} to {summary['until']}</div>"
        f"<div>Total runs: {summary['total_runs']} &middot; "
        f"Total cost: ${summary['total_cost_usd']:.2f}"
        + (f" &middot; <span style='color:#c0392b'>Errors: {summary['total_errors']}</span>" if summary['total_errors'] else "")
        + f"</div>"
        f"<ul style='margin:6px 0;padding-left:20px'>{''.join(rows)}</ul>"
        f"</div>"
    )
