"""EMIS Lambda entry point — orchestrates the weekly/midweek/Friday run.

The same Lambda is invoked by three EventBridge rules with different
``mode`` values: ``monday``, ``wednesday``, ``friday``.

Pipeline (per run):
    1. OAuth refresh → access token (rotate refresh token if it changed)
    2. Load VIP + blocklist from S3
    3. Fetch inbox mail + sent mail + calendar events for the relevant window
    4. Filter mail (drop blocklist, keep VIP) and group into threads
    5. Extract attachments
    6. Load prior 4 weeks of agendas from S3 for cross-week memory
    7. Build agenda via Claude (mode-aware prompt, prompt-cached system)
    8. Render Markdown + PDF
    9. Persist all artifacts to S3
   10. Side effects:
        - SES email
        - Create / update OneDrive Markdown + PDF
        - Create Microsoft To Do tasks for action items (dedup)
        - Create / update calendar event with agenda in the body
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from . import extract
from .agenda.briefs import build_briefs
from .agenda.builder import build_agenda
from .agenda.filters import apply_filters, load_blocklist, load_vip
from .agenda.memory import load_prior_agendas
from .agenda.threading import group_into_threads
from .config import load_config
from .email.dashboard import render_dashboard_html
from .agenda.retrospective import build_retrospective
from .email.sender import (
    render_briefs_html, render_briefs_text, render_html,
    render_retrospective_html, render_retrospective_text,
    render_text, send_via_ses,
)
from .export import markdown as md_export
from .graph import auth as graph_auth
from .graph import calendar as graph_calendar
from .graph import onedrive as graph_onedrive
from .graph import todo as graph_todo
from .graph.mail import (
    create_draft_reply, default_since, fetch_attachments, find_message_in_thread,
    list_messages_since, list_sent_messages_since,
)
from .nudge_drafts import generate_nudge_bodies
from .snooze import (
    DoneRecord, active_snoozes, load_closures, prune_closures, save_closures,
)
from .state import store
from .telemetry import (
    current_month_cost_for_user,
    load_runs, record_run, render_telemetry_html, summarize_last_n_days,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("emis")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Entry point invoked by EventBridge or the Coordinator Lambda.

    Event shape::

        {"mode": "monday" | "wednesday" | "friday" | "morning",
         "user_id": "..." }  # optional — multi-tenant fan-out passes this

    When ``user_id`` is absent, falls back to the legacy single-user secret
    at GraphSecretId. When present, loads that user's record from the Users
    table and operates entirely under per-user S3 / DDB state.
    """
    mode = (event or {}).get("mode") or os.getenv("MODE", "monday")
    user_id = (event or {}).get("user_id") or None
    if mode == "morning":
        return asyncio.run(_run_briefs(user_id=user_id))
    return asyncio.run(_run(mode, user_id=user_id))


