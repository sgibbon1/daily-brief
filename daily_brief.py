#!/usr/bin/env python3
"""
Daily Intelligence Brief
Fetches emails from alumni.nd.edu (Gmail API) and alumni.jh.edu (Microsoft Graph),
filters for AI / national-security / Russia-Ukraine / China topics,
summarizes with Claude, writes a dated Markdown file, and marks relevant emails as read.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

import anthropic
import requests

# Provider-agnostic completion (Anthropic or Gemini, chosen by AI_PROVIDER in
# .env) with built-in token-usage logging.
from llm import complete, AI_PROVIDER
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ─────────────────────────────────────────────────────────────────────────────
# Paths & config
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
load_dotenv(dotenv_path=SCRIPT_DIR / ".env", override=True)
CREDENTIALS_DIR = SCRIPT_DIR / "credentials"
OUTPUT_DIR = SCRIPT_DIR / "output"
CREDENTIALS_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# Retry count for Google API network calls. These runs fire on wake-from-sleep
# (launchd), when the network is often briefly flaky, so transient socket errors
# are common; num_retries adds exponential backoff so a blip doesn't abort a run.
API_RETRIES = 5

# Gmail (ND)
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
GMAIL_TOKEN_FILE = CREDENTIALS_DIR / "gmail_token.json"
ND_EMAIL = os.getenv("ND_EMAIL_ADDRESS", "")
ND_GMAIL_CLIENT_ID = os.getenv("ND_GMAIL_CLIENT_ID", "")
ND_GMAIL_CLIENT_SECRET = os.getenv("ND_GMAIL_CLIENT_SECRET", "")

# Microsoft Graph (JHU)
JHU_EMAIL = os.getenv("JHU_EMAIL_ADDRESS", "")
JHU_CLIENT_ID = os.getenv("JHU_AZURE_CLIENT_ID", "")
JHU_TENANT_ID = os.getenv("JHU_AZURE_TENANT_ID", "common")
JHU_CLIENT_SECRET = os.getenv("JHU_AZURE_CLIENT_SECRET", "")
JHU_TOKEN_FILE = CREDENTIALS_DIR / "jhu_token.json"

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPES = ["https://graph.microsoft.com/Mail.ReadWrite"]

# Claude
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Obsidian vault
VAULT_TODAY_PATH = os.getenv("VAULT_TODAY_PATH", "")

MAX_BODY_CHARS = 4_000          # chars sent to Claude for regular emails
MAX_BODY_CHARS_TRUSTED = 20_000  # chars sent to Claude for trusted newsletter senders
MAX_BODY_CHARS_FETCH = 20_000   # max chars fetched per email (safety cap)
BATCH_SIZE = 8                   # emails per Claude call

# ─────────────────────────────────────────────────────────────────────────────
# Topic definitions
# ─────────────────────────────────────────────────────────────────────────────

TOPICS = [
    "Artificial Intelligence & Emerging Technology",
    "National Security & Defense Technology",
    "Russia, Ukraine & Eastern Europe",
    "China & Indo-Pacific Competition",
    "Economic Competition & Geopolitics",
]

KEYWORDS: dict[str, list[str]] = {
    "Artificial Intelligence & Emerging Technology": [
        "artificial intelligence", " ai ", "machine learning", "llm", "large language model",
        "neural network", "deep learning", "generative ai", "chatgpt", "gpt-", "claude",
        "gemini", "openai", "anthropic", "deepmind", "autonomous systems", "algorithm",
        "semiconductor", "nvidia", "quantum computing", "cyber", "autonomous weapons",
        "foundation model", "diffusion model", "robotics", "agi",
    ],
    "National Security & Defense Technology": [
        "national security", "defense", "pentagon", "department of defense", "dod",
        "military", "nato", "intelligence community", "cia", "nsa", "fbi", "dia",
        "weapon system", "warfare", "missile", "nuclear", "arms control",
        "counterterrorism", "surveillance", "signals intelligence", "geoint", "humint",
        "darpa", "diu", "defense innovation", "classified", "c2", "command and control",
        "electronic warfare", "hypersonic", "unmanned",
    ],
    "Russia, Ukraine & Eastern Europe": [
        "russia", "ukraine", "putin", "zelensky", "kremlin", "moscow", "kyiv",
        "russian", "ukrainian", "belarus", "nato", "eastern europe", "baltic states",
        "moldova", "georgia (country)", "wagner group", "bakhmut", "kharkiv",
        "zaporizhzhia", "donbas", "crimea", "sanctions on russia", "war in ukraine",
        "lavrov", "medvedev", "svr", "fsb", "gru",
        # Cyrillic (for Russian-language newsletters like Novaya Gazeta, The Bell RU)
        "россия", "украина", "путин", "кремль", "москва", "киев", "война",
    ],
    "China & Indo-Pacific Competition": [
        "china", "chinese", "beijing", "xi jinping", "pla ", "people's liberation",
        "taiwan", "south china sea", "indo-pacific", "taiwan strait", "semiconductor",
        "huawei", "tiktok", "uyghur", "hong kong", "belt and road", "bri",
        "trade war", "decoupling", "chip war", "export controls", "tsmc",
        "byd", "dji", "mofcom", "ccp",
    ],
    "Economic Competition & Geopolitics": [
        "geopolitics", "sanctions", "tariff", "imf", "world bank",
        "reserve currency", "economic coercion", "supply chain", "critical minerals",
        "rare earth", "energy security", "oil price", "lng", "petrodollar",
        "dollar dominance", "de-dollarization", "strategic competition",
        # broader economic/geopolitical terms
        "economy", "economic", "gdp", "inflation", "recession", "trade policy",
        "fiscal", "monetary policy", "debt ceiling", "federal reserve",
    ],
}

# ---------------------------------------------------------------------------
# Trusted senders — bypass keyword filter, go straight to Claude
# These are curated intelligence / policy / tech newsletters where every
# edition is potentially relevant. Claude's second-stage filter still runs.
# ---------------------------------------------------------------------------
TRUSTED_SENDERS: dict[str, str] = {
    # sender substring (lowercased) → topic bucket
    # --- Strategic analysis & foreign policy ---
    "newsletters@e.econo":          "Economic Competition & Geopolitics",      # The Economist newsletters
    "noreply@e.economist.com":      "Economic Competition & Geopolitics",      # The Economist (noreply variant)
    "economist.com":                "Economic Competition & Geopolitics",      # The Economist (catch-all)
    "newsletter@warontherocks.com": "National Security & Defense Technology",  # War on the Rocks (daily)
    "cogsofwar@warontherocks.com":  "National Security & Defense Technology",  # War on the Rocks (Cogs of War)
    "warontherocks.com":            "National Security & Defense Technology",  # War on the Rocks (catch-all)
    "newsletters@foreignpolicy.com":"National Security & Defense Technology",  # Foreign Policy
    "newsletters@foreig":           "National Security & Defense Technology",
    "fpevents@foreignpolicy.com":   "National Security & Defense Technology",
    "lawfaremedia.org":             "National Security & Defense Technology",  # Lawfare
    "rusi.org":                     "National Security & Defense Technology",  # RUSI
    "csis.org":                     "National Security & Defense Technology",  # CSIS
    "cfr.org":                      "National Security & Defense Technology",  # CFR
    "rand.org":                     "National Security & Defense Technology",  # RAND
    # --- Defense & tech reporting ---
    "newsletters@breakingde":       "National Security & Defense Technology",  # Breaking Defense
    "breakingdefense.com":          "National Security & Defense Technology",
    "defenseone.com":               "National Security & Defense Technology",  # Defense One
    "c4isrnet.com":                 "National Security & Defense Technology",  # C4ISRNET
    # --- Policy newsletters (broad coverage) ---
    "politico.com":                 "National Security & Defense Technology",  # All POLITICO newsletters
    # --- Russia / Ukraine ---
    "support@kyivpost.com":         "Russia, Ukraine & Eastern Europe",        # Kyiv Post
    "kyivpost.com":                 "Russia, Ukraine & Eastern Europe",
    "ringtone@thebell.io":          "Russia, Ukraine & Eastern Europe",        # The Bell (EN + RU)
    "thebell.io":                   "Russia, Ukraine & Eastern Europe",
    "subscribe@novayagazeta.ru":    "Russia, Ukraine & Eastern Europe",        # Novaya Gazeta
    "novayagazeta.ru":              "Russia, Ukraine & Eastern Europe",
    "novayagazeta.eu":              "Russia, Ukraine & Eastern Europe",
    "meduza.io":                    "Russia, Ukraine & Eastern Europe",        # Meduza
    # --- AI & tech newsletters ---
    "dan@tldrnewsletter.com":       "Artificial Intelligence & Emerging Technology",  # TLDR AI
    "tldrnewsletter.com":           "Artificial Intelligence & Emerging Technology",  # TLDR (catch-all)
    "wpintelligence@washingt":      "Artificial Intelligence & Emerging Technology",  # WP Intelligence
    "lesserwrong.com":              "Artificial Intelligence & Emerging Technology",  # LessWrong (no-reply@lesserwrong.com)
    # --- Geopolitics / economics ---
    "foreignaffairs.com":           "Economic Competition & Geopolitics",      # Foreign Affairs
}

# ─────────────────────────────────────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────────────────────────────────────

def decode_mime_words(text: str) -> str:
    parts = decode_header(text or "")
    out = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            out.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def strip_html(html: str) -> str:
    return BeautifulSoup(html, "html.parser").get_text(separator="\n")


def keyword_match(email_dict: dict) -> list[str]:
    """Return matched topic buckets.

    Two-stage:
    1. Check sender against TRUSTED_SENDERS allowlist — always passes to Claude.
    2. Fall back to keyword scan of subject + body.
    """
    sender_lower = email_dict.get("sender", "").lower()
    for pattern, topic in TRUSTED_SENDERS.items():
        if pattern in sender_lower:
            return [topic]  # trusted source — pass through with its bucket

    haystack = (email_dict["subject"] + " " + email_dict["body"]).lower()
    return [topic for topic, kws in KEYWORDS.items() if any(kw in haystack for kw in kws)]


# ─────────────────────────────────────────────────────────────────────────────
# Gmail — Notre Dame (alumni.nd.edu)
# ─────────────────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if GMAIL_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not ND_GMAIL_CLIENT_ID or not ND_GMAIL_CLIENT_SECRET:
                print(
                    "\nERROR: ND_GMAIL_CLIENT_ID or ND_GMAIL_CLIENT_SECRET not set in .env\n"
                    "See README step 1 for instructions."
                )
                sys.exit(1)
            client_config = {
                "installed": {
                    "client_id": ND_GMAIL_CLIENT_ID,
                    "client_secret": ND_GMAIL_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
            flow = InstalledAppFlow.from_client_config(client_config, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        GMAIL_TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _extract_gmail_body(payload: dict) -> str:
    mime = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data", "")

    if mime == "text/plain" and data:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    if mime == "text/html" and data:
        return strip_html(base64.urlsafe_b64decode(data).decode("utf-8", errors="replace"))

    parts = payload.get("parts", [])
    # Prefer plain text
    for part in parts:
        if part.get("mimeType") == "text/plain":
            result = _extract_gmail_body(part)
            if result:
                return result
    for part in parts:
        result = _extract_gmail_body(part)
        if result:
            return result
    return ""


def fetch_gmail_emails(service, since: datetime, until: datetime) -> list[dict]:
    date_str = since.strftime("%Y/%m/%d")
    until_str = until.strftime("%Y/%m/%d")
    # `is:unread` restricts the brief to mail Sean hasn't read yet. Already-read
    # mail is skipped, and the run marks everything it processes as read at the
    # end — so each brief only ever covers genuinely new messages.
    result = service.users().messages().list(
        userId="me", q=f"is:unread after:{date_str} before:{until_str}", maxResults=200
    ).execute(num_retries=API_RETRIES)
    messages = result.get("messages", [])

    emails = []
    for ref in messages:
        msg = service.users().messages().get(
            userId="me", id=ref["id"], format="full"
        ).execute(num_retries=API_RETRIES)
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        try:
            date = parsedate_to_datetime(headers.get("Date", ""))
        except Exception:
            date = datetime.now(timezone.utc)

        emails.append({
            "id": ref["id"],
            "account": "ND Alumni (alumni.nd.edu)",
            "subject": decode_mime_words(headers.get("Subject", "(no subject)")),
            "sender": headers.get("From", "Unknown"),
            "date": date,
            "body": _extract_gmail_body(msg["payload"])[:MAX_BODY_CHARS_FETCH],
            "link": f"https://mail.google.com/mail/u/0/#inbox/{ref['id']}",
            "_service": service,
        })
    return emails


def mark_gmail_read(service, ids: list[str]) -> None:
    if not ids:
        return
    # num_retries gives googleapiclient built-in exponential backoff on transient
    # socket errors (ConnectionResetError / read timeouts). This is the LAST step
    # of a run, after the brief is already in Today.md — without retries a single
    # wake-from-sleep network blip here crashed the script and left every briefed
    # email unread (the exact symptom we saw on Jun 1 / Jun 3). See API_RETRIES.
    service.users().messages().batchModify(
        userId="me",
        body={"ids": ids, "removeLabelIds": ["UNREAD"]},
    ).execute(num_retries=API_RETRIES)
    print(f"  ✓ Marked {len(ids)} ND Gmail messages as read.")


# ─────────────────────────────────────────────────────────────────────────────
# Microsoft Graph — Johns Hopkins (alumni.jh.edu)
# ─────────────────────────────────────────────────────────────────────────────

def _load_jhu_token() -> dict | None:
    if JHU_TOKEN_FILE.exists():
        return json.loads(JHU_TOKEN_FILE.read_text())
    return None


def _save_jhu_token(token: dict) -> None:
    JHU_TOKEN_FILE.write_text(json.dumps(token, indent=2))


def get_jhu_access_token() -> str:
    import msal

    token_cache = msal.SerializableTokenCache()
    cached = _load_jhu_token()
    if cached:
        token_cache.deserialize(json.dumps(cached))

    if JHU_CLIENT_SECRET:
        app = msal.ConfidentialClientApplication(
            JHU_CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{JHU_TENANT_ID}",
            client_credential=JHU_CLIENT_SECRET,
            token_cache=token_cache,
        )
    else:
        app = msal.PublicClientApplication(
            JHU_CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{JHU_TENANT_ID}",
            token_cache=token_cache,
        )

    accounts = app.get_accounts(username=JHU_EMAIL)
    result = None
    if accounts:
        result = app.acquire_token_silent(GRAPH_SCOPES, account=accounts[0])

    if not result:
        # Interactive browser login
        if JHU_CLIENT_SECRET:
            result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        else:
            result = app.acquire_token_interactive(scopes=GRAPH_SCOPES, login_hint=JHU_EMAIL)

    if "access_token" not in result:
        raise RuntimeError(
            f"JHU auth failed: {result.get('error_description', result)}\n"
            "Run setup_auth.py to re-authenticate."
        )

    if token_cache.has_state_changed:
        _save_jhu_token(json.loads(token_cache.serialize()))

    return result["access_token"]


def fetch_jhu_emails(since: datetime, until: datetime) -> list[dict]:
    token = get_jhu_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    until_iso = until.strftime("%Y-%m-%dT%H:%M:%SZ")
    # `isRead eq false` is the Graph equivalent of Gmail's is:unread — restrict
    # the brief to messages Sean hasn't read yet (the run marks them read at the end).
    url = (
        f"{GRAPH_BASE}/me/mailFolders/inbox/messages"
        f"?$filter=isRead eq false and receivedDateTime ge {since_iso} and receivedDateTime lt {until_iso}"
        f"&$select=id,subject,from,receivedDateTime,body,webLink"
        f"&$top=100"
    )

    emails = []
    while url:
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        for msg in data.get("value", []):
            body_content = msg.get("body", {}).get("content", "")
            body_type = msg.get("body", {}).get("contentType", "text")
            if body_type == "html":
                body_content = strip_html(body_content)

            try:
                date = datetime.fromisoformat(
                    msg["receivedDateTime"].replace("Z", "+00:00")
                )
            except Exception:
                date = datetime.now(timezone.utc)

            sender_obj = msg.get("from", {}).get("emailAddress", {})
            sender = f"{sender_obj.get('name', '')} <{sender_obj.get('address', '')}>".strip()

            emails.append({
                "id": msg["id"],
                "account": "JHU Alumni (alumni.jh.edu)",
                "subject": msg.get("subject", "(no subject)"),
                "sender": sender,
                "date": date,
                "body": body_content[:MAX_BODY_CHARS_FETCH],
                "link": msg.get("webLink", ""),
            })
        url = data.get("@odata.nextLink")

    return emails


def mark_jhu_read(ids: list[str]) -> None:
    if not ids:
        return
    token = get_jhu_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    for msg_id in ids:
        # Retry transient network errors with backoff, mirroring the Gmail path —
        # these runs fire on wake-from-sleep and the network is often briefly flaky.
        for attempt in range(API_RETRIES):
            try:
                requests.patch(
                    f"{GRAPH_BASE}/me/messages/{msg_id}",
                    headers=headers,
                    json={"isRead": True},
                    timeout=30,
                ).raise_for_status()
                break
            except requests.RequestException:
                if attempt == API_RETRIES - 1:
                    raise
                time.sleep(2 ** attempt)
    print(f"  ✓ Marked {len(ids)} JHU emails as read.")


# ─────────────────────────────────────────────────────────────────────────────
# Claude summarization
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a senior intelligence analyst assistant supporting a DoD official who tracks five topic areas:

1. Artificial Intelligence & Emerging Technology
2. National Security & Defense Technology
3. Russia, Ukraine & Eastern Europe
4. China & Indo-Pacific Competition
5. Economic Competition & Geopolitics

You receive a numbered batch of emails. For each, decide whether it contains substantive content on any of those topics — not peripheral mentions, but meaningful coverage. Then return a JSON array (one object per email, in order) with this schema:

[
  {
    "email_index": <int>,
    "relevant": <bool>,
    "topics": [<matching topic names, max 2>],
    "summary": "<if relevant: 4–7 precise analyst sentences. Be specific — name actors, numbers, dates, implications. Empty string if not relevant.>",
    "significance": "<if relevant: one sentence on why this matters strategically. Empty if not relevant.>"
  }
]

Rules:
- Err toward inclusion when there is genuine substantive content; exclude newsletters with only passing mentions.
- Emails tagged [TRUSTED SOURCE] are from curated intelligence, policy, or technology publications that reliably cover these topics. Include them unless the specific issue is genuinely off-topic for all five areas. Err strongly toward inclusion for these.
- Summaries should stand alone — a reader who never sees the email should come away fully informed.
- Write with analytical precision: no filler phrases, no hedging without cause.
- Return only the JSON array. No prose before or after it."""


