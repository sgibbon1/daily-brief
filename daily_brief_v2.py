#!/usr/bin/env python3
"""daily_brief_v2.py — trend-synthesis rewrite of the Daily Intelligence Brief.

New model (vs the per-email daily_brief.py it will replace):
  - One synthesized narrative per topic area surfacing the day's TRENDS across all
    sources — no more single-journalist snapshot / single-datapoint overreaction.
  - Two LLM stages (map-reduce):
      Stage 1 ROUTE   — cheap batched classify: {relevant?, which topic?, event?}.
                        Tiny schema-enforced JSON; nothing to mis-escape.
      Stage 2 SYNTH   — one call per topic; reads all that topic's email PLUS its
                        FULL archive history (cross-day trend memory, no day
                        limit) PLUS any of the reader's still-unreviewed prior
                        write-ups on this topic, which it's asked to UPDATE and
                        merge into the fresh synthesis rather than repeat as a
                        separate block — the brief distills trends over however
                        long it's been left unread, instead of accumulating
                        parallel near-duplicate sections; writes markdown with
                        `###` subtopics and inline `[desc](E#)` TAG citations
                        that we substitute for real URLs.
  - Coverage is mechanically verifiable:
      * link integrity — model cites tags (E1, E2…), never raw URLs, so a link can
        never be fabricated or cross-wired; unknown tags are dropped.
      * in-bucket completeness — any relevant email the synthesis didn't weave in
        is auto-appended under "### Also noted" (a small item can't vanish silently).
      * "## Coverage" footer lists everything routed NON-relevant (which gets marked
        read and disappears) so the filter itself is auditable.
  - "## Upcoming Events (DC Metro & Virtual)" — topic-relevant events extracted from
    the same email, DC-area or virtual, future-dated, with an RSVP link.
  - Each `## Topic` carries one `- [ ] Reviewed` checkbox (Sean's read-tracker).
  - A successful run marks ALL fetched email read (relevant → synthesized;
    non-relevant → dropped but cleared), so unread finally means "unprocessed."

Reuses daily_brief.py's fetch/auth/mark-read/insert scaffolding verbatim.

Flags: --dry-run (preview, marks nothing), --limit N, --backlog (big one-time
clear: raises cap + sub-batches), --mark-read (needed to clear under --backlog).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import daily_brief as db
from llm import complete, get_ai_provider

TOPICS_ORDERED = [
    "Artificial Intelligence & Emerging Technology",
    "National Security & Defense Technology",
    "China & Indo-Pacific Competition",
    "Economic Competition & Geopolitics",
    "Russia, Ukraine & Eastern Europe",
    "Other",
]
_VALID_TOPICS = set(TOPICS_ORDERED)

# Trusted newsletters (WOTR, Economist, Lawfare, ChinaTalk…) are long-form: the
# substance sits well past the masthead/intro, so they get a much deeper read at
# BOTH stages. Routing on a shallow slice was judging a long issue on its opening
# boilerplate. Matches the old per-email script's 20k trusted depth.
ROUTE_BODY_CHARS = 1200
ROUTE_BODY_CHARS_TRUSTED = 5000
SYNTH_BODY_CHARS = 4000
SYNTH_BODY_CHARS_TRUSTED = 20000
ROUTE_BATCH = 12
SYNTH_SUBBATCH = 12


# ─────────────────────────────────────────────────────────────────────────────
# Fetch
# ─────────────────────────────────────────────────────────────────────────────
#
# The daily window is TIME-based, not count-based. Taking "the newest N unread"
# silently clips a busy day: arrivals run 46–67/day against the old N=60, so the
# tail of a heavy day fell off the back. Instead we ask for everything unread
# since the last brief ran, and --limit is only a runaway-cost backstop.
#
# Mail briefed on a previous run is already marked read, so re-querying from the
# last brief's date costs nothing and self-corrects: anything that arrived late,
# or that a failed run missed, is picked up on the next run rather than lost.

DEFAULT_WINDOW_CAP_DAYS = 7    # a long outage shouldn't trigger a 10k-email run
FETCH_SAFETY_CAP = 400


def _last_brief_date() -> _date | None:
    """Date of the most recent generated brief, from the output directory."""
    dates = []
    for f in db.OUTPUT_DIR.glob("brief_*.md"):
        m = re.fullmatch(r"brief_(\d{4})-(\d{2})-(\d{2})", f.stem)
        if m:
            try:
                dates.append(datetime(*map(int, m.groups())).date())
            except ValueError:
                pass
    return max(dates) if dates else None


def daily_window_query() -> tuple[str, str]:
    """(gmail_query_terms, human_label) covering everything since the last brief.

    Gmail's `after:D` means D 00:00 onward, so passing the last brief's own date
    re-covers that day — deliberately, since mail can arrive after the run.
    """
    last = _last_brief_date()
    today = datetime.now().date()
    start = today - timedelta(days=2) if last is None else min(last, today)
    # Never reach further back than the cap, however long the outage.
    start = max(start, today - timedelta(days=DEFAULT_WINDOW_CAP_DAYS))
    gap = (today - start).days
    return (f"after:{start.strftime('%Y/%m/%d')}",
            f" · window {start.strftime('%b %-d')}→today ({gap}d)")


def same_day_window_query() -> tuple[str, str]:
    """(gmail_query_terms, human_label) covering ALL of today — read or unread.

    Used when a brief for today already exists (a same-day re-run/update). The
    normal is:unread window is a one-shot snapshot: once a topic section is
    written, mail that arrives later on that same topic never gets folded back
    in, and mail marked read (by the first run, or by you opening it) looks
    "handled" even when its content was never actually in a section. Rescanning
    the whole day by DATE rather than by read-state and fully re-synthesizing
    fixes both — a re-run always reflects everything that's arrived today, not
    just the delta since the last run. Costs more (re-routes/re-synthesizes
    today's earlier mail too), which is why it's scoped to today only, not the
    full since-last-brief window.
    """
    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)
    return (f"after:{today.strftime('%Y/%m/%d')} before:{tomorrow.strftime('%Y/%m/%d')}",
            " · same-day rescan (read + unread)")

def _gmail_link(gmail_id: str, rfc822_message_id: str | None) -> str:
    """Best-effort deep link to one message.

    Every `https://mail.google.com/...#...` format tried (`#inbox/<id>`,
    `#all/<id>`, `#search/rfc822msgid:...`) lands on the plain inbox on Sean's
    phone instead of the message. Fetching Gmail's real
    apple-app-site-association (2026-08) showed why: Gmail's iOS app isn't
    registered for Universal Links on mail.google.com at all (only
    Maps/Calendar/Photos/etc. are — Gmail appears solely under
    `webcredentials`, for password autofill, unrelated to link-opening). So
    this isn't an OS-level handoff dropping the fragment; it's Gmail's own
    mobile web JS redirecting into the app once the page has already loaded —
    which no mail.google.com URL shape can prevent, since it fires after the
    page is already rendered. No fragment format was ever going to fix this.

    `message://%3C<Message-ID>%3E` sidesteps mail.google.com entirely — it's
    Apple Mail's own scheme (confirmed Sean's ND account is set up in iOS
    Mail), so opening it never touches Gmail's web client or app at all. Per
    Apple's documented-by-reverse-engineering behavior: on iOS, if the message
    isn't immediately at hand Mail still opens and loads it async in the
    background — no failure mode. On macOS, an uncached message can show an
    error dialog instead (MCMailErrorDomain 1030) rather than silently
    displaying the wrong thing — the one place this could regress — but these
    links are always to mail from the last day or two, so it should almost
    always already be synced. NOT YET CONFIRMED WORKING on Sean's phone as of
    2026-08-11 — this is a retry after a miscommunication caused an earlier
    attempt to get reverted before it was actually tested; watch for
    confirmation on the next real brief. Falls back to Gmail's
    `#all/<gmail-id>` (desktop-reliable, at least not silently wrong) for the
    rare email with no Message-ID header.
    """
    if rfc822_message_id:
        bare = rfc822_message_id.strip().strip("<>")
        if bare:
            return f"message://{quote(f'<{bare}>', safe='')}"
    return f"https://mail.google.com/mail/u/0/#all/{gmail_id}"


def _headers_ci(header_list: list[dict]) -> dict[str, str]:
    """Gmail API header list → dict keyed by LOWERCASED header name.

    RFC 5322 header field names are case-insensitive, but not every sender's
    mail server capitalizes them the conventional way — FT's myFT digest sends
    `Message-Id`, not `Message-ID`. A dict built with the raw (as-sent) casing
    and looked up with a hardcoded "Message-ID" silently misses that mail: the
    lookup returns None, `_gmail_link` has no RFC822 ID to build a `message://`
    link from, and falls back to a Gmail-web `#all/<id>` link instead — which
    is exactly why myFT's links open in Gmail while everything else opens in
    Mail. Lowercasing the key at construction (and looking up with lowercase
    names everywhere this is used) makes the lookup match regardless of how
    the sender capitalized it.
    """
    return {h["name"].lower(): h["value"] for h in header_list}


def fetch_all_unread(service, limit: int, extra_query: str = "",
                     include_read: bool = False) -> list[dict]:
    """Mail newest-first, unread-only unless `include_read`.

    `extra_query` appends raw Gmail search terms to bound the window
    (e.g. 'after:2026/06/30 before:2026/08/05'). Gmail's `before:X` means
    strictly earlier than X 00:00 local, so a Jul 1–Aug 4 window is
    'after:2026/06/30 before:2026/08/05'.

    `include_read` is for RETROSPECTIVE catch-up runs: the daily job marks
    briefed mail read, so a backward-looking pass that filtered on is:unread
    would see only the residue the filter deliberately skipped.
    """
    q = (("" if include_read else "is:unread ") + extra_query).strip()
    messages: list[dict] = []
    page = None
    # Page through — a multi-week window runs to thousands, well past one page.
    while len(messages) < limit:
        result = service.users().messages().list(
            userId="me", q=q, maxResults=min(500, limit - len(messages)),
            pageToken=page,
        ).execute(num_retries=db.API_RETRIES)
        messages += result.get("messages", [])
        page = result.get("nextPageToken")
        if not page:
            break
    messages = messages[:limit]

    def _fetch(ref: dict) -> dict | None:
        # Each thread needs its OWN service object — googleapiclient's http
        # transport is not thread-safe and sharing one corrupts responses.
        try:
            svc = db.get_gmail_service()
            msg = svc.users().messages().get(
                userId="me", id=ref["id"], format="full"
            ).execute(num_retries=db.API_RETRIES)
        except Exception as exc:
            print(f"  ⚠ could not fetch {ref['id']}: {exc}")
            return None
        headers = _headers_ci(msg["payload"].get("headers", []))
        # Normalize to timezone-AWARE. parsedate_to_datetime returns an aware
        # datetime when the header carries an offset (nearly always) and a naive
        # one when it doesn't; mixing the two makes any sort or subtraction throw
        # "can't compare offset-naive and offset-aware datetimes".
        try:
            date = parsedate_to_datetime(headers.get("date", ""))
        except Exception:
            date = None
        if date is None:
            date = datetime.now(timezone.utc)
        elif date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return {
            "id": ref["id"],
            "account": "ND Alumni (alumni.nd.edu)",
            "subject": db.decode_mime_words(headers.get("subject", "(no subject)")),
            "sender": headers.get("from", "Unknown"),
            "date": date,
            "body": db._extract_gmail_body(msg["payload"])[:db.MAX_BODY_CHARS_FETCH],
            "link": _gmail_link(ref["id"], headers.get("message-id")),
        }

    # Sequential fetch of a few thousand messages takes ~40 min; threaded, ~1.
    with ThreadPoolExecutor(max_workers=8) as pool:
        emails = [e for e in pool.map(_fetch, messages) if e]
    emails.sort(key=lambda e: e["date"], reverse=True)
    # Stable tags for citation/substitution (E1, E2, …).
    for i, e in enumerate(emails):
        e["tag"] = f"E{i + 1}"
    return emails


def _is_trusted(e: dict) -> bool:
    """Is this from a curated intel/policy/tech newsletter (TRUSTED_SENDERS)?
    Trusted mail is read far more deeply at both routing and synthesis."""
    return any(p in e.get("sender", "").lower() for p in db.TRUSTED_SENDERS)


# Senders whose mail is PERSONAL/PROFESSIONAL correspondence, never brief material.
# These are hard-coded belt-and-braces on top of the router's `personal` judgement:
# a colleague's note or a recruiter ping must NEVER be marked read by an automated
# job, and one bad LLM call shouldn't be able to bury it. The list is deliberately
# not exhaustive — the router catches the rest.
PERSONAL_SENDER_PATTERNS = [
    "legionintel.com",
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "handshake",
    "calendly.com",
    "docusign",
    "greenhouse.io",
    "lever.co",
    "workday",
]


def _is_personal_sender(e: dict) -> bool:
    return any(p in e.get("sender", "").lower() for p in PERSONAL_SENDER_PATTERNS)


def _sender_name(sender: str) -> str:
    from email.utils import parseaddr
    name, addr = parseaddr(sender)
    return (name or addr or sender).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Read-before-fetch — surface the blind spot `is:unread` can't see
# ─────────────────────────────────────────────────────────────────────────────
#
# Reading mail directly in Gmail (opening it, or even a wide preview pane) marks
# it read before this script ever runs — `is:unread` then skips it at the fetch
# stage, so it's never routed, never synthesized, and never appears in Coverage
# either (Coverage only lists mail the ROUTER saw and rejected). It just vanishes
# with no trace, and it's the same shape of surprise as mail getting marked read
# without a summary, so it deserves the same visibility. We don't re-route every
# already-read email in the window — most of it is ordinary inbox traffic
# (shopping receipts, personal correspondence) and dumping all of it into the
# brief would bury the signal. Instead this scans only the curated
# TRUSTED_SENDERS allowlist — the sources most likely to have mattered — with a
# cheap sender-string check and no LLM call.
def find_read_before_fetch(service, extra_query: str) -> list[dict]:
    q = (f"is:read {extra_query}").strip()
    try:
        result = service.users().messages().list(
            userId="me", q=q, maxResults=200
        ).execute(num_retries=db.API_RETRIES)
    except Exception as exc:
        print(f"  ⚠ read-before-fetch scan failed: {exc}")
        return []

    hits: list[dict] = []
    for ref in result.get("messages", []):
        try:
            msg = service.users().messages().get(
                userId="me", id=ref["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Message-ID"],
            ).execute(num_retries=db.API_RETRIES)
        except Exception:
            continue
        headers = _headers_ci(msg["payload"].get("headers", []))
        sender = headers.get("from", "")
        if not any(p in sender.lower() for p in db.TRUSTED_SENDERS):
            continue
        hits.append({
            "sender": _sender_name(sender),
            "subject": db.decode_mime_words(headers.get("subject", "(no subject)")),
            "link": _gmail_link(ref["id"], headers.get("message-id")),
        })
    return hits


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — ROUTE: relevance + topic + event flag (schema-enforced JSON)
# ─────────────────────────────────────────────────────────────────────────────

ROUTE_SYSTEM = """You triage incoming email for a DoD official's daily intelligence brief. The bar for inclusion is a genuine GEOPOLITICAL, NATIONAL-SECURITY/INTELLIGENCE, or TECHNOLOGY nexus — nothing else belongs in this brief.

