# Daily Intelligence Brief

Fetches emails from your two alumni inboxes each day, filters for AI, national security, Russia/Ukraine, China, and related topics, summarizes them with Claude, and saves a dated Markdown file to `output/`. Relevant emails are marked as read.

## What you get

A file like `output/brief_2026-05-24.md` structured as:

```
# Daily Intelligence Brief — May 24, 2026

*Generated 08:15 | 7 relevant emails*

## Artificial Intelligence & Emerging Technology

### [Subject line]
**From:** Sender Name | **Account:** ND Alumni | **Date:** May 24, 2026

*Why this matters: [one strategic sentence]*

[4–7 sentence analyst-quality summary]

[Open email →](https://mail.google.com/...)

---

## Russia, Ukraine & Eastern Europe
...
```

---

## Setup (one time)

There are four things to configure: Python packages, a Google Cloud app (Gmail), a Microsoft Azure app (JHU), and your Anthropic API key.

### Install dependencies

```bash
cd "/Users/yourname/Library/CloudStorage/GoogleDrive-your.email@example.com/My Drive/Sean/Code/ai_code/daily_brief"
pip3 install -r requirements.txt
```

---

### Step 1 — Gmail API (Notre Dame, alumni.nd.edu)

This gives the script permission to read your ND inbox.