def summarize_batch(batch: list[dict]) -> list[dict]:
    email_blocks = []
    for i, e in enumerate(batch):
        try:
            date_str = e["date"].strftime("%B %d, %Y %H:%M UTC")
        except Exception:
            date_str = "Unknown"
        is_trusted = bool(keyword_match(e))
        trusted_tag = " [TRUSTED SOURCE]" if is_trusted else ""
        body_limit = MAX_BODY_CHARS_TRUSTED if is_trusted else MAX_BODY_CHARS
        email_blocks.append(
            f"EMAIL {i}{trusted_tag}\n"
            f"From: {e['sender']}\n"
            f"Subject: {e['subject']}\n"
            f"Date: {date_str}\n"
            f"Account: {e['account']}\n"
            f"---\n{e['body'][:body_limit]}\n"
        )

    raw = complete(
        system=SYSTEM_PROMPT,
        user="Assess and summarize the following emails:\n\n" + "\n\n".join(email_blocks),
        max_tokens=4096,
        anthropic_model="claude-sonnet-4-6",
        project="daily_brief", script="daily_brief.py", label="summarize",
    ).strip()
    # Strip markdown code fences if model wraps output
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def summarize_all(candidates: list[dict]) -> list[dict]:
    relevant = []
    for start in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[start : start + BATCH_SIZE]
        end = min(start + len(batch), len(candidates))
        print(f"  Summarizing emails {start + 1}–{end} of {len(candidates)}...")
        results = summarize_batch(batch)
        for res in results:
            idx = res.get("email_index", 0)
            if res.get("relevant") and idx < len(batch):
                relevant.append({**batch[idx], **res})
    return relevant


