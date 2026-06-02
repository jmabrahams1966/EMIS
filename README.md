# EMIS — E-Mail Ingestor and Scheduler

Reads your Outlook / Microsoft 365 mailbox, sent items, and calendar three
times a week, summarizes with Claude, and delivers a structured agenda by
email + creates Microsoft To Do tasks + archives Markdown + PDF to OneDrive +
posts the agenda as a calendar event. Browse past weeks via a lightweight
web UI.

## How it runs

```
EventBridge (5 schedules)
  ├─ Mon 06:00 ET → weekly plan (priorities, meetings, action items, etc.)
  ├─ Wed 08:00 ET → mid-week check-in (what's slipping?)
  ├─ Fri 15:00 ET → end-of-week recap + look-ahead
  ├─ Daily 06:30 ET (Mon-Fri) → pre-meeting briefs for today's calendar
  └─ Every 30 min → snooze poller (parses agenda-email replies)

Lambda (one function, mode-aware)
  ├─ Graph: Mail.Read (all folders except Drafts/Sent/Outbox/Junk/Deleted), Calendars.ReadWrite, Tasks.ReadWrite, Files.ReadWrite
  ├─ Filter (VIP allowlist + automated-mail blocklist; both editable in S3)
  ├─ Thread grouping (by conversationId)
  ├─ Cross-week memory (last 4 weeks of agendas → status: new/carried_over/resolved/stale)
  ├─ Attachment text extraction (PDF / DOCX / XLSX / HTML)
  ├─ Claude: opus-4-7 via public API (json_schema output) or Bedrock (forced tool-use)
  └─ Side effects:
       ├─ SES — emails the agenda
       ├─ Microsoft To Do — creates one task per action item (deduped)
       ├─ Calendar — creates/updates the "EMIS Weekly Plan" event with agenda in body
       ├─ OneDrive — uploads agenda.{mode}.md and agenda.{mode}.pdf to /EMIS/{YYYY-WW}/
       └─ S3 — agenda.{mode}.json + attachments archived for cross-week memory

Web UI Lambda (Function URL, token-gated)
  └─ Browse past weeks at https://<function-url>/?token=…
```

## What you get each run

The agenda has seven sections (the schema is fixed across modes):

- **week_summary** — 2-4 sentence frame
- **priorities** — 3-5 ranked items with urgency (`high`/`medium`/`low`)
- **meetings** — calendar events + email-proposed times (each tagged `calendar` or `email`)
- **action_items** — owner, due date, ISO date if extractable, urgency, status
- **follow_ups** — what others owe you, with `weeks_open` count
- **promises_made** — what *you* committed to in sent mail this week
- **fyi** — awareness-only context

Items carry a `status`: `new` (this week), `carried_over` (open from a prior
week), `resolved` (recently closed — surfaced in `week_summary`), or `stale`
(open ≥ 3 weeks; candidate for dropping).

## One-time setup

1. **Register an Azure app** (Entra ID → App registrations → New registration).
   - Account type: *Accounts in any organizational directory and personal Microsoft accounts*
   - Redirect URI (public client / "Mobile and desktop applications"
     platform): `http://127.0.0.1:8765/callback` (the bootstrap script binds
     to `127.0.0.1`, and Entra treats `localhost` and `127.0.0.1` as distinct
     URIs — using the wrong one returns AADSTS900971).
   - API permissions → Microsoft Graph → Delegated:
     `Mail.Read`, `Calendars.ReadWrite`, `Tasks.ReadWrite`, `Files.ReadWrite`,
     `User.Read`, `offline_access`. Grant consent.
   - Note Application (client) ID and tenant. For personal MSA use `consumers`;
     work/school use your tenant GUID or `common`.

2. **Capture a refresh token locally**
   ```bash
   pip install -r requirements.txt
   export GRAPH_CLIENT_ID=...
   export GRAPH_TENANT_ID=common
   python scripts/bootstrap_oauth.py
   ```

3. **Push secrets to AWS Secrets Manager**
   ```bash
   aws secretsmanager create-secret --name emis/graph \
     --secret-string '{"client_id":"...","tenant_id":"common","refresh_token":"..."}'
   aws secretsmanager create-secret --name emis/anthropic \
     --secret-string '{"api_key":"sk-ant-..."}'
   ```

4. **Verify the SES sender** for the `From` address (e.g. `agenda@yourdomain.com`).

5. **Deploy**
   ```bash
   cd infrastructure
   sam build && sam deploy --guided
   ```
   You'll be prompted for the sender, recipient, `WebUiToken` (any long random
   string — pick something stronger than `password123`), and Anthropic model id
   (default: `claude-opus-4-7`).