For each email decide:
1. relevant: does it have SUBSTANTIVE content (real coverage, not a passing mention) with a clear geopolitical, national-security/intelligence, or technology nexus? Map relevant email to the single best-fit area:
     - Artificial Intelligence & Emerging Technology
     - National Security & Defense Technology
     - China & Indo-Pacific Competition
     - Economic Competition & Geopolitics
     - Russia, Ukraine & Eastern Europe
   STRONGLY prefer one of the five named areas — a nuclear/proliferation or Middle East security story is National Security & Defense Technology, a trade/sanctions story is Economic Competition, etc. Only fall back to "Other" when NO named area fits.
   Use topic "Other" ONLY for email whose SUBSTANCE is geopolitical/intel/tech but genuinely fits none of the five areas — e.g. Latin America / Africa / Arctic security, space or cyber policy, nuclear proliferation dynamics outside the five regions, a novel emerging-tech domain. "Other" is a NARROW bucket, usually empty. It is NOT for: arts/literary/culture events (an FT Weekend Festival, a book festival), general-interest news digests, book/film reviews, or anything whose subject is not itself geopolitical/intel/tech. When unsure whether something belongs in "Other," it does NOT — set relevant=false.
2. topic: the best-fit area, or "Other" (only per the rule above).
3. is_event: does it announce a specific UPCOMING event a policy professional might attend (in-person or virtual)? (Independent of relevance.)
4. personal: is this PERSONAL or PROFESSIONAL CORRESPONDENCE directed at the reader as an individual, rather than a publication sent to a subscriber list? Set personal=TRUE for: mail from a named human writing to the reader; anything from an employer, client, colleague, business partner, or their company domain (e.g. Legion Intelligence); recruiters and job-application/interview correspondence; professional-network and social notifications (LinkedIn invitations, messages, job alerts); calendar invites, contracts and e-signature requests; account, billing, medical, financial, legal, or school/family mail; anything the reader plainly needs to ACT on personally. When in doubt about whether something is addressed to him personally, set personal=TRUE — the cost of wrongly briefing a personal email is far higher than the cost of skipping a newsletter.
   personal=FALSE for ordinary newsletters, digests, press releases, think-tank blasts, and marketing — even when the greeting is "Hi Sean" or the sender signs their name, because those go to a whole list.
   personal is INDEPENDENT of relevance: a note from a colleague about Ukraine drones is still personal=TRUE. Personal mail is never briefed and never marked read.