# ─────────────────────────────────────────────────────────────────────────────
# Markdown formatter
# ─────────────────────────────────────────────────────────────────────────────

def _format_email_block(e: dict) -> list[str]:
    """Format a single email as an Obsidian checkbox block."""
    name, addr = parseaddr(e["sender"])
    display = name or addr
    try:
        date_display = e["date"].strftime("%B %d, %Y")
    except Exception:
        date_display = "Unknown date"

    lines = [f"- [ ] **{e['subject']}**"]
    lines.append(
        f"  **From:** {display} | "
        f"**Account:** {e['account']} | "
        f"**Date:** {date_display}"
    )
    lines.append("")
    if e.get("significance"):
        lines.append(f"  *{e['significance']}*")
        lines.append("")
    for summary_line in e.get("summary", "").splitlines():
        lines.append(f"  {summary_line}" if summary_line.strip() else "")
    lines.append("")
    if e.get("link"):
        lines.append(f"  [Open email →]({e['link']})")
    else:
        lines.append("  *No direct link available — search by subject in your inbox.*")
    lines += ["", "---", ""]
    return lines


def _parse_carryover_items(carryover_text: str) -> list[dict]:
    """Parse a carried-over brief body back into individual items by category.

    Returns [{"category": <topic name or None>, "lines": [block lines]}], oldest
    first. This is what lets us *re-consolidate*: each item is re-tagged with its
    category by NAME (the heading's `#` depth is ignored, which is what stops the
    ##→###→######  drift), then merged with today's emails under one heading each.

    Only UNCHECKED ('- [ ]') items are kept — a checked ('- [x]') item has been
    read, so it drops out instead of piling up across days. (This mirrors what
    daily.py --generate already does when it carries items into the morning note.)
    """
    if not carryover_text:
        return []
    body = re.sub(r"<!--.*?-->\n?", "", carryover_text)          # drop carryover_date marker
    body = re.sub(r"^\*\d+ (?:unread|carried over) from .+?\*\s*$", "", body, flags=re.MULTILINE)

    lines = body.splitlines()
    items: list[dict] = []
    current_cat: str | None = None
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        heading = re.match(r"^#{1,6}\s+(.*\S)\s*$", line)
        if heading:
            text = heading.group(1).strip()
            if not text.lower().startswith("daily intelligence brief"):
                # Match by name; anything not a known topic (e.g. "Other Relevant") → None
                current_cat = text if text in TOPICS else None
            i += 1
            continue
        item = re.match(r"^- \[( |x)\] ", line)
        if item:
            checked = item.group(1) == "x"
            block = [line]
            i += 1
            # Gather the item body until the next item, heading, or end of input.
            while i < n and not re.match(r"^- \[( |x)\] ", lines[i]) \
                    and not re.match(r"^#{1,6}\s+\S", lines[i]):
                block.append(lines[i])
                i += 1
            while block and block[-1].strip() in ("", "---"):   # trim trailing rule/blank
                block.pop()
            if not checked and block:
                items.append({"category": current_cat, "lines": block})
            continue
        i += 1
    return items