async def _run(mode: str, user_id: str | None = None) -> dict[str, Any]:
    cfg = load_config()
    now = datetime.now(timezone.utc)
    since = default_since(cfg.lookback_days)

    # Multi-tenant: if invoked with a user_id, load that user's enrollment
    # record and override the relevant config fields (refresh token, sender
    # + recipient email, channels, categories). State storage and token
    # rotation both target DDB for these invocations.
    user = None
    if user_id:
        from .users import load_user as _load_user
        user = _load_user(user_id)
        if user is None:
            logger.error("user_id=%r not found in Users table", user_id)
            return {"status": "user_not_found", "user_id": user_id}
        cfg.graph_refresh_token = user.refresh_token
        cfg.agenda_sender = user.sender_email or user.email
        cfg.agenda_recipient = user.email
        logger.info("per-user run: user_id=%s email=%s mode=%s",
                    user_id, user.email, mode)

        # Monthly cost cap enforcement. If the user has set a cap and this
        # month's spend already meets/exceeds it, skip generation entirely
        # and send a short notice instead.
        cap = user.monthly_cost_cap_usd or 0
        if cap > 0 and cfg.state_bucket:
            try:
                runs_so_far = load_runs(cfg.state_bucket)
                current_spend = current_month_cost_for_user(runs_so_far, user_id, now)
                if current_spend >= cap:
                    logger.warning(
                        "user %s exceeded monthly cap ($%.2f >= $%d); skipping",
                        user_id, current_spend, cap,
                    )
                    try:
                        send_via_ses(
                            sender=cfg.agenda_sender,
                            recipient=cfg.agenda_recipient,
                            subject=f"EMIS — monthly cap reached (${cap})",
                            html=(
                                f"<p>Your {mode.title()} agenda was skipped because this "
                                f"month's Bedrock spend (${current_spend:.2f}) has met "
                                f"your ${cap}/month cap.</p>"
                                f"<p>The cap resets on the 1st. You can adjust it in "
                                f"<a href='{cfg.web_ui_url}settings'>settings</a>.</p>"
                            ),
                            text=(
                                f"Agenda skipped — monthly cap reached.\n"
                                f"Current month: ${current_spend:.2f} / ${cap}.\n"
                                f"Adjust the cap at {cfg.web_ui_url}settings"
                            ),
                        )
                    except Exception as exc:
                        logger.warning("cap notice SES failed: %s", exc)
                    from .users import record_run_outcome as _record
                    _record(user_id, ok=False, error=f"monthly cap ${cap} reached")
                    return {
                        "status": "skipped_cap_reached",
                        "user_id": user_id, "mode": mode,
                        "current_spend": current_spend, "cap": cap,
                    }
            except Exception as exc:
                logger.warning("cost cap check failed: %s", exc)

    # 1. Auth — refresh-token exchange. If Microsoft says the token is
    # invalid (user changed password, revoked consent, etc.), mark the
    # multi-tenant user as needs_reauth so future scheduled runs skip them
    # until they sign in again via /login. Solo legacy path just raises.
    try:
        tokens = await graph_auth.exchange_refresh_token(
            tenant_id=cfg.graph_tenant_id,
            client_id=cfg.graph_client_id,
            client_secret=cfg.graph_client_secret,
            refresh_token=cfg.graph_refresh_token,
        )
    except Exception as exc:
        msg = str(exc)
        if user_id and ("invalid_grant" in msg or "AADSTS" in msg or "401" in msg or "400" in msg):
            logger.warning("user %s token invalid, marking needs_reauth: %s", user_id, msg[:200])
            try:
                from .users import update_settings as _us
                _us(user_id, status="needs_reauth")
                from .users import record_run_outcome as _record
                _record(user_id, ok=False, error=f"token expired/revoked: {msg[:200]}")
                try:
                    send_via_ses(
                        sender=cfg.agenda_sender,
                        recipient=cfg.agenda_recipient,
                        subject="EMIS — please sign in again",
                        html=(
                            f"<p>Your EMIS access to Microsoft 365 has expired or "
                            f"been revoked (password change, consent revocation, etc.).</p>"
                            f"<p>Future agendas are paused until you "
                            f"<a href='{cfg.web_ui_url}login'>sign in again</a>.</p>"
                        ),
                        text=(
                            f"EMIS — please sign in again.\n"
                            f"Microsoft 365 access expired or was revoked.\n"
                            f"Sign in: {cfg.web_ui_url}login"
                        ),
                    )
                except Exception as ses_exc:
                    logger.warning("reauth notice SES failed: %s", ses_exc)
            except Exception as inner_exc:
                logger.warning("marking needs_reauth failed: %s", inner_exc)
            return {"status": "token_invalid", "user_id": user_id, "mode": mode}
        raise
    if not cfg.dry_run and tokens.refresh_token != cfg.graph_refresh_token:
        if user_id:
            from .users import update_refresh_token as _update_token
            _update_token(user_id, tokens.refresh_token)
        else:
            graph_auth.rotate_refresh_token_secret(
                os.environ["GRAPH_SECRET_ID"], tokens.refresh_token
            )
        # Keep the lru_cached Config in sync so the next invocation in this
        # warm container doesn't reuse the stale token.
        cfg.graph_refresh_token = tokens.refresh_token

    # 2. Filters from S3
    vip = load_vip(cfg.state_bucket) if cfg.state_bucket else []
    blocklist = load_blocklist(cfg.state_bucket) if cfg.state_bucket else []

    # 3. Fetch in parallel
    cal_start, cal_end = _calendar_window(mode, now)
    messages, sent, calendar_events = await asyncio.gather(
        list_messages_since(
            access_token=tokens.access_token, since=since,
            max_messages=cfg.max_messages,
        ),
        list_sent_messages_since(
            access_token=tokens.access_token, since=since,
            max_messages=cfg.max_sent_messages,
        ),
        graph_calendar.list_events_in_range(
            access_token=tokens.access_token, start=cal_start, end=cal_end,
        ),
    )
    logger.info(
        "fetched: inbox=%d sent=%d calendar=%d",
        len(messages), len(sent), len(calendar_events),
    )

    # 4. Filter + thread
    filtered, dropped = apply_filters(messages, vip, blocklist)
    threads = group_into_threads(filtered, vip)

    # 5. Attachments
    attachment_texts = await _extract_all_attachments(
        access_token=tokens.access_token,
        messages=filtered,
        bucket=cfg.state_bucket,
        week_start=since,
        max_bytes=cfg.max_attachment_bytes,
        dry_run=cfg.dry_run,
        onedrive_folder=cfg.onedrive_folder,
        mirror_to_onedrive=cfg.upload_to_onedrive,
        user_id=user_id,
    )

    # 6. Memory — anchor lookback on `now` so weeks_back=1 is last week,
    # not two weeks ago (since = now - lookback_days).
    prior = (
        load_prior_agendas(cfg.state_bucket, now, user_id=user_id)
        if cfg.state_bucket else []
    )

    # 6b. Closures — load existing state, two-way sync from Microsoft To Do,
    # prune very-old records, then pass the active set to the agenda prompt.
    closures = prune_closures(load_closures(cfg.state_bucket, user_id=user_id), now)
    if cfg.create_todo_tasks and cfg.state_bucket:
        try:
            todo_list_id = await graph_todo.ensure_list(
                tokens.access_token, cfg.todo_list_name,
            )
            existing_todo_ids = {d.source_id for d in closures.done if d.source == "todo_sync"}
            completed = await graph_todo.list_completed_tasks(
                tokens.access_token, todo_list_id,
            )
            ts = now.isoformat()
            new_count = 0
            for task in completed:
                tid = task["id"]
                if tid in existing_todo_ids:
                    continue
                completed_at = (task.get("completedDateTime") or {}).get("dateTime") or ts
                closures.done.append(DoneRecord(
                    item_match=task.get("title", ""),
                    completed_at=completed_at,
                    source="todo_sync",
                    source_id=tid,
                ))
                new_count += 1
            if new_count:
                logger.info("synced %d completed To Do tasks into closures", new_count)
        except Exception as exc:
            logger.warning("To Do completion sync failed: %s", exc)

    if cfg.state_bucket and not cfg.dry_run:
        save_closures(cfg.state_bucket, closures, user_id=user_id)

    closures_for_prompt = {
        "snoozes": [s.to_dict() for s in active_snoozes(closures, now)],
        "done": [d.to_dict() for d in closures.done],
        "drops": [d.to_dict() for d in closures.drops],
    }

    # 6c. User-supplied dashboard extras: free-text notes per item, and pins
    # that force items to surface as priorities. Best-effort; missing files
    # produce empty dict/list.
    try:
        user_notes = store.load_notes(cfg.state_bucket, user_id=user_id) if cfg.state_bucket else {}
    except Exception as exc:
        logger.warning("loading user_notes failed: %s", exc)
        user_notes = {}
    try:
        user_pins = store.load_pins(cfg.state_bucket, user_id=user_id) if cfg.state_bucket else []
    except Exception as exc:
        logger.warning("loading user_pins failed: %s", exc)
        user_pins = []

    # 7. Build the agenda
    result = build_agenda(
        mode=mode,
        threads=threads,
        sent_messages=sent,
        calendar_events=calendar_events,
        prior_agendas=prior,
        attachment_texts=attachment_texts,
        # Frame the agenda by the FORWARD calendar window (today → look-ahead),
        # not the backward mail-lookback `since`. `since` still scopes which
        # emails we ingest for context; it must not define the agenda period,
        # or the agenda reads as "the prior week" instead of the days ahead.
        week_start=cal_start,
        week_end=cal_end,
        now=now,
        api_key=cfg.anthropic_api_key,
        model=cfg.anthropic_model,
        aws_region=cfg.aws_region,
        closures=closures_for_prompt,
        user_notes=user_notes,
        user_pins=user_pins,
    )

    # 9. Persist (was step 9 in the docstring; renders below)
    if cfg.state_bucket and not cfg.dry_run:
        store.save_agenda(cfg.state_bucket, since, mode, result.agenda, user_id=user_id)

    # 8. Render
    # Record this run's cost. Telemetry is best-effort — never blocks the
    # main pipeline.
    try:
        record_run(
            bucket=cfg.state_bucket, mode=mode,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            now=now,
            user_id=user_id or "",
        )
    except Exception as exc:
        logger.warning("telemetry record failed: %s", exc)

    html = render_html(
        result.agenda, since, now, mode=mode,
        web_ui_url=cfg.web_ui_url, web_ui_token=cfg.web_ui_token,
    )
    # Friday gets two extras prepended: a retrospective ("what landed/slipped")
    # and a 7-day telemetry footer. Both are best-effort.
    retro_html = ""
    retro_text = ""
    if mode == "friday" and cfg.state_bucket:
        try:
            iso = since.isocalendar()
            week_iso = f"{iso.year:04d}-W{iso.week:02d}"
            monday_agenda = store.load_agenda(cfg.state_bucket, week_iso, "monday", user_id=user_id)
            retro = build_retrospective(
                monday_agenda=monday_agenda,
                friday_agenda=result.agenda,
                closures=closures_for_prompt,
                monday_iso=since.isoformat(),
            )
            retro_html = render_retrospective_html(retro)
            retro_text = render_retrospective_text(retro)
            if retro_html:
                # Inject right after the dashboard banner (or h1 if no banner).
                html = html.replace(
                    "<h1 style=\"margin-bottom:4px\">",
                    f"{retro_html}<h1 style=\"margin-bottom:4px\">",
                    1,
                )
        except Exception as exc:
            logger.warning("retrospective failed: %s", exc)
        try:
            summary = summarize_last_n_days(load_runs(cfg.state_bucket), days=7, now=now)
            footer = render_telemetry_html(summary)
            if footer:
                html = html.replace("</body></html>", f"{footer}</body></html>")
        except Exception as exc:
            logger.warning("telemetry summary failed: %s", exc)
    text = render_text(result.agenda, since, now, mode=mode)
    if retro_text:
        text = retro_text + "\n" + text
    md_text = md_export.render(result.agenda, since, now, mode=mode)
    try:
        from .export import pdf as pdf_export  # lazy: avoids fpdf2 import at module load
        pdf_bytes = pdf_export.render(result.agenda, since, now, mode=mode)
    except Exception as exc:
        logger.warning("PDF render failed: %s", exc)
        pdf_bytes = None

    if cfg.state_bucket and not cfg.dry_run:
        store.save_artifact(cfg.state_bucket, since, f"agenda.{mode}.md", md_text.encode("utf-8"), "text/markdown", user_id=user_id)
        store.save_artifact(cfg.state_bucket, since, f"agenda.{mode}.html", html.encode("utf-8"), "text/html; charset=utf-8", user_id=user_id)
        if pdf_bytes:
            store.save_artifact(cfg.state_bucket, since, f"agenda.{mode}.pdf", pdf_bytes, "application/pdf", user_id=user_id)

    subject = _email_subject(mode, now)

    if cfg.dry_run:
        dashboard_html = render_dashboard_html(
            result.agenda, since, now, mode=mode,
            closures={
                "snoozes": [s.to_dict() for s in closures.snoozes],
                "done": [d.to_dict() for d in closures.done],
                "drops": [d.to_dict() for d in closures.drops],
            },
            prior_agendas=prior,
        )
        previews = _write_previews(
            name=f"agenda.{mode}",
            html=html, text=text, md=md_text, pdf_bytes=pdf_bytes,
            dashboard=dashboard_html,
        )
        if previews:
            logger.info("wrote previews: %s", ", ".join(previews.values()))
        print(text)
        return {
            "status": "dry_run", "mode": mode,
            "tokens": _token_dict(result),
            "previews": previews,
        }

    # 10. Side effects — each is best-effort so one failure (e.g. SES sandbox,
    # OneDrive 5xx) doesn't block the rest.
    side_effects: dict[str, Any] = {}

    try:
        side_effects["ses"] = send_via_ses(
            sender=cfg.agenda_sender, recipient=cfg.agenda_recipient,
            subject=subject, html=html, text=text,
        ).get("MessageId")
    except Exception as exc:
        logger.warning("SES send failed: %s", exc)
        side_effects["ses"] = {"error": str(exc)}

    if cfg.upload_to_onedrive:
        try:
            side_effects["onedrive"] = await _upload_to_onedrive(
                access_token=tokens.access_token,
                folder=cfg.onedrive_folder,
                week_start=since, mode=mode,
                md=md_text, pdf_bytes=pdf_bytes,
            )
        except Exception as exc:
            logger.warning("OneDrive upload failed: %s", exc)

    # Best-effort SMS via Twilio. In multi-tenant mode, only sends if the
    # user has "sms" in their channels list. In legacy mode, always attempts
    # (the secret existing in env governs whether it actually fires).
    sms_enabled = (user is None) or ("sms" in (user.channels or set()))
    if sms_enabled:
        try:
            from .sms import send_agenda_sms
            iso = since.isocalendar()
            week_iso = f"{iso.year:04d}-W{iso.week:02d}"
            if cfg.web_ui_url and cfg.web_ui_token:
                from urllib.parse import urlencode
                qs = urlencode({
                    "token": cfg.web_ui_token, "week": week_iso, "mode": mode,
                })
                dashboard_url = f"{cfg.web_ui_url.rstrip('/')}/?{qs}"
            else:
                dashboard_url = ""
            side_effects["sms"] = send_agenda_sms(
                agenda=result.agenda, mode=mode, dashboard_url=dashboard_url,
            )
        except Exception as exc:
            logger.warning("SMS send failed: %s", exc)
    else:
        side_effects["sms"] = {"status": "skipped", "reason": "user_opted_out"}

    if cfg.create_todo_tasks and mode in ("monday", "wednesday"):
        # Best-effort: a Graph hiccup here must not crash the run, since
        # the agenda email has already been sent and async-retry would
        # cause duplicate sends.
        try:
            side_effects["todo"] = await graph_todo.sync_action_items(
                access_token=tokens.access_token,
                list_name=cfg.todo_list_name,
                action_items=[
                    a for a in result.agenda.get("action_items", [])
                    if a.get("status") in ("new", "carried_over")
                    and a.get("owner", "").lower() == "you"
                ],
            )
        except Exception as exc:
            logger.warning("To Do sync_action_items failed: %s", exc)

    if mode == "monday":
        # Auto-draft polite nudge replies for stale follow-ups so the user
        # can send with one click later. Stale = weeks_open >= 2 and not
        # resolved. Best-effort; failures don't block the rest.
        stale = [
            f for f in result.agenda.get("follow_ups", [])
            if f.get("weeks_open", 0) >= 2
            and f.get("status") != "resolved"
            and f.get("counterparty") and f.get("thread")
        ]
        if stale:
            try:
                bodies = generate_nudge_bodies(
                    follow_ups=stale,
                    api_key=cfg.anthropic_api_key,
                    model=cfg.anthropic_model,
                    aws_region=cfg.aws_region,
                )
                drafts_created = 0
                for f in stale:
                    body = bodies.get(f.get("thread", ""))
                    if not body:
                        continue
                    src = await find_message_in_thread(
                        access_token=tokens.access_token,
                        thread_subject=f.get("thread", ""),
                        counterparty=f.get("counterparty", ""),
                    )
                    if not src:
                        continue
                    try:
                        await create_draft_reply(
                            access_token=tokens.access_token,
                            original_message_id=src["id"],
                            body_text=body,
                        )
                        drafts_created += 1
                    except Exception as exc:
                        logger.warning("nudge draft creation failed: %s", exc)
                side_effects["nudge_drafts"] = drafts_created
                logger.info("nudge drafts created: %d", drafts_created)
            except Exception as exc:
                logger.warning("nudge body generation failed: %s", exc)
                side_effects["nudge_drafts"] = {"error": str(exc)}

    if cfg.create_calendar_event and mode == "monday":
        try:
            side_effects["calendar_event"] = await graph_calendar.upsert_weekly_plan_event(
                access_token=tokens.access_token, week_of=now, html_body=html,
            )
        except Exception as exc:
            logger.warning("calendar event upsert failed: %s", exc)
            side_effects["calendar_event"] = {"error": str(exc)}

    out = {
        "status": "sent", "mode": mode,
        "messages_processed": len(messages),
        "messages_dropped_by_filter": dropped,
        "threads": len(threads),
        "sent_messages": len(sent),
        "calendar_events": len(calendar_events),
        "attachments_extracted": sum(len(v) for v in attachment_texts.values()),
        "tokens": _token_dict(result),
        "side_effects": side_effects,
    }
    if user_id:
        try:
            from .users import record_run_outcome as _record
            _record(user_id, ok=True)
        except Exception as exc:
            logger.warning("record_run_outcome failed: %s", exc)
    return out