Set relevant=FALSE — do not force into "Other" — for anything WITHOUT that nexus, even if substantive and worth the reader's time: philosophy/ideas essays, personal finance (credit cards, student loans), religious/devotional readings, purely domestic partisan politics with no national-security angle, personal or lifestyle newsletters, marketing, logistics, fundraising. The reader keeps these unread in their own inbox as a to-do list; the brief must leave them alone.

Emails tagged [TRUSTED SOURCE] are curated intel/policy/tech publications and are usually LONG-FORM DIGESTS covering several stories in one issue. Read the WHOLE excerpt before judging — the on-domain substance is often further down, past the masthead, subject line, and opening item. If ANY meaningful part of the issue has the nexus, mark it relevant and pick the topic that best fits that part. Only mark a trusted source non-relevant when the entire issue is off-domain or non-editorial (a subscription/marketing promo, a hiring notice, an event-only blast).

Return ONLY a JSON array, one object per email in order:
{"email_index": <int>, "relevant": <bool>, "topic": "<area or 'Other'>", "is_event": <bool>, "personal": <bool>}."""

ROUTE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "email_index": {"type": "integer"},
            "relevant": {"type": "boolean"},
            "topic": {"type": "string"},
            "is_event": {"type": "boolean"},
            "personal": {"type": "boolean"},
        },
        "required": ["email_index", "relevant", "topic", "is_event", "personal"],
    },
}


def route_emails(emails: list[dict]) -> None:
    for start in range(0, len(emails), ROUTE_BATCH):
        batch = emails[start:start + ROUTE_BATCH]
        blocks = []
        for i, e in enumerate(batch):
            trusted = _is_trusted(e)
            tag = " [TRUSTED SOURCE]" if trusted else ""
            depth = ROUTE_BODY_CHARS_TRUSTED if trusted else ROUTE_BODY_CHARS
            blocks.append(
                f"EMAIL {i}{tag}\nFrom: {e['sender']}\nSubject: {e['subject']}\n"
                f"---\n{e['body'][:depth]}\n"
            )
        raw = complete(
            system=ROUTE_SYSTEM,
            user="Triage these emails:\n\n" + "\n\n".join(blocks),
            max_tokens=1600, thinking_level="low",
            anthropic_model="claude-haiku-4-5-20251001",
            response_schema=ROUTE_SCHEMA,
            project="daily_brief", script="daily_brief_v2.py", label="route",
        ).strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw); raw = re.sub(r"\s*```$", "", raw)
        try:
            verdicts = json.loads(raw)
        except json.JSONDecodeError:
            # Fail SAFE, not loud: an unrouted email is listed in the Coverage
            # audit and left unread, so nothing is lost — whereas defaulting to
            # relevant would brief and mark-read mail we never actually judged,
            # which is exactly how a personal email would get buried.
            for e in batch:
                e["relevant"], e["topic"], e["is_event"] = False, None, False
                e["personal"] = _is_personal_sender(e)
            print(f"  ⚠ routing batch @{start} unparseable — left unread, see Coverage")
            continue
        for v in verdicts:
            idx = v.get("email_index", -1)
            if 0 <= idx < len(batch):
                topic = v.get("topic", "Other")
                batch[idx]["relevant"] = bool(v.get("relevant"))
                batch[idx]["topic"] = topic if topic in _VALID_TOPICS else "Other"
                batch[idx]["is_event"] = bool(v.get("is_event"))
                batch[idx]["personal"] = bool(v.get("personal"))
        for e in batch:
            e.setdefault("relevant", False)
            e.setdefault("topic", None)
            e.setdefault("is_event", False)
            # Known personal/professional domains override the model's call in the
            # protective direction only — a sender on the list is ALWAYS personal.
            e["personal"] = bool(e.get("personal")) or _is_personal_sender(e)
            if e["personal"]:
                e["relevant"] = False   # never briefed, never marked read
                e["is_event"] = False


# ─────────────────────────────────────────────────────────────────────────────
# Cross-day trend memory
# ─────────────────────────────────────────────────────────────────────────────

def _archive_dir() -> Path | None:
    if not db.VAULT_TODAY_PATH:
        return None
    d = Path(db.VAULT_TODAY_PATH).parent.parent / "archive" / "Daily Intelligence Brief"
    return d if d.exists() else None


def recent_context(topic: str) -> str:
    """Every archived day's coverage of `topic`, oldest first — no day limit and
    no truncation. Sean wants trend continuity even across a month-long gap, so
    this deliberately scans the WHOLE archive rather than a fixed recent window;
    the merge-based carry-forward below (see `collect_carryover`) is what keeps
    this from growing without bound in practice — once an ongoing thread is
    folded into a fresh synthesis, later days build on that compact summary
    rather than re-reading the raw history that produced it."""
    d = _archive_dir()
    if not d:
        return ""
    files = sorted(d.glob("*.md"), reverse=True)
    pat = re.compile(
        rf"^#{{2,4}}\s+{re.escape(topic)}\s*$\n(.*?)(?=^#{{1,4}}\s+\S|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    slices = []
    for f in files:
        try:
            m = pat.search(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if m and m.group(1).strip():
            slices.append(f"[{f.stem}]\n{m.group(1).strip()}")
    return "\n\n".join(slices)


# ─────────────────────────────────────────────────────────────────────────────
# Carry-forward — an unreviewed section keeps reappearing until it's checked off
# ─────────────────────────────────────────────────────────────────────────────
#
# The `- [ ] Reviewed` boxes are the reader's own "I've consumed this" tracker.
# Until a box is ticked, that block is re-emitted in the next day's brief so it
# can't scroll out of sight — the brief behaves like a queue, not a newspaper.
#
# TWO WRINKLES the parser has to handle:
#  1. Heading levels SHIFT. Generated briefs use `## Topic` / `### Subtopic`, but
#     insert_into_today() demotes everything by one when it lands in Today.md, so
#     the archived copy reads `### Topic` / `#### Subtopic`. We match topics by
#     NAME at any level and renormalize on the way back out.
#  2. Carried blocks get re-carried. The "carried from" line records the ORIGINAL
#     date, and re-carrying preserves it, so the "since" shown to the reader is
#     always when the thread first opened, however many days it's been open —
#     there is no age cutoff; an item lives until reviewed or until it gets
#     folded into a fresh synthesis on some topic-relevant day (see main()'s
#     merge step).

_CARRY_LINE_RE = re.compile(r"^_Carried forward from ([0-9]{4}-[0-9]{2}-[0-9]{2})\b.*_$", re.M)
_REVIEWED_BOX_RE = re.compile(r"^- \[([ xX])\]\s*Reviewed\s*$", re.M)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(\S.*?)\s*$", re.M)


def _blocks(text: str, level: int) -> list[tuple[str, str]]:
    """Split `text` into (heading_title, body) pairs at exactly `level` hashes.
    Body runs to the next heading of the SAME OR SHALLOWER level."""
    heads = [(m.start(), len(m.group(1)), m.group(2), m.end())
             for m in _HEADING_RE.finditer(text)]
    out = []
    for i, (pos, lv, title, end) in enumerate(heads):
        if lv != level:
            continue
        stop = len(text)
        for pos2, lv2, _, _ in heads[i + 1:]:
            if lv2 <= level:
                stop = pos2
                break
        out.append((title, text[end:stop].strip("\n")))
    return out


def _shift_headings(text: str, delta: int) -> str:
    if not delta:
        return text
    return _HEADING_RE.sub(
        lambda m: "#" * max(1, min(6, len(m.group(1)) + delta)) + " " + m.group(2), text)


def _prior_brief_text() -> tuple[str, str] | None:
    """(iso_date, text) of the most recent archived brief BEFORE today."""
    d = _archive_dir()
    if not d:
        return None
    today = datetime.now().strftime("%Y%m%d")
    files = sorted((f for f in d.glob("*.md") if f.stem.isdigit() and f.stem < today),
                   reverse=True)
    for f in files:
        try:
            return f"{f.stem[:4]}-{f.stem[4:6]}-{f.stem[6:]}", f.read_text(encoding="utf-8")
        except Exception:
            continue
    return None


def _already_reviewed_today() -> set[str]:
    """Titles the reader ticked off in TODAY's brief. On a same-day re-run the
    carry-forward source is still yesterday's archive (where every box is
    unchecked), so without this a box ticked this morning would pop right back."""
    if not db.VAULT_TODAY_PATH:
        return set()
    try:
        text = Path(db.VAULT_TODAY_PATH).read_text(encoding="utf-8")
    except Exception:
        return set()
    done: set[str] = set()
    heads = [(m.start(), m.group(2)) for m in _HEADING_RE.finditer(text)]
    for i, (pos, title) in enumerate(heads):
        stop = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        box = _REVIEWED_BOX_RE.search(text[pos:stop])
        if box and box.group(1).lower() == "x":
            done.add(title.strip())
    return done


def collect_carryover() -> dict[str, list[dict]]:
    """{topic: [{title, body, since}]} — blocks left unreviewed in the last brief.

    A topic section with subsections carries per-SUBSECTION (so ticking one
    doesn't drop the rest); a section without subsections carries whole.
    """
    prior = _prior_brief_text()
    if not prior:
        return {}
    prior_date, text = prior
    reviewed = _already_reviewed_today()

    # Topics may sit at ## (raw brief) or ### (archived copy) — find their level.
    levels = {lv for m in _HEADING_RE.finditer(text)
              for lv, t in [(len(m.group(1)), m.group(2).strip())] if t in _VALID_TOPICS}
    if not levels:
        return {}
    tlevel = min(levels)

    def _since(body: str) -> str:
        m = _CARRY_LINE_RE.search(body)
        return m.group(1) if m else prior_date

    def _clean(body: str) -> str:
        body = _REVIEWED_BOX_RE.sub("", body)
        body = _CARRY_LINE_RE.sub("", body)
        # A standalone "---" is a VAULT-rendering artifact — insert_into_today's
        # _add_subsection_dividers (daily_brief.py) bakes one into the Today.md
        # copy, which then gets archived and read back in here. Left in, it
        # compounds: each day this block is re-carried, insert_into_today adds
        # ANOTHER "---" on top of the one already carried in the body, so an
        # item unreviewed for a week accumulates a stack of them. Strip it here
        # so the carried body is divider-free, matching freshly-synthesized
        # text, and insert_into_today adds exactly one correctly-placed "---"
        # each time regardless of how many days this block has been carried.
        body = re.sub(r"^---[ \t]*$\n?", "", body, flags=re.MULTILINE)
        body = _strip_inline_bold(body)
        return body.strip()

    carry: dict[str, list[dict]] = {}
    for topic, body in _blocks(text, tlevel):
        if topic.strip() not in _VALID_TOPICS:
            continue
        subs = _blocks(body, tlevel + 1)
        if subs:
            for sub_title, sub_body in subs:
                if sub_title.strip() in reviewed:
                    continue
                box = _REVIEWED_BOX_RE.search(sub_body)
                if not box or box.group(1).lower() == "x":
                    continue          # ticked, or never had a box — nothing owed
                since = _since(sub_body)
                cleaned = _clean(sub_body)
                if cleaned:
                    # Renormalize to generation levels: this sub becomes a `###`.
                    carry.setdefault(topic.strip(), []).append({
                        "title": sub_title.strip(),
                        "body": _shift_headings(cleaned, 3 - (tlevel + 1)),
                        "since": since})
        else:
            if topic.strip() in reviewed:
                continue
            box = _REVIEWED_BOX_RE.search(body)
            if not box or box.group(1).lower() == "x":
                continue
            since = _since(body)
            cleaned = _clean(body)
            if cleaned and "_No significant developments._" not in cleaned:
                carry.setdefault(topic.strip(), []).append({
                    "title": "Carried forward",
                    "body": _shift_headings(cleaned, 3 - (tlevel + 1)),
                    "since": since})
    return carry


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — SYNTHESIZE one topic (markdown, tag-cited)
# ─────────────────────────────────────────────────────────────────────────────

SYNTH_SYSTEM = """You are a senior intelligence analyst writing ONE topic section of a DoD official's daily brief.

You get today's emails on this topic (often several outlets) each labeled with a TAG like [E3], this topic's full archive history for trend context, and — when there's a backlog — ONGOING THREADS: topic write-ups from prior days the reader hasn't reviewed yet. Write a concise, trend-focused synthesis — the throughline of what's developing, not a list of what each email said.

Rules:
- Lead with the day's throughline; where a recurring pattern warrants it, use `### Subtopic` headings.
- AGGREGATE across sources. When multiple independent outlets converge, say so (real trend). When a claim rests on a SINGLE source, attribute it and don't inflate it. Don't overreact to one datapoint.
- Use the archive context to note whether a theme is BUILDING, CONTINUING, or FADING — only when the context supports it. Refer to prior days in PLAIN PROSE (e.g. "as reported earlier this week"); do NOT wrap prior-days context in link syntax — only today's tagged emails can be linked.
- ONGOING THREADS — this is the reader's unread backlog on this topic, not just background. The reader wants trends distilled over time, not the same ground re-reported day after day. For each ongoing thread: if today's emails genuinely develop it further, UPDATE it — merge the new development into that thread's throughline under a recognizable `### Subtopic` heading (reuse or closely echo its original title) rather than writing a separate, parallel section that repeats what it already said. If today's emails don't touch it at all, still include it (restate its existing throughline briefly, close to as-written) so it isn't silently dropped before the reader has seen it — do not fabricate new movement for it. Only give a genuinely unrelated development its own new `### Subtopic` with no ongoing-thread counterpart.
- NO markdown bold (`**text**`) anywhere in the prose, including on BUILDING/CONTINUING/FADING and other trend words — state the trend in plain text (e.g. "a continuing theme", "is building").
- CITATIONS — cite a development with a markdown link whose target is the email's TAG, placed at a clause or sentence boundary so it still reads naturally once the bracket text is swapped for the outlet's name (we do this substitution automatically — whatever you put in the brackets is discarded). Example: `Nvidia launched a cyberdefense alliance [ph](E17).` NEVER write a bare `(E17)` or `[E17]` on its own, and NEVER write a raw URL. Every email you draw from must be cited this way — EVERY sentence drawing on that email, including a second or third sentence from the SAME source later in the same paragraph, needs its OWN `[desc](E#)`; never fall back to just typing the outlet's name as bare prose. Don't invent tags. An ongoing thread's OWN prior text may already contain real `[name](url)` links from when it was first written — keep those as-is; they are not tags and don't need re-citing.
- Be terse. No filler, no restating. If the material is thin, a few sentences is fine.
- Output GitHub-flavored markdown ONLY. Do NOT emit the topic name as a heading (it's added for you). Start with prose or a `### Subtopic`."""


def _synth_block(e: dict) -> str:
    limit = SYNTH_BODY_CHARS_TRUSTED if _is_trusted(e) else SYNTH_BODY_CHARS
    try:
        date_str = e["date"].strftime("%b %d")
    except Exception:
        date_str = "?"
    return (f"[{e['tag']}] From: {e['sender']} | {date_str}\n"
            f"Subject: {e['subject']}\n---\n{e['body'][:limit]}\n")


# When a section covers many emails or a multi-day window (a missed-day catch-up
# or the backlog), expand detail so specifics aren't compressed away. A normal
# daily section stays tight.
EXPAND_EMAIL_THRESHOLD = 8
_EXPAND_NOTE = (
    "\n\nNOTE: this section covers a LONGER-THAN-USUAL window (a multi-day catch-up "
    "or backlog), so be more THOROUGH than a normal daily section — preserve the "
    "important specifics (names, numbers, dates), use `### Subtopic` headings "
    "generously to organize the larger volume, and do NOT drop a meaningful "
    "development for brevity. Still trend-focused, just fuller.")


# Per-topic standing instructions. Ukraine (politics, military capability, and
# especially defense technology) is a top standing interest, so it gets a
# permanent subsection in NatSec and explicit call-outs in the Russia section.
TOPIC_GUIDANCE = {
    "National Security & Defense Technology": (
        "\n\nSTANDING REQUIREMENT — this section must ALWAYS include a "
        "`### Ukraine Defense Tech` subsection covering Ukrainian defense technology, "
        "military capabilities, procurement, drone/EW/missile innovation, and "
        "defense-industrial developments — including lessons Western militaries are "
        "drawing from Ukraine. Scan EVERY email for Ukraine-related defense content, "
        "including sources from Ukraine and any passing but substantive Ukraine "
        "mention in a broader piece. If today's material genuinely has none, still "
        "emit the subsection with one line saying so."),
    "Russia, Ukraine & Eastern Europe": (
        "\n\nSTANDING EMPHASIS — Ukraine is a top interest. Lead with Ukraine when "
        "there is Ukrainian material, and make Ukraine-related developments "
        "(politics, military capabilities, defense technology, war conduct) explicit "
        "in your `### Subtopic` headings rather than folding them into general "
        "Russia coverage."),
}


def _carried_block(item: dict) -> str:
    return (f"[ONGOING since {item['since']}] \"{item['title']}\"\n---\n"
            f"{item['body']}\n")


def _synth_call(topic: str, blocks: list[str], context: str,
                is_reduce: bool, expanded: bool,
                carried_blocks: list[str] | None = None) -> str:
    guidance = TOPIC_GUIDANCE.get(topic, "")
    carried_note = ""
    if carried_blocks:
        carried_note = (
            "\n\nONGOING THREADS (the reader's unread backlog on this topic — "
            "update the ones today's emails develop further, restate the rest "
            "briefly so nothing is dropped; see the ONGOING THREADS rule):\n\n"
            + "\n\n".join(carried_blocks))
    if is_reduce:
        user = (f"TOPIC: {topic}\n\nMERGE these partial write-ups into one clean, "
                f"well-organized section. Remove repetition, KEEP every [E#] tag "
                f"citation and every distinct development."
                f"{guidance}{_EXPAND_NOTE if expanded else ''}{carried_note}"
                f"\n\nPARTIALS:\n\n" + "\n\n---\n\n".join(blocks))
    else:
        user = (f"TOPIC: {topic}\n\nPRIOR COVERAGE (full archive history, context "
                f"only):\n{context or '(none available)'}"
                f"{guidance}{_EXPAND_NOTE if expanded else ''}{carried_note}"
                f"\n\nEMAILS:\n\n" + "\n\n".join(blocks))
    return complete(
        system=SYNTH_SYSTEM, user=user,
        max_tokens=4200 if expanded else 2600, thinking_level="low",
        anthropic_model="claude-sonnet-4-6",
        project="daily_brief", script="daily_brief_v2.py", label="synthesize",
    ).strip()


def _strip_inline_bold(md: str) -> str:
    """Remove markdown bold (`**text**`) from prose, keeping the text itself.

    The brief never intends bold as a formatting element (headers use `#`, not
    bold) — this is a belt-and-suspenders backstop for SYNTH_SYSTEM's
    no-bold rule, since an LLM instruction isn't a hard guarantee. Applied to
    both freshly-synthesized text and carried-forward text, since carried
    content can be several days old and predate this fix.
    """
    return re.sub(r"\*\*(.+?)\*\*", r"\1", md)


def _apply_tags(md: str, tagmap: dict[str, str], namemap: dict[str, str]) -> str:
    """Turn every email-tag citation into a real clickable URL, whatever form the
    model emitted — proper `[desc](E#)`, bare `(E#)`, or bare `[E#]`. The link TEXT
    is always overwritten with the source's publication/sender name from `namemap`
    (never the model's own bracket text or the email subject) — deterministic, so
    it can't drift into a subject-line paraphrase. Unknown tags are dropped
    entirely. Guarantees no fabricated/cross-wired URL and no raw tag marker
    leaking into the brief."""
    # 1) [anything](E#) → [publication name](url); unknown tag → fall back to the
    #    model's own bracket text as plain words (better than a hole in the prose)
    md = re.sub(
        r"\[([^\]]+)\]\((E\d+)\)",
        lambda m: f"[{namemap[m.group(2)]}]({tagmap[m.group(2)]})" if m.group(2) in tagmap else m.group(1),
        md)
    # 2) bare (E#) → ([publication name](url)); unknown → drop
    md = re.sub(
        r"\((E\d+)\)",
        lambda m: f"([{namemap[m.group(1)]}]({tagmap[m.group(1)]}))" if m.group(1) in tagmap else "",
        md)
    # 3) bare [E#] → [publication name](url); unknown → drop
    md = re.sub(
        r"\[(E\d+)\]",
        lambda m: f"[{namemap[m.group(1)]}]({tagmap[m.group(1)]})" if m.group(1) in tagmap else "",
        md)
    # 4) unwrap any leftover [text](target) whose target isn't a real URL — the
    #    model sometimes "links" cross-day context to a date or source name
    #    (e.g. [US strikes on Iran](20260724)); those have no clickable target.
    md = re.sub(r"\[([^\]]+)\]\((?!https?://)[^)]*\)", r"\1", md)
    return re.sub(r"[ ]{2,}", " ", md)  # tidy any double spaces left by a drop


def _link_bare_source_names(md: str, emails: list[dict]) -> str:
    """Safety net for SYNTH_SYSTEM's citation rule. The model reliably brackets
    the FIRST reference to a source in a paragraph but sometimes drops the
    bracket on a later sentence citing the same email, leaving the bare sender
    name sitting in the prose as inert text (e.g. "...raising concerns about
    weaponization TLDR AI." instead of "...weaponization [TLDR AI](E4).") —
    `_apply_tags` only touches `[text](E#)`/`(E#)`/`[E#]` patterns, so a bare
    name with no tag markup at all sails through untouched. Since every
    sender's own link is already known (it's `emails`, not model output), this
    mechanically wraps any bare, unlinked occurrence of a sender's exact
    display name — no LLM call, so no room for a repeat of the drop.
    Longest names first so a short name can't half-match inside a longer one
    sharing a prefix (e.g. "FP's James Palmer" vs a hypothetical "FP").

    Many sender display names are themselves "X from Y" author+publication
    citations (Substack's convention, e.g. "Al Mauroni from Nuclear Weapons
    (and other WMD)") — `\\b` at the trailing edge of a name ending in
    punctuation like `)` never matches, because `\\b` needs a word-char/
    non-word-char TRANSITION, and both the `)` and whatever follows it in
    prose (another punctuation mark, or a space) are non-word characters. So
    a name ending in `)` immediately followed by "." or ", " — the normal
    case in a sentence — silently failed to link. `(?<!\\w)`/`(?!\\w)` check
    only one side each and don't have that blind spot.
    """
    name_to_link: dict[str, str] = {}
    for e in emails:
        name = _sender_name(e["sender"])
        if name:
            name_to_link.setdefault(name, e["link"])
    for name in sorted(name_to_link, key=len, reverse=True):
        link = name_to_link[name]
        # Skip a match already wrapped as `[Name](url)` — preceded by `[` or
        # immediately followed by `](`.
        pattern = re.compile(
            r"(?<!\[)(?<!\w)" + re.escape(name) + r"(?!\w)(?!\]\()")
        md = pattern.sub(lambda m, link=link: f"[{m.group(0)}]({link})", md)
    return md


def synthesize_topic(topic: str, emails: list[dict], expanded: bool,
                     tagmap: dict[str, str] | None = None,
                     namemap: dict[str, str] | None = None,
                     carried: list[dict] | None = None) -> str:
    context = recent_context(topic)
    blocks = [_synth_block(e) for e in emails]
    # Ongoing (unreviewed) threads for this topic — see the ONGOING THREADS rule
    # in SYNTH_SYSTEM. Only handed to the FINAL call (the reduce step when
    # sub-batching, otherwise the single call): merging is a whole-section
    # judgment, and giving it to every partial would risk each partial
    # separately re-stating the same backlog instead of one clean merge.
    carried_blocks = [_carried_block(it) for it in carried] if carried else None
    # Sub-batch (map-reduce) whenever the bucket is large — a natural multi-day
    # catch-up gets the same completeness treatment as an explicit --backlog run.
    if len(blocks) > SYNTH_SUBBATCH:
        partials = [_synth_call(topic, blocks[s:s + SYNTH_SUBBATCH], context, False, expanded)
                    for s in range(0, len(blocks), SYNTH_SUBBATCH)]
        raw = _synth_call(topic, partials, context, is_reduce=True, expanded=expanded,
                          carried_blocks=carried_blocks)
    else:
        raw = _synth_call(topic, blocks, context, is_reduce=False, expanded=expanded,
                          carried_blocks=carried_blocks)

    # In-bucket completeness: any relevant email whose tag never got cited is
    # appended under "### Also noted" so a small item can't vanish silently.
    # Detect the tag in ANY citation form — [desc](E#), bare (E#), or [E#].
    cited = set(re.findall(r"[\[(](E\d+)[\])]", raw))
    # Resolve against ALL fetched email, not just this topic's bucket. Tags are
    # global (E1…En) and the model does sometimes cite an email that routing put
    # in a neighbouring bucket. With a bucket-scoped map those tags resolve to
    # nothing and _apply_tags strips the link, stranding the descriptor mid-
    # sentence ("…does not advance the frontier Sonnet 5 release.").
    tagmap = tagmap or {e["tag"]: e["link"] for e in emails}
    namemap = namemap or {e["tag"]: _sender_name(e["sender"]) for e in emails}
    missing = [e for e in emails if e["tag"] not in cited]
    body = _apply_tags(raw, tagmap, namemap)
    body = _link_bare_source_names(body, emails)
    body = _strip_inline_bold(body)
    if missing:
        body += "\n\n### Also noted\n"
        for e in missing:
            body += f"- [{_sender_name(e['sender'])}]({e['link']}) — {e['subject']}\n"
    # Every email in `emails` just landed either in the synthesized prose (cited)
    # or the "Also noted" fallback — mark it so main() can verify, before marking
    # anything read, that it actually made it into the brief rather than trusting
    # that invariant blindly (see main()'s mark-read guard).
    for e in emails:
        e["_verified"] = True
    return body.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Events extraction
# ─────────────────────────────────────────────────────────────────────────────

EVENTS_SYSTEM = """You extract UPCOMING events from email for a DC-based policy professional.

SPECIAL SOURCE — POLITICO's National Security Daily ends nearly every issue with a "Tomorrow Today" section listing next-day/upcoming Washington events (hearings, think-tank panels, briefings). ALWAYS mine that section and extract every listed event that passes the filters below; these are prime DC-area natsec events.

Pull an event ONLY if it meets ALL of these:
1. SUBJECT nexus — it is substantively about AI/emerging tech, national security/defense, China/Indo-Pacific, economics/geopolitics, or Russia/Ukraine. EXCLUDE philosophy/ideas salons, liberalism/political-theory discussions, book/literary/arts festivals, receptions, general-interest talks, and gym/fitness class schedules (CrossFit, weightlifting, HYROX, etc.) — even from serious outlets or emails that also mention "national security" in a business name. If the topic isn't itself geopolitical/intel/tech, skip it, and never assign one of the five topic labels to an event that fails this filter.
2. LOCATION — it is in the Washington DC metro area (DC, Arlington, Alexandria, Bethesda, etc.) OR virtual/online. Skip in-person events elsewhere with no virtual option.
3. FUTURE — it takes place ON OR AFTER today's date (given in the user message). Skip any event whose date has already passed.

Return ONLY a JSON array (possibly empty), one object per qualifying event:
{"tag": "<E# of the source email>", "title": "<event title>", "when": "<date/time as stated>", "where": "<DC-area venue or 'Virtual'>", "topic": "<one of the five areas>"}."""

EVENTS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "tag": {"type": "string"},
            "title": {"type": "string"},
            "when": {"type": "string"},
            "where": {"type": "string"},
            "topic": {"type": "string"},
        },
        "required": ["tag", "title", "when", "where"],
    },
}