def _subject_key(block_first_line: str) -> str:
    """Dedup key for an item: its bold subject text, lowercased."""
    m = re.search(r"\*\*(.+?)\*\*", block_first_line)
    return (m.group(1) if m else block_first_line).strip().lower()


def format_brief(relevant: list[dict], today: datetime, carryover_text: str = "") -> str:
    date_str = today.strftime("%B %d, %Y")

    # Re-consolidate: pull carried-over items back into structured form so they
    # merge into the SAME category headings as today's emails (no repeated
    # headings, no heading drift).
    carryover_items = _parse_carryover_items(carryover_text)
    date_m = re.search(r"<!-- carryover_date: (.+?) -->", carryover_text or "")
    date_label = date_m.group(1) if date_m else "previous day"
    n_carry = len(carryover_items)
    carryover_note = f" · {n_carry} unread from {date_label}" if n_carry else ""

    lines = [
        f"# Daily Intelligence Brief — {date_str}",
        "",
        f"*Generated {datetime.now().strftime('%H:%M')} | {len(relevant)} relevant email{'s' if len(relevant) != 1 else ''}{carryover_note}*",
        "",
    ]

    # Each bucket holds bare block-line-lists (no trailing separator); the
    # renderer adds one separator between items so spacing stays uniform whether
    # a block came from today's fetch or from the carryover.
    by_topic: dict[str, list[list[str]]] = {t: [] for t in TOPICS}
    other: list[list[str]] = []
    seen: set[str] = set()

    def _place(block: list[str], category: str | None) -> None:
        key = _subject_key(block[0]) if block else ""
        if not block or key in seen:
            return
        seen.add(key)
        (by_topic[category] if category in by_topic else other).append(block)

    # Today's new emails first (freshest on top), then the carried-over backlog.
    for e in relevant:
        block = _format_email_block(e)
        while block and block[-1].strip() in ("", "---"):   # strip trailing separator
            block.pop()
        category = next((t for t in e.get("topics", []) if t in by_topic), None)
        _place(block, category)

    for it in carryover_items:
        _place(it["lines"], it["category"])

    def _emit(heading: str, blocks: list[list[str]]) -> list[str]:
        out = [f"## {heading}", ""]
        for b in blocks:
            out += b
            out += ["", "---", ""]
        return out

    for topic in TOPICS:
        if by_topic[topic]:
            lines += _emit(topic, by_topic[topic])
    if other:
        lines += _emit("Other Relevant", other)

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Obsidian Today.md insertion
# ─────────────────────────────────────────────────────────────────────────────

