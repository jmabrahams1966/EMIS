# Getting Started with EMIS

**EMIS** (Email Ingestor & Scheduler) reads your Microsoft 365 inbox, calendar, and sent mail three times a week and emails you a structured agenda — what you owe people, what you're waiting on, what's coming up, and what to prepare for. You don't need to install anything; everything happens in the cloud.

You'll get three weekly emails:

- **Monday 6:00 AM** — Weekly agenda. The full picture for the week ahead.
- **Wednesday 8:00 AM** — Mid-week check-in. What's slipping, what needs attention by Friday.
- **Friday 3:00 PM** — End-of-week recap. What landed, what carries into next week.

Plus an optional **morning brief** weekdays at 6:30 AM with prep notes for that day's meetings.

---

## Step 1: Enroll (one time, ~2 minutes)

1. Open this link in your browser: **https://2mzabtr4o3vecbuembrosc7k2y0bdolo.lambda-url.us-east-1.on.aws/**
2. Click **Enroll with Microsoft 365 →**
3. Sign in with your work email (`yourname@nybrainspine.com`)
4. Review the permissions screen — EMIS asks to read your mail, calendar, tasks, and OneDrive files. It does not send mail on your behalf and cannot modify your data on its own.
5. Click **Accept**. You'll see "You're enrolled!"

That's it. Your first agenda arrives on the next scheduled morning.

---

## Step 2: Read your first agenda

Your agenda email looks like a tidy summary. At the top there's a blue strip that says **View interactive dashboard →** — click it.

### What you can do on the dashboard

- **✓ done** next to an item — marks it complete; it won't reappear next week
- **1d · 1w · Mon** — snooze the item for 1 day, 1 week, or until next Monday
- **✕** — drop the item permanently; EMIS won't surface it again
- **📝 note** — add a free-text note (e.g., "told Sarah I'd send by Friday") that EMIS preserves in next week's agenda
- **📌 pin** — pin an item to the top so it stays visible even if EMIS would otherwise downgrade it
- **🖨 print view** — clean printable version for taking to a meeting
- **Filter chips** at the top of each section — show only `clinical`, `business`, `admin`, or `personal` items

The dashboard URL is the same every time. Bookmark it: **https://hl5bamdb5vdytk2p6mm527gyli0hxcrp.lambda-url.us-east-1.on.aws/**

You'll sign in with Microsoft 365 the first time. It remembers you for 7 days.

---

## Step 3: Adjust your settings

Click **Settings** in the top-right corner of the dashboard, or visit `/settings` directly.

| Setting | What it does |
|---|---|
| **Delivery channels** | Email is always on. SMS is currently disabled at the system level. |
| **Schedules** | Uncheck a mode (Monday / Wednesday / Friday / Morning) to stop receiving that run. |
| **Categories** | Pick which of `clinical / business / admin / personal` are relevant to your work. EMIS will surface those and de-emphasize the others. |
| **Monthly spend cap** | Optional safety limit. If your scheduled runs exceed `$X` of AI cost in a calendar month, EMIS skips the rest of the month and emails you a notice. Default: No cap. |

### Removing yourself

The **Danger zone** at the bottom has a "Delete my account" button. Clicking it removes your enrollment and your stored agendas. Your Microsoft 365 inbox is untouched.

---

## Privacy & security

- EMIS runs entirely in AWS infrastructure that's covered by the practice's existing Business Associate Agreement.
- Your inbox content stays inside that infrastructure — it's never sent to outside vendors.
- The AI summary uses **Claude (Anthropic)** running on **AWS Bedrock**, which Anthropic operates under HIPAA terms.
- Each enrolled user can only see their own dashboard. EMIS administrators can see *who* is enrolled and *how much* AI cost each person uses, but cannot read your agendas or your mail.

## Need help?

If something looks wrong or you don't get an expected email, contact **John (jma@nybrainspine.com)**.

If you ever forget the URLs:

- **Enroll**: https://2mzabtr4o3vecbuembrosc7k2y0bdolo.lambda-url.us-east-1.on.aws/
- **Dashboard**: https://hl5bamdb5vdytk2p6mm527gyli0hxcrp.lambda-url.us-east-1.on.aws/