6. **(Optional)** Edit the VIP and blocklist:
   ```bash
   # VIP — strict matching only (a false positive bypasses filtering):
   #   "alice@example.com"  → exact email match
   #   "@example.com"       → domain, including subdomains
   #   anything else        → ignored (substring matching on emails is unsafe)
   aws s3 cp - s3://<state-bucket>/config/vip_senders.json <<'EOF'
   ["ceo@example.com", "@board.example.com", "investor@vc.com"]
   EOF

   # Blocklist — case-insensitive substring match (a false positive just
   # drops a real email, so substring is fine here):
   aws s3 cp - s3://<state-bucket>/config/blocklist.json <<'EOF'
   ["no-reply@", "notifications@", "newsletter@", "@mailchimp.com"]
   EOF
   ```
   These are loaded fresh on every run — no redeploy needed.

## Replying to snooze

Hit Reply on any EMIS email and tell it which items to defer:

```
snooze the Costco DCF thread until next Monday
snooze horizon enrollment for 2 weeks
```

A second Lambda polls your inbox every 30 min, parses replies via Claude
(natural language — phrasing doesn't matter), and writes snoozes to
`s3://<state-bucket>/state/snoozes.json`. The next agenda run reads them
and suppresses matching items from priorities / action_items / follow_ups
until the `until` date passes. Snoozes auto-expire and are pruned after 90
days.

Drop / done / delegate aren't supported yet — only snooze.

## Web UI

After deploy, the `WebUiUrl` output is a Lambda Function URL. Access with:

    https://<random>.lambda-url.us-east-1.on.aws/?token=YOUR_WEB_UI_TOKEN

Lists past weeks, each with the three modes (Monday / Wednesday / Friday).
Click a week → click a mode → see the same HTML rendering the email used.

## Local dry-run

```bash
export GRAPH_CLIENT_ID=...
export GRAPH_TENANT_ID=common
export GRAPH_REFRESH_TOKEN=...
export ANTHROPIC_API_KEY=...
export AGENDA_RECIPIENT=you@yourdomain.com
export AGENDA_SENDER=agenda@yourdomain.com
export DRY_RUN=1                # print agenda, skip SES/To Do/Calendar/OneDrive
python -m src.handler monday    # or wednesday, friday, morning
```

To see what the formatted email and PDF actually look like (rather than just
the plaintext fallback printed to the terminal), set `PREVIEW_DIR`:

```bash
export PREVIEW_DIR=./preview
python -m src.handler monday

open preview/agenda.monday.html           # static email render
open preview/agenda.monday.dashboard.html # interactive dashboard (tabs + "+ Cal")
open preview/agenda.monday.pdf            # OneDrive archive
```

The dashboard is the interactive view: tabbed navigation (Week at a glance /
Priorities / Meetings / Action items / Follow-ups / Promises / FYI), every
dated item has a "+ Cal" button that downloads a one-event ICS so you can
add it to your calendar with one click, and links jump to the source Outlook
thread. It's the same HTML the deployed Web UI Lambda serves. The morning
briefs flow writes `briefs.morning.html` / `.txt` (no dashboard for briefs —
they're already short).

## Repo layout

```
src/
  handler.py              Lambda entry — orchestrates the full pipeline
  web_ui.py               Web UI Lambda — Function URL handler
  config.py               env + Secrets Manager loaders
  agenda/
    builder.py            Claude call (prompt-cached, adaptive thinking, json_schema)
    prompts.py            frozen system prompt + JSON schema + mode notes
    filters.py            VIP allowlist + blocklist (S3-backed)
    threading.py          group messages by conversationId
    memory.py             load + render last 4 weeks of agendas
  graph/
    auth.py               OAuth refresh-token exchange + rotation
    mail.py               list inbox + sent + attachments
    calendar.py           list events + upsert weekly-plan event
    todo.py               Microsoft To Do — ensure list, dedup-create tasks
    onedrive.py           upload Markdown + PDF
  extract/
    pdf.py docx.py xlsx.py html.py  → attachment text extractors
    __init__.py           dispatcher
  export/
    markdown.py           Markdown render
    pdf.py                PDF render (fpdf2)
  email/
    sender.py             HTML/plaintext render + SES send
  state/
    store.py              S3 layout: agendas, artifacts, attachments
scripts/
  bootstrap_oauth.py      one-time OAuth code+PKCE flow
infrastructure/
  template.yaml           SAM: 1 bucket, 2 Lambdas, 3 schedules, Function URL
```

## Cost notes

- A typical run is ~30-150K input tokens (inbox + threads + calendar + memory)
  and ~2-5K output. On opus-4-7 at $5/M input / $25/M output that's roughly
  $0.20-$0.80 per run, $0.60-$2.50/week across the three runs.
- Prompt caching is not used: the system prompt is ~600 tokens (well under
  Opus 4.7's 4096-token cache-write minimum) and runs are 48+ hours apart
  (longer than any ephemeral cache TTL). Adding `cache_control` would just
  pay the write premium for no reads.
- SES outbound: 1 email per run × 3 runs/week.
- Lambda: ≤ 15 min per run; small.
- S3: a few MB per week including attachments. Lifecycle expires after 365d.