def _shift_headings(md: str) -> str:
    """Shift all ATX headings down one level (## → ###, ### → ####, etc.)."""
    return re.sub(r"^(#{1,5}) ", lambda m: "#" + m.group(1) + " ", md, flags=re.MULTILINE)


def insert_into_today(brief_md: str, today_path: Path) -> bool:
    """
    Insert the brief as the second-to-last section of Today.md,
    immediately before the final ## section (Therapy).
    Returns True on success, False if the file couldn't be updated.
    """
    if not today_path.exists():
        print(f"  Today.md not found at {today_path} — skipping vault insert.")
        return False

    text = today_path.read_text(encoding="utf-8")

    # Remove the standalone H1 title line ("# Daily Intelligence Brief — …")
    # and shift all remaining headings down one level to fit under a ## section
    body_lines = brief_md.splitlines()
    # Drop the first H1 line and the blank line after it
    if body_lines and body_lines[0].startswith("# "):
        body_lines = body_lines[1:]
    if body_lines and body_lines[0].strip() == "":
        body_lines = body_lines[1:]
    body = _shift_headings("\n".join(body_lines)).strip()

    section = f"\n---\n\n## Daily Intelligence Brief\n\n{body}\n"

    # Remove any existing brief section including its leading --- separator
    text = re.sub(
        r"\n---\n\n## Daily Intelligence Brief\n.*?(?=\n## |\Z)",
        "",
        text,
        flags=re.DOTALL,
    )
    # Collapse any run of blank lines / stray --- separators at end of file
    # (artifacts from previous re-runs) into a single clean trailing newline
    text = re.sub(r"(\n---\s*){2,}$", "\n---\n", text.rstrip()) + "\n"

    # Insert before ## Today's Jobs if present; otherwise append at end of file
    jobs_match = re.search(r"^## Today's Jobs", text, re.MULTILINE)
    if jobs_match:
        insert_pos = jobs_match.start()
        today_path.write_text(text[:insert_pos] + section + "\n" + text[insert_pos:], encoding="utf-8")
    else:
        today_path.write_text(text.rstrip() + "\n" + section, encoding="utf-8")

    print("  ✓ Brief inserted into Today.md.")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Catch-up: find dates without a brief