async def _run_briefs(user_id: str | None = None) -> dict[str, Any]:
    """Pre-meeting briefs flow — separate from the weekly agenda pipeline.

    Fetches today's calendar events, pulls 4 weeks of mail with each event's
    attendees, asks Claude for a short brief per meeting, and emails them.
    Skips silently when no meetings are on the calendar today.
    """
    cfg = load_config()
    if user_id:
        from .users import load_user as _load_user
        user = _load_user(user_id)
        if user is None:
            logger.error("user_id=%r not found", user_id)
            return {"status": "user_not_found", "user_id": user_id}
        cfg.graph_refresh_token = user.refresh_token
        cfg.agenda_sender = user.sender_email or user.email
        cfg.agenda_recipient = user.email
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    mail_since = now - timedelta(days=28)

    # 1. Auth
    tokens = await graph_auth.exchange_refresh_token(
        tenant_id=cfg.graph_tenant_id,
        client_id=cfg.graph_client_id,
        client_secret=cfg.graph_client_secret,
        refresh_token=cfg.graph_refresh_token,
    )
    if not cfg.dry_run and tokens.refresh_token != cfg.graph_refresh_token:
        graph_auth.rotate_refresh_token_secret(
            os.environ["GRAPH_SECRET_ID"], tokens.refresh_token
        )
        cfg.graph_refresh_token = tokens.refresh_token

    # 2. Today's calendar events
    events = await graph_calendar.list_events_in_range(
        access_token=tokens.access_token, start=today_start, end=today_end,
    )
    if not events:
        logger.info("morning: no meetings today, skipping")
        return {"status": "skipped", "mode": "morning", "reason": "no meetings"}

    # 3. Last 4 weeks of mail (folder-wide scan)
    messages = await list_messages_since(
        access_token=tokens.access_token,
        since=mail_since,
        max_messages=cfg.max_messages,
    )

    # 4. Filter (no thread grouping yet — briefs.py handles per-meeting grouping)
    vip = load_vip(cfg.state_bucket) if cfg.state_bucket else []
    blocklist = load_blocklist(cfg.state_bucket) if cfg.state_bucket else []
    filtered, dropped = apply_filters(messages, vip, blocklist)
    logger.info(
        "morning: meetings=%d mail=%d (dropped %d)",
        len(events), len(filtered), dropped,
    )

    # 5. Build briefs
    result = build_briefs(
        events=events,
        messages=filtered,
        self_email=cfg.agenda_recipient,
        now=now,
        api_key=cfg.anthropic_api_key,
        model=cfg.anthropic_model,
        aws_region=cfg.aws_region,
    )

    try:
        record_run(
            bucket=cfg.state_bucket, mode="morning",
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            now=now,
            user_id=user_id or "",
        )
    except Exception as exc:
        logger.warning("telemetry record failed: %s", exc)

    # 6. Render
    html = render_briefs_html(
        result.briefs, now,
        web_ui_url=cfg.web_ui_url, web_ui_token=cfg.web_ui_token,
    )
    text = render_briefs_text(result.briefs, now)
    subject = f"Today's briefs — {now.strftime('%a %b %d')}"

    if cfg.dry_run:
        previews = _write_previews(name="briefs.morning", html=html, text=text)
        if previews:
            logger.info("wrote previews: %s", ", ".join(previews.values()))
        print(text)
        return {
            "status": "dry_run", "mode": "morning",
            "meetings": len(events), "briefs": len(result.briefs),
            "tokens": {"input": result.input_tokens, "output": result.output_tokens},
            "previews": previews,
        }

    # 7. Send
    try:
        ses_resp = send_via_ses(
            sender=cfg.agenda_sender, recipient=cfg.agenda_recipient,
            subject=subject, html=html, text=text,
        )
        ses_id = ses_resp.get("MessageId")
    except Exception as exc:
        logger.warning("SES send failed: %s", exc)
        ses_id = {"error": str(exc)}

    return {
        "status": "sent", "mode": "morning",
        "meetings": len(events),
        "briefs": len(result.briefs),
        "tokens": {"input": result.input_tokens, "output": result.output_tokens},
        "ses": ses_id,
    }