1. Go to [https://console.cloud.google.com](https://console.cloud.google.com)
2. Click the project dropdown at the top → **New Project** → name it `daily-brief` → **Create**
3. In the left sidebar: **APIs & Services → Library** → search **Gmail API** → click it → **Enable**
4. In the left sidebar: **APIs & Services → OAuth consent screen** (may appear as **Google Auth Platform** in the new UI)
   - Choose **External** → **Create** (or **Get Started**)
   - Fill in App name (`Daily Brief`), your email for support/contact → **Save and Continue**
   - On "Scopes": skip → **Save and Continue**
   - On "Test users": click **Add Users** → enter your `alumni.nd.edu` address → **Save and Continue**
   - Click **Back to Dashboard**
5. In the left sidebar: **Credentials** (or **Google Auth Platform → Clients**)
   - Click **Create Credentials → OAuth client ID** (or **Create Client**)
   - Application type: **Desktop app** → Name: `daily-brief` → **Create**
6. Click on **daily-brief** in the client list to open its detail page. You'll see two values to copy:
   - **Client ID** — looks like `297790329302-xxxx.apps.googleusercontent.com`
   - **Client Secret** — looks like `GOCSPX-xxxxxxxxxxxxxxxxxxxx`
7. Paste both into your `.env` file as `ND_GMAIL_CLIENT_ID` and `ND_GMAIL_CLIENT_SECRET`

> No JSON file download needed — the script reads these values directly from `.env`.

---

### Step 2 — Microsoft Graph API (Johns Hopkins, alumni.jh.edu)

This gives the script permission to read your JHU inbox. JHU uses Microsoft 365, so we use Microsoft's Graph API.

1. Go to [https://portal.azure.com](https://portal.azure.com) — sign in with your **personal Microsoft account** (or create a free one; you do NOT need to sign in with your JHU account here)
2. In the search bar at the top, search **App registrations** → click it
3. Click **New registration**
   - Name: `daily-brief`
   - Supported account types: **Accounts in any organizational directory and personal Microsoft accounts**
   - Redirect URI: choose **Public client/native (mobile & desktop)** from the dropdown, then enter: `http://localhost`
   - Click **Register**
4. You're now on the app's overview page. Copy the **Application (client) ID** — you'll need it shortly
5. In the left sidebar of your app: **API permissions**
   - Click **Add a permission → Microsoft Graph → Delegated permissions**
   - Search for and add: `Mail.ReadWrite`
   - Click **Add permissions**
   - Click **Grant admin consent for [your name]** → **Yes** (this button may not appear if you're not an admin; that's okay — it will work without it for personal use)
6. That's it for Azure. You do NOT need a client secret for this setup.

---

### Step 3 — Create your `.env` file

```bash
cp .env.example .env
```

Open `.env` and fill in:

```
ANTHROPIC_API_KEY=sk-ant-...          # from console.anthropic.com → API Keys
ND_EMAIL_ADDRESS=yourname@alumni.nd.edu
JHU_EMAIL_ADDRESS=yourname@alumni.jh.edu
JHU_AZURE_CLIENT_ID=paste-the-ID-from-step-2-here
JHU_AZURE_TENANT_ID=common
JHU_AZURE_CLIENT_SECRET=             # leave blank
```

Your Anthropic API key is at [https://console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys).

---

### Step 4 — Run the one-time auth flow

```bash
python3 setup_auth.py
```

This will open two browser windows (one for Google, one for Microsoft). Sign into each with the corresponding alumni account. Tokens are saved to `credentials/` and refresh automatically — you won't need to do this again unless you revoke access.

---

## Running the brief

```bash
python3 daily_brief.py
```

Output is saved to `output/brief_YYYY-MM-DD.md` **and** inserted as `## Daily Intelligence Brief` into `_inbox/Today.md` in your Obsidian vault (after `## Therapy`, before `## Jobs`).

### Catch-up behavior

If the script hasn't run for one or more days, it automatically catches up — generating a separate dated brief for each missed day (up to 7 days back). Catch-up briefs are saved to `output/` only; `Today.md` is only updated for the current day's run.

---

## Obsidian integration

Set `VAULT_TODAY_PATH` in `.env` to the absolute path of your `_inbox/Today.md`. The brief is inserted before `## Jobs` (if present) or appended after `## Therapy`. Running the script a second time on the same day replaces rather than duplicates the section.

---

## Automating it daily

The brief runs at **5:00 PM daily** via launchd (`com.seang.daily-brief`).

**How it works:** launchd fires `~/scripts/run_daily_brief.sh`, which `cd`s into the project directory and calls `python3 daily_brief.py`. Using a bash wrapper is necessary because `/bin/bash` (not python3) needs macOS Full Disk Access to reach the Google Drive path.

**Plist:** `~/Library/LaunchAgents/com.seang.daily-brief.plist`  
**Wrapper:** `~/scripts/run_daily_brief.sh`  
**Log:** `~/scripts/daily_brief_launchd.log`

**Prerequisite:** `/bin/bash` must be in System Settings → Privacy & Security → Full Disk Access.

To reload after editing the plist:
```bash
launchctl unload ~/Library/LaunchAgents/com.seang.daily-brief.plist
launchctl load   ~/Library/LaunchAgents/com.seang.daily-brief.plist
```

---

## Troubleshooting

**Gmail auth fails / token expired:** delete `credentials/gmail_token.json` and re-run `setup_auth.py`.

**JHU auth fails:** delete `credentials/jhu_token.json` and re-run `setup_auth.py`. If JHU blocks the sign-in, it may be because the Azure app hasn't been consented. Try opening `https://login.microsoftonline.com/common/adminconsent?client_id=YOUR_CLIENT_ID` in a browser.

**"quota exceeded" errors from Google:** the Gmail API free tier is generous (1 billion units/day) — this should never be an issue for personal use.

**JHU emails not appearing:** verify that `alumni.jh.edu` uses Microsoft 365 by trying to log in at [https://outlook.office.com](https://outlook.office.com). If it uses a different provider, open an issue and I can adapt the IMAP fallback.

**No emails summarized even though you got relevant emails today:** check `output/cron.log` (if running via cron) or run interactively. The keyword filter is intentionally broad; if Claude is filtering too aggressively, the threshold can be tuned.

**A trusted newsletter keeps getting missed:** the script uses a two-stage filter — keyword scan, then Claude relevance scoring. Curated sources can bypass the keyword stage entirely via the `TRUSTED_SENDERS` dict at the top of `daily_brief.py`. Each entry maps a sender address substring (lowercased) to a topic bucket. Add a new line like:
```python
"newsletters@e.econo": "Economic Competition & Geopolitics",  # The Economist
```
Emails from that sender will always reach Claude, regardless of subject keywords. Claude's second-stage filter still runs, so truly off-topic issues (subscription confirmations, etc.) get dropped there.