# Event-listing blocks that ride at the END of a long digest. POLITICO NatSec
# Daily's is "Tomorrow Today"; the others are common variants worth catching.
_EVENT_BLOCK_RE = re.compile(
    r"(tomorrow\s+today|on\s+the\s+calendar|upcoming\s+events|mark\s+your\s+calendar)",
    re.IGNORECASE)


def _event_excerpt(e: dict) -> str:
    """Excerpt to send the event extractor.

    A dedicated invite states itself up top, so the head is enough. But a digest's
    event listing sits at the very END (past a 2k head slice), so when we spot an
    event-block marker we send the text from that marker onward — otherwise
    "Tomorrow Today" would never reach the model.
    """
    body = e.get("body", "")
    m = _EVENT_BLOCK_RE.search(body)
    if m:
        return body[:800] + "\n…\n" + body[max(0, m.start() - 200):m.start() + 4000]
    return body[:2000]


def extract_events(emails: list[dict]) -> list[dict]:
    """Scan ALL fetched email (event invites are often routed non-relevant) for
    DC-area / virtual upcoming events. Returns event dicts with a real link."""
    # Routing flags dedicated event invites, but event LISTINGS also ride inside
    # digests it marks non-event — notably POLITICO NatSec Daily's "Tomorrow
    # Today" block. Include any email carrying that marker regardless of is_event.
    # Personal/professional mail is excluded outright — a meeting a colleague set
    # up is the reader's own business, not a public DC-area event listing.
    candidates = [e for e in emails
                  if not e.get("personal")
                  and (e.get("is_event") or _EVENT_BLOCK_RE.search(e.get("body", "")))]
    if not candidates:
        return []
    tagmap = {e["tag"]: e["link"] for e in candidates}
    events: list[dict] = []
    for start in range(0, len(candidates), ROUTE_BATCH):
        batch = candidates[start:start + ROUTE_BATCH]
        blocks = [f"[{e['tag']}] From: {e['sender']}\nSubject: {e['subject']}\n"
                  f"---\n{_event_excerpt(e)}\n" for e in batch]
        today = datetime.now().strftime("%A, %B %d, %Y")
        raw = complete(
            system=EVENTS_SYSTEM,
            user=f"Today is {today}. Only include events on or after today.\n\n"
                 "Extract events:\n\n" + "\n\n".join(blocks),
            max_tokens=1500, thinking_level="low",
            anthropic_model="claude-haiku-4-5-20251001", response_schema=EVENTS_SCHEMA,
            project="daily_brief", script="daily_brief_v2.py", label="events",
        ).strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw); raw = re.sub(r"\s*```$", "", raw)
        try:
            for ev in json.loads(raw):
                ev["link"] = tagmap.get(ev.get("tag", ""), "")
                if ev.get("title"):
                    events.append(ev)
        except json.JSONDecodeError:
            print(f"  ⚠ events batch @{start} unparseable — skipped")
    return events