def _write_previews(
    *,
    name: str,
    html: str,
    text: str,
    md: str | None = None,
    pdf_bytes: bytes | None = None,
    dashboard: str | None = None,
) -> dict[str, str]:
    """Write rendered outputs to ``$PREVIEW_DIR`` so they can be opened directly.

    Used during local dry-runs to let the user see exactly what each surface
    looks like — open ``agenda.monday.html`` in a browser, ``agenda.monday.pdf``
    in Preview, etc. No-op when ``PREVIEW_DIR`` is unset.
    """
    preview_dir = os.getenv("PREVIEW_DIR")
    if not preview_dir:
        return {}
    os.makedirs(preview_dir, exist_ok=True)
    written: dict[str, str] = {}
    paths = {
        "html": os.path.join(preview_dir, f"{name}.html"),
        "txt": os.path.join(preview_dir, f"{name}.txt"),
    }
    with open(paths["html"], "w", encoding="utf-8") as f:
        f.write(html)
    written["html"] = paths["html"]
    with open(paths["txt"], "w", encoding="utf-8") as f:
        f.write(text)
    written["txt"] = paths["txt"]
    if md is not None:
        md_path = os.path.join(preview_dir, f"{name}.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md)
        written["md"] = md_path
    if pdf_bytes is not None:
        pdf_path = os.path.join(preview_dir, f"{name}.pdf")
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)
        written["pdf"] = pdf_path
    if dashboard is not None:
        dash_path = os.path.join(preview_dir, f"{name}.dashboard.html")
        with open(dash_path, "w", encoding="utf-8") as f:
            f.write(dashboard)
        written["dashboard"] = dash_path
    return written