# ─────────────────────────────────────────────────────────────────────────────

def get_missing_dates(output_dir: Path, max_lookback: int = 7) -> list[datetime]:
    """
    Return a list of UTC-midnight datetimes for dates that don't yet have a
    brief in output_dir, from the day after the most recent brief up to today.
    Looks back at most max_lookback days so a long offline stretch doesn't
    trigger a massive catch-up.

    NOTE: the dated output/brief_YYYY-MM-DD.md files are the run-state ledger,
    not disposable output. Their presence is how we know a given day already
    ran (skip it) vs. was missed (back-fill it, e.g. after opening the laptop
    late). The brief itself also lives in Today.md, but do NOT stop writing the
    output/ copies — deleting them would make every run re-post already-covered
    days. They're gitignored, so they stay local.
    """
    today = datetime.now().date()  # local date, not UTC
    floor = today - timedelta(days=max_lookback - 1)

    existing: set = set()
    for f in output_dir.glob("brief_*.md"):
        try:
            existing.add(
                datetime.strptime(f.stem.replace("brief_", ""), "%Y-%m-%d").date()
            )
        except ValueError:
            pass

    if existing:
        start = max(max(existing) + timedelta(days=1), floor)
    else:
        start = floor

    missing = []
    current = start
    while current <= today:
        if current not in existing:
            missing.append(
                datetime(current.year, current.month, current.day, tzinfo=timezone.utc)
            )
        current += timedelta(days=1)
    return missing


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    # Provider key check (AI_PROVIDER selects Anthropic [default] or Gemini).
    if AI_PROVIDER == "gemini":
        if not os.environ.get("GEMINI_API_KEY"):
            print("ERROR: AI_PROVIDER=gemini but GEMINI_API_KEY not set in .env")
            sys.exit(1)
    elif not ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set in .env")
        sys.exit(1)

    today_date = datetime.now().date()  # local date, not UTC

    # ── Find missing dates ───────────────────────────────────────────────────
    missing = get_missing_dates(OUTPUT_DIR)
    if not missing:
        print("All dates already covered — nothing to do.")
        return

    if len(missing) > 1:
        date_list = ", ".join(d.strftime("%b %d") for d in missing)
        print(f"\nCatching up {len(missing)} missed day(s): {date_list}")

    # ── Connect to Gmail once (token is reused across all date loops) ────────
    gmail_service = None
    if ND_EMAIL:
        try:
            gmail_service = get_gmail_service()
        except Exception as exc:
            print(f"Gmail connection failed: {exc}")
    else:
        print("Skipping Gmail — ND_EMAIL_ADDRESS not set.")

    # ── Loop over each missing date ──────────────────────────────────────────
    for run_date in missing:
        since = run_date
        until = run_date + timedelta(days=1)
        is_today = run_date.date() == today_date

        print(f"\n{'='*60}")
        print(f"  Daily Intelligence Brief — {run_date.strftime('%B %d, %Y')}")
        print(f"{'='*60}")

        # Fetch emails for this specific day
        gmail_emails: list[dict] = []
        if gmail_service:
            try:
                gmail_emails = fetch_gmail_emails(gmail_service, since, until)
                print(f"ND Gmail: {len(gmail_emails)} emails found.")
            except Exception as exc:
                print(f"  Gmail error: {exc}")

        jhu_emails: list[dict] = []
        if JHU_EMAIL and JHU_CLIENT_ID and JHU_TOKEN_FILE.exists():
            try:
                jhu_emails = fetch_jhu_emails(since, until)
                print(f"JHU Graph: {len(jhu_emails)} emails found.")
            except Exception as exc:
                print(f"  JHU error: {exc}")
        else:
            print("Skipping JHU — not configured or no saved token.")

        all_emails = gmail_emails + jhu_emails
        candidates = all_emails  # no keyword pre-filter — Claude decides relevance
        print(f"Total: {len(all_emails)} emails → sending all to Claude")

        if not candidates:
            print("No topic-relevant emails. No brief generated for this date.")
            continue

        print(f"Summarizing {len(candidates)} candidates with Claude...")
        relevant = summarize_all(candidates)
        print(f"Deemed substantively relevant: {len(relevant)}")

        if not relevant:
            print("No emails cleared the relevance bar. No brief generated.")
            continue

        # Pull in any unread items carried over from the previous day.
        # Primary: read from Today.md's existing ## Daily Intelligence Brief section
        # (injected at 6am by daily.py --generate). Fallback: carryover file if
        # daily.py hasn't run yet (e.g. machine woke up late).
        carryover_text = ""
        if is_today and VAULT_TODAY_PATH:
            today_path = Path(VAULT_TODAY_PATH)
            if today_path.exists():
                today_md = today_path.read_text(encoding="utf-8")
                m = re.search(
                    r"## Daily Intelligence Brief\n\n(.*?)(?=\n## |\Z)",
                    today_md, re.DOTALL,
                )
                if m:
                    existing_body = m.group(1).strip()
                    date_m2 = re.search(r"^\*(\d+) unread from (.+?)\*$", existing_body, re.MULTILINE)
                    if date_m2:
                        date_label = date_m2.group(2)
                        carryover_body = re.sub(
                            r"^\*\d+ unread from .+?\*\n\n?", "", existing_body
                        ).strip()
                        n_carry = len(re.findall(r"^- \[ \]", carryover_body, re.MULTILINE))
                        carryover_text = f"<!-- carryover_date: {date_label} -->\n{carryover_body}"
                        print(f"  ↩ Prepending {n_carry} unread item(s) from {date_label}.")
        # Fallback: carryover file still present (daily.py --generate hasn't run yet)
        if not carryover_text and VAULT_TODAY_PATH:
            fallback = Path(VAULT_TODAY_PATH).parent / "brief_carryover.md"
            if fallback.exists():
                carryover_text = fallback.read_text(encoding="utf-8")
                fallback.unlink()
                print(f"  ↩ Prepending unread items from carryover file (fallback).")

        # Write dated brief to output/ — this file is the run-state ledger
        # (see get_missing_dates); keep it even though the brief also goes in
        # Today.md, or catch-up detection breaks.
        brief = format_brief(relevant, run_date, carryover_text)
        outfile = OUTPUT_DIR / f"brief_{run_date.strftime('%Y-%m-%d')}.md"
        outfile.write_text(brief, encoding="utf-8")
        print(f"Brief saved → {outfile.name}")

        # Insert into Today.md only for today's run
        if is_today and VAULT_TODAY_PATH:
            insert_into_today(brief, Path(VAULT_TODAY_PATH))
        elif not is_today:
            print(f"  (catch-up date — skipping Today.md insert)")

        # Mark relevant emails as read.
        # Belt-and-suspenders: mark_gmail_read already retries the API call, but
        # we also catch here so that if marking still fails it can't crash the
        # whole run. The brief + ledger file are already written, so a crash here
        # would (a) abort any remaining catch-up dates in this loop and (b) leave
        # this day recorded as done with its emails still unread and no retry. A
        # loud warning is better than a hard failure.
        nd_ids = [e["id"] for e in relevant if "ND Alumni" in e.get("account", "")]
        if nd_ids and gmail_service:
            try:
                mark_gmail_read(gmail_service, nd_ids)
            except Exception as exc:
                print(f"  ⚠ Could not mark {len(nd_ids)} ND emails read: {exc}")

        jhu_ids = [e["id"] for e in relevant if "JHU Alumni" in e.get("account", "")]
        if jhu_ids:
            try:
                mark_jhu_read(jhu_ids)
            except Exception as exc:
                print(f"  ⚠ Could not mark {len(jhu_ids)} JHU emails read: {exc}")

    print("\nAll done.")


if __name__ == "__main__":
    main()