# ─────────────────────────────────────────────────────────────────────────────
# Assemble
# ─────────────────────────────────────────────────────────────────────────────

def _checkbox_body(body: str) -> tuple[str, bool]:
    """Put a `- [ ] Reviewed` box under each `###` subsection heading.

    Returns (body, had_subsections). When a section HAS subsections the boxes go
    on them (finer-grained tracking); the caller then omits the section-level box.
    The box sits IMMEDIATELY under the heading — no blank line between them —
    but IS followed by exactly one blank line before the content (Obsidian
    renders a checkbox glued to the next paragraph as one run-on block). The
    model sometimes leaves its own blank line after a `### Heading` (natural
    markdown habit); since the box is inserted after the fact, that blank line
    would otherwise land between the heading and the box instead of between the
    box and the content, so the source's blank line is dropped and a single one
    is re-inserted after the box instead.

    The model also sometimes opens a topic with a throughline paragraph BEFORE
    its first `### Subtopic` (a natural way to lead a synthesis). When the body
    has real subsections, each of those carries its own box below — but that
    leading paragraph has no heading of its own to hang a box on, so it reads
    as un-tracked. Give it a synthetic `### Overview` heading (with its own
    box) so every paragraph in the section is trackable, not just the ones
    the model happened to put a subheading on.
    """
    lines = body.splitlines()
    has_subsections = any(re.match(r"^###\s+\S", l) for l in lines)
    out: list[str] = []
    found = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if i == 0 and has_subsections and not re.match(r"^###\s+\S", line):
            out += ["### Overview", "- [ ] Reviewed", ""]
        out.append(line)
        if re.match(r"^###\s+\S", line):
            found = True
            out += ["- [ ] Reviewed", ""]
            while i + 1 < len(lines) and not lines[i + 1].strip():
                i += 1
        i += 1
    return "\n".join(out), found