# ── Helpers ────────────────────────────────────────────────────────────────

# The agenda always covers a strict rolling window: today through the next
# N days. Bump this to change the span (e.g. 6 for "today + 5 more days").
AGENDA_WINDOW_DAYS = 5


def _calendar_window(mode: str, now: datetime) -> tuple[datetime, datetime]:
    """Strict rolling window — today 00:00 through today + AGENDA_WINDOW_DAYS,
    regardless of mode.

    Previously the window varied by mode (coming week / rest-of-week /
    next-week look-ahead), which made the agenda's date span jump around. The
    agenda should always look forward from *today* for a fixed number of days.
    `mode` still selects the prompt framing/tone, not the date range.
    """
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight, midnight + timedelta(days=AGENDA_WINDOW_DAYS)


def _email_subject(mode: str, now: datetime) -> str:
    return {
        "monday": f"Weekly agenda — week of {now.strftime('%b %d, %Y')}",
        "wednesday": f"Mid-week check-in — {now.strftime('%b %d, %Y')}",
        "friday": f"End-of-week recap — {now.strftime('%b %d, %Y')}",
    }.get(mode, f"EMIS agenda — {now.strftime('%b %d, %Y')}")


def _token_dict(result) -> dict[str, int]:
    return {
        "input": result.input_tokens, "output": result.output_tokens,
        "cache_read": result.cache_read_tokens,
        "cache_write": result.cache_creation_tokens,
    }