def _find_orphan_checkboxes(brief: str) -> list[str]:
    """Return one description per `- [ ] Reviewed` box that ISN'T immediately
    preceded (skipping only blank lines) by a heading line.

    A structural safety net, not a generator for the bug it catches. Every
    `- [ ] Reviewed` box in this file is supposed to always sit right under a
    heading — `_checkbox_body` and `_render_carried` both guarantee that when
    they run. This exists for the case where that guarantee is violated
    anyway: a stale import of this module at execution time (this exact repo
    lives on a Google Drive mount that has, on other days, served processes a
    not-yet-hydrated / stale file — see the EDEADLK fix in `daily_brief.py`'s
    dotenv loading), a future regression, or a genuinely new LLM output shape
    nobody anticipated. Silent malformed output is the actual failure mode
    worth closing off — 2026-08-12's brief shipped with one topic's checkbox
    sitting directly under metadata with no heading above it, and nothing
    caught it before Sean did by eye the next morning. This makes that loud
    instead of silent.
    """
    lines = brief.splitlines()
    orphans = []
    for i, line in enumerate(lines):
        if not re.match(r"^- \[[ xX]\] Reviewed\s*$", line):
            continue
        j = i - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        if j < 0 or not re.match(r"^#{1,6}\s+\S", lines[j]):
            context = lines[j] if j >= 0 else "(start of file)"
            orphans.append(f"line {i + 1} (preceding non-blank line: {context!r})")
    return orphans