async def _extract_all_attachments(
    *, access_token, messages, bucket, week_start, max_bytes, dry_run,
    onedrive_folder: str = "", mirror_to_onedrive: bool = False,
    user_id: str | None = None,
):
    out: dict[str, list[tuple[str, str]]] = {}
    iso = week_start.isocalendar()
    week_label = f"{iso.year:04d}-W{iso.week:02d}"
    for msg in messages:
        if not msg.has_attachments:
            continue
        try:
            atts = await fetch_attachments(
                access_token=access_token, message_id=msg.id, max_bytes=max_bytes,
            )
        except Exception as exc:
            logger.warning("attachment fetch failed for %s: %s", msg.id, exc)
            continue
        extracted = []
        for att in atts:
            if bucket and not dry_run:
                try:
                    store.save_attachment(
                        bucket, week_start, msg.id, att.name,
                        att.content_bytes, att.content_type,
                        user_id=user_id,
                    )
                except Exception as exc:
                    logger.warning("S3 put failed for %s: %s", att.name, exc)
            if mirror_to_onedrive and onedrive_folder and not dry_run:
                # Mirror to OneDrive so attachments are browsable in Files
                # alongside the agenda PDF/Markdown — same per-week layout.
                try:
                    safe_name = att.name.replace("/", "_")
                    await graph_onedrive.upload_file(
                        access_token=access_token,
                        path=f"{onedrive_folder}/{week_label}/attachments/{msg.id}/{safe_name}",
                        data=att.content_bytes,
                        content_type=att.content_type,
                    )
                except Exception as exc:
                    logger.warning("OneDrive attachment upload failed for %s: %s", att.name, exc)
            text = extract.extract(att.name, att.content_type, att.content_bytes)
            if text:
                extracted.append((att.name, text))
        if extracted:
            out[msg.id] = extracted
    return out


async def _upload_to_onedrive(*, access_token, folder, week_start, mode, md, pdf_bytes):
    iso = week_start.isocalendar()
    base = f"{folder}/{iso.year:04d}-W{iso.week:02d}"
    results: dict[str, Any] = {}
    try:
        results["md"] = await graph_onedrive.upload_file(
            access_token=access_token,
            path=f"{base}/agenda.{mode}.md",
            data=md.encode("utf-8"),
            content_type="text/markdown",
        )
    except Exception as exc:
        logger.warning("OneDrive md upload failed: %s", exc)
        results["md"] = {"error": str(exc)}
    if pdf_bytes:
        try:
            results["pdf"] = await graph_onedrive.upload_file(
                access_token=access_token,
                path=f"{base}/agenda.{mode}.pdf",
                data=pdf_bytes,
                content_type="application/pdf",
            )
        except Exception as exc:
            logger.warning("OneDrive pdf upload failed: %s", exc)
            results["pdf"] = {"error": str(exc)}
    return results


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "monday"
    if mode == "morning":
        result = asyncio.run(_run_briefs())
    else:
        result = asyncio.run(_run(mode))
    print(result)