def _render_carried(items: list[dict]) -> list[str]:
    """Re-emit unreviewed blocks from the last brief, each keeping its own box and
    its ORIGINAL date so the age cap survives repeated carrying."""
    out: list[str] = []
    for it in items:
        since = it["since"]
        try:
            pretty = datetime.strptime(since, "%Y-%m-%d").strftime("%b %-d")
        except ValueError:
            pretty = since
        out += [f"### {it['title']}",
                "- [ ] Reviewed",
                "",
                f"_Carried forward from {since} ({pretty}) — not yet reviewed._",
                "", it["body"], ""]
    return out


def assemble_brief(sections: dict[str, str], events: list[dict],
                   non_relevant: list[dict], personal: list[dict],
                   n_relevant: int, n_total: int, window_label: str,
                   carry: dict[str, list[dict]] | None = None,
                   title: str | None = None,
                   unverified: list[dict] | None = None,
                   read_before: list[dict] | None = None) -> str:
    now = datetime.now()
    carry = carry or {}
    out = [
        title or f"# Daily Intelligence Brief — {now.strftime('%B %d, %Y')}", "",
        f"*Generated {now.strftime('%H:%M')} | {n_relevant} relevant of {n_total} "
        f"emails{window_label} · trend synthesis*", "",
    ]
    for topic in TOPICS_ORDERED:
        body = sections.get(topic)
        carried = carry.get(topic, [])
        if not body and not carried:
            out += [f"## {topic}", "- [ ] Reviewed", "",
                    "_No significant developments._", ""]
            continue
        out += [f"## {topic}"]
        if body:
            # Checkboxes live on the SUBSECTIONS when a section has any; only a
            # section with no `###` subsections carries its own checkbox.
            boxed, has_subs = _checkbox_body(body)
            if not has_subs:
                out += ["- [ ] Reviewed", ""]
            out += [boxed, ""]
        elif carried:
            out += ["_No new developments today; unreviewed items below._", ""]
        out += _render_carried(carried)

    out += ["## Upcoming Events (DC Metro & Virtual)", "- [ ] Reviewed", ""]
    if events:
        for ev in events:
            link = f" · [details →]({ev['link']})" if ev.get("link") else ""
            topic = f" · _{ev['topic']}_" if ev.get("topic") else ""
            out.append(f"- **{ev['title']}** — {ev.get('when','?')} · "
                       f"{ev.get('where','?')}{topic}{link}")
        out.append("")
    else:
        out += ["_No upcoming events surfaced._", ""]

    # Coverage audit — email the router judged OFF-domain. These are LEFT UNREAD
    # in the inbox (the reader's own to-do list), so this is just a check on the
    # filter, not a list of things that were cleared.
    out += ["## Coverage",
            f"<details><summary>{len(non_relevant)} email(s) filtered as off-domain "
            f"(left unread in your inbox, not surfaced)</summary>", ""]
    for e in non_relevant:
        out.append(f"- [{_sender_name(e['sender'])}]({e['link']}) — {e['subject']}")
    out += ["", "</details>", ""]

    # Personal/professional mail is listed by SUBJECT ONLY, never summarized, and
    # never marked read — it's the reader's own correspondence to answer.
    if personal:
        out += [f"<details><summary>{len(personal)} personal/professional email(s) "
                f"— left untouched for you to handle</summary>", ""]
        for e in personal:
            out.append(f"- [{_sender_name(e['sender'])}]({e['link']}) — {e['subject']}")
        out += ["", "</details>", ""]

    # Safety net: relevant email that, for whatever reason, never made it into a
    # synthesized section (see synthesize_topic's `_verified` flag). This should
    # be structurally impossible in normal operation, but mark_gmail_read only
    # ever runs on the verified subset — if this ever fires, the guarantee "never
    # read without a summary" holds anyway, at the cost of leaving these unread
    # for a manual look instead of silently marking them read.
    if unverified:
        out += ["## ⚠ Flagged — Left Unread (Safety Check)",
                f"<details><summary>{len(unverified)} email(s) were judged relevant "
                f"but didn't make it into a synthesized section, so they were NOT "
                f"marked read — please check manually</summary>", ""]
        for e in unverified:
            out.append(f"- [{_sender_name(e['sender'])}]({e['link']}) — {e['subject']}")
        out += ["", "</details>", ""]

    # Mail already read (by you, in Gmail) before this run's fetch even started —
    # `is:unread` skipped it, so it was never routed and never appears in
    # Coverage above. Only the curated trusted-source list is checked here (see
    # find_read_before_fetch); ordinary already-read inbox traffic is expected
    # and not worth surfacing.
    if read_before:
        out += ["## Read Before This Run (Trusted Sources)",
                "_Already read in Gmail before this run started, so it was never "
                "routed or summarized. If that was you skimming it, ignore this — "
                "otherwise open it directly:_", ""]
        for e in read_before:
            out.append(f"- [{e['sender']}]({e['link']}) — {e['subject']}")
        out.append("")

    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Trend-synthesis Daily Intelligence Brief")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=FETCH_SAFETY_CAP,
                    help=f"safety cap on messages fetched (default {FETCH_SAFETY_CAP}); "
                         "the daily window is time-based, not count-based")
    ap.add_argument("--backlog", action="store_true")
    ap.add_argument("--mark-read", action="store_true")
    ap.add_argument("--before", metavar="YYYY/MM/DD",
                    help="only unread mail strictly BEFORE this date (Gmail syntax)")
    ap.add_argument("--after", metavar="YYYY/MM/DD",
                    help="only unread mail on or after this date")
    ap.add_argument("--no-carry", action="store_true",
                    help="skip carrying forward unreviewed sections from the last brief")
    ap.add_argument("--out-name", help="write the brief to this filename stem instead "
                                       "of today's date (for one-off catch-up runs)")
    ap.add_argument("--include-read", action="store_true",
                    help="include already-read mail (retrospective catch-up runs)")
    ap.add_argument("--no-vault", action="store_true",
                    help="write the file only; do NOT insert into Today.md")
    ap.add_argument("--title", help="override the brief's H1 title")
    args = ap.parse_args(argv)

    if get_ai_provider() == "gemini" and not os.environ.get("GEMINI_API_KEY"):
        print("ERROR: AI_PROVIDER=gemini but GEMINI_API_KEY not set"); sys.exit(1)

    limit = max(args.limit, 300) if args.backlog else args.limit
    if not db.ND_EMAIL:
        print("ND_EMAIL_ADDRESS not set — nothing to do."); return
    service = db.get_gmail_service()
    extra = " ".join(filter(None, [
        f"before:{args.before}" if args.before else "",
        f"after:{args.after}" if args.after else "",
    ]))
    # A plain daily run (no explicit window, not a backlog/retrospective) covers
    # everything unread since the last brief — the whole day's arrivals, however
    # heavy the day, rather than a fixed count of the newest. But if a brief for
    # TODAY already exists, this is a same-day re-run/update — switch to a full
    # date-range rescan (read + unread) of just today so a topic already written
    # up gets late arrivals folded back in, rather than looking permanently
    # "done" the moment its section is first synthesized. See
    # same_day_window_query()'s docstring.
    fetch_note = ""
    same_day_rescan = False
    if not extra and not args.backlog and not args.include_read:
        if _last_brief_date() == datetime.now().date():
            extra, fetch_note = same_day_window_query()
            same_day_rescan = True
            print("  (today's brief already exists — rebuilding it from ALL of "
                  "today's mail, read or unread, instead of just new arrivals)")
        else:
            extra, fetch_note = daily_window_query()
    emails = fetch_all_unread(service, limit, extra,
                              include_read=(args.include_read or same_day_rescan))
    print(f"Fetched {len(emails)} email(s)"
          f"{f' [{extra}]' if extra else ''}{fetch_note}.")
    if len(emails) >= limit:
        print(f"  ⚠ hit the --limit safety cap ({limit}) — some mail in the window "
              f"was not fetched; raise --limit if this recurs.")
    # Trusted-source mail already read (by you) before this fetch even ran — see
    # find_read_before_fetch's docstring-comment for why this needs its own scan
    # rather than showing up in Coverage. Skipped for --include-read (a
    # retrospective already sees read mail directly), --backlog (avoid extra API
    # cost on a big one-time clear), and same_day_rescan (a full rescan already
    # gives every one of today's already-read emails a real routing decision, so
    # flagging them here would just be a false-positive echo of that).
    read_before: list[dict] = []
    if not args.include_read and not args.backlog and not same_day_rescan:
        read_before = find_read_before_fetch(service, extra)
        if read_before:
            print(f"  ⚠ {len(read_before)} trusted-source email(s) already read "
                  f"before this run — never routed, listed in the brief for visibility.")

    if not emails and not read_before:
        print("Nothing to brief."); return

    print("Stage 1 — routing…")
    route_emails(emails)
    relevant = [e for e in emails if e.get("relevant")]
    personal = [e for e in emails if e.get("personal")]
    non_relevant = [e for e in emails if not e.get("relevant") and not e.get("personal")]
    print(f"  {len(relevant)} relevant of {len(emails)} (nexus-filtered); "
          f"{len(personal)} personal/professional held back.")

    # Window span of the RELEVANT mail — a wide span means a missed-day catch-up
    # (or the backlog), which triggers expanded, more-thorough synthesis so a
    # longer window doesn't compress important detail away.
    window_days = 0
    window_label = ""
    dates = [e["date"].date() for e in relevant if e.get("date")]
    if dates:
        window_days = (max(dates) - min(dates)).days
        if window_days >= 1:
            window_label = (f" spanning {min(dates).strftime('%b %-d')}–"
                            f"{max(dates).strftime('%b %-d')}")

    by_topic: dict[str, list[dict]] = {}
    for e in relevant:
        by_topic.setdefault(e.get("topic") or "Other", []).append(e)

    # Collected BEFORE synthesis (not after) so each topic's unreviewed backlog
    # can be handed to that topic's OWN synthesis call and merged into one
    # updated section, instead of being tacked on afterward as a separate,
    # frozen, potentially-redundant block (see SYNTH_SYSTEM's ONGOING THREADS
    # rule).
    carry = {} if args.no_carry else collect_carryover()
    if carry:
        print(f"Carrying forward {sum(len(v) for v in carry.values())} unreviewed "
              f"block(s) from the last brief.")

    print("Stage 2 — synthesizing per topic…")
    global_tagmap = {e["tag"]: e["link"] for e in emails}
    global_namemap = {e["tag"]: _sender_name(e["sender"]) for e in emails}
    sections: dict[str, str] = {}
    merged_topics: list[str] = []
    for topic in TOPICS_ORDERED:
        bucket = by_topic.get(topic)
        if bucket:
            expanded = (args.backlog or len(bucket) >= EXPAND_EMAIL_THRESHOLD
                        or window_days >= 2)
            carried_items = carry.get(topic)
            note = f" + {len(carried_items)} ongoing thread(s)" if carried_items else ""
            print(f"  {topic}: {len(bucket)} email(s){note}{' [expanded]' if expanded else ''}")
            sections[topic] = synthesize_topic(topic, bucket, expanded,
                                               global_tagmap, global_namemap,
                                               carried=carried_items)
            if carried_items:
                merged_topics.append(topic)

    # Threads folded into a fresh synthesis above are now represented there —
    # drop them from `carry` so assemble_brief()'s separate _render_carried()
    # path doesn't ALSO re-emit them as a parallel frozen block. Only topics
    # with no fresh mail at all (nothing to merge into) still flow through
    # that path unchanged.
    for topic in merged_topics:
        carry.pop(topic, None)

    print("Extracting events…")
    events = extract_events(emails)
    print(f"  {len(events)} event(s).")

    # Safety net (see synthesize_topic's `_verified` flag and assemble_brief's
    # "Flagged" section): only mark read what's actually verifiable in the brief
    # we're about to write. This should always be everything in `relevant` — a
    # gap here means a future bug, not today's behavior — but the guarantee is
    # enforced here rather than assumed.
    to_mark = [e for e in relevant if e.get("_verified")]
    unverified = [e for e in relevant if not e.get("_verified")]
    if unverified:
        print(f"  ⚠ {len(unverified)} relevant email(s) not verified in any "
              f"synthesized section — will NOT be marked read.")

    brief = assemble_brief(sections, events, non_relevant, personal,
                           len(relevant), len(emails), window_label, carry,
                           title=args.title, unverified=unverified,
                           read_before=read_before)

    orphans = _find_orphan_checkboxes(brief)
    if orphans:
        print(f"  ⚠⚠⚠ STRUCTURAL WARNING: {len(orphans)} checkbox(es) with no "
              f"heading directly above them — a section is about to ship "
              f"without a visible header. This should be impossible; "
              f"investigate _checkbox_body / the module actually loaded at "
              f"runtime. Offending spot(s):")
        for o in orphans:
            print(f"      {o}")

    stem = args.out_name or datetime.now().strftime("%Y-%m-%d")
    if args.dry_run:
        preview = db.OUTPUT_DIR / f"brief_preview_{stem}.md"
        preview.write_text(brief, encoding="utf-8")
        print(f"\n[DRY RUN] Preview → {preview}\n[DRY RUN] Nothing marked read.\n")
        print("=" * 70); print(brief)
        return

    outfile = db.OUTPUT_DIR / f"brief_{stem}.md"
    outfile.write_text(brief, encoding="utf-8")
    print(f"Brief saved → {outfile.name}")
    if db.VAULT_TODAY_PATH and not args.no_vault:
        db.insert_into_today(brief, Path(db.VAULT_TODAY_PATH))
    # Mark read ONLY the emails that were briefed (relevant/synthesized). Off-domain
    # mail is LEFT UNREAD on purpose — the reader parks credit-card/student-loan/
    # philosophy mail in the inbox as a to-do list, and the brief must not touch it.
    # Personal/professional mail is excluded upstream (relevant is forced False).
    # A retrospective (--include-read) NEVER marks anything: it is re-reading mail
    # that was already handled, and the unread residue in that window is unread on
    # purpose.
    if args.include_read:
        print("  (retrospective run: nothing marked read)")
    elif args.mark_read or not args.backlog:
        try:
            db.mark_gmail_read(service, [e["id"] for e in to_mark])
        except Exception as exc:
            print(f"  ⚠ Could not mark read: {exc}")
    else:
        print(f"  (backlog run: re-run with --mark-read to clear the {len(to_mark)} briefed emails)")
    print("\nAll done.")


if __name__ == "__main__":
    main()
