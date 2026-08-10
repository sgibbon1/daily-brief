#!/usr/bin/env python3
"""daily_brief_v2.py — trend-synthesis rewrite of the Daily Intelligence Brief.

New model (vs the per-email daily_brief.py it will replace):
  - One synthesized narrative per topic area surfacing the day's TRENDS across all
    sources — no more single-journalist snapshot / single-datapoint overreaction.
  - Two LLM stages (map-reduce):
      Stage 1 ROUTE   — cheap batched classify: {relevant?, which topic?, event?}.
                        Tiny schema-enforced JSON; nothing to mis-escape.
      Stage 2 SYNTH   — one call per topic; reads all that topic's email PLUS the
                        last few days' coverage of the same topic (cross-day trend
                        memory); writes markdown with `###` subtopics and inline
                        `[desc](E#)` TAG citations that we substitute for real URLs.
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
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

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

CONTEXT_DAYS = 5
CONTEXT_CHARS = 2500
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
# Fetch — all unread (not date-windowed)
# ─────────────────────────────────────────────────────────────────────────────

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
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        # Normalize to timezone-AWARE. parsedate_to_datetime returns an aware
        # datetime when the header carries an offset (nearly always) and a naive
        # one when it doesn't; mixing the two makes any sort or subtraction throw
        # "can't compare offset-naive and offset-aware datetimes".
        try:
            date = parsedate_to_datetime(headers.get("Date", ""))
        except Exception:
            date = None
        if date is None:
            date = datetime.now(timezone.utc)
        elif date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return {
            "id": ref["id"],
            "account": "ND Alumni (alumni.nd.edu)",
            "subject": db.decode_mime_words(headers.get("Subject", "(no subject)")),
            "sender": headers.get("From", "Unknown"),
            "date": date,
            "body": db._extract_gmail_body(msg["payload"])[:db.MAX_BODY_CHARS_FETCH],
            "link": f"https://mail.google.com/mail/u/0/#inbox/{ref['id']}",
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
            max_tokens=1600, thinking_level="minimal",
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


def recent_context(topic: str, n_days: int = CONTEXT_DAYS) -> str:
    d = _archive_dir()
    if not d:
        return ""
    files = sorted(d.glob("*.md"), reverse=True)[:n_days]
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
    return ("\n\n".join(slices))[:CONTEXT_CHARS]


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
#     date, and re-carrying preserves it — otherwise the age cap would never fire
#     and an ignored item would live forever.

CARRY_MAX_DAYS = 7
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
    cutoff = datetime.now().date() - timedelta(days=CARRY_MAX_DAYS)

    # Topics may sit at ## (raw brief) or ### (archived copy) — find their level.
    levels = {lv for m in _HEADING_RE.finditer(text)
              for lv, t in [(len(m.group(1)), m.group(2).strip())] if t in _VALID_TOPICS}
    if not levels:
        return {}
    tlevel = min(levels)

    def _age_ok(body: str) -> tuple[bool, str]:
        m = _CARRY_LINE_RE.search(body)
        since = m.group(1) if m else prior_date
        try:
            keep = datetime.strptime(since, "%Y-%m-%d").date() >= cutoff
        except ValueError:
            keep = True
        return keep, since

    def _clean(body: str) -> str:
        body = _REVIEWED_BOX_RE.sub("", body)
        body = _CARRY_LINE_RE.sub("", body)
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
                keep, since = _age_ok(sub_body)
                cleaned = _clean(sub_body)
                if keep and cleaned:
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
            keep, since = _age_ok(body)
            cleaned = _clean(body)
            if keep and cleaned and "_No significant developments._" not in cleaned:
                carry.setdefault(topic.strip(), []).append({
                    "title": "Carried forward",
                    "body": _shift_headings(cleaned, 3 - (tlevel + 1)),
                    "since": since})
    return carry


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — SYNTHESIZE one topic (markdown, tag-cited)
# ─────────────────────────────────────────────────────────────────────────────

SYNTH_SYSTEM = """You are a senior intelligence analyst writing ONE topic section of a DoD official's daily brief.

You get today's emails on this topic (often several outlets), each labeled with a TAG like [E3], and this topic's coverage from the last few days for context. Write a concise, trend-focused synthesis — the throughline of what's developing, not a list of what each email said.

Rules:
- Lead with the day's throughline; where a recurring pattern warrants it, use `### Subtopic` headings.
- AGGREGATE across sources. When multiple independent outlets converge, say so (real trend). When a claim rests on a SINGLE source, attribute it and don't inflate it. Don't overreact to one datapoint.
- Use the prior-days context to note whether a theme is BUILDING, CONTINUING, or FADING — only when the context supports it. Refer to prior days in PLAIN PROSE (e.g. "as reported earlier this week"); do NOT wrap prior-days context in link syntax — only today's tagged emails can be linked.
- CITATIONS — follow this format exactly: cite a development as a markdown link whose bracket text describes it and whose target is the email's TAG. Example: `Nvidia launched a cyberdefense alliance [Open Secure AI Alliance](E17).` Real descriptive words in the brackets, the bare tag (E17) in the parens. NEVER write a bare `(E17)` or `[E17]` on its own, and NEVER write a raw URL. Every email you draw from must be cited this way. Don't invent tags.
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


def _synth_call(topic: str, blocks: list[str], context: str,
                is_reduce: bool, expanded: bool) -> str:
    guidance = TOPIC_GUIDANCE.get(topic, "")
    if is_reduce:
        user = (f"TOPIC: {topic}\n\nMERGE these partial write-ups into one clean, "
                f"well-organized section. Remove repetition, KEEP every [E#] tag "
                f"citation and every distinct development."
                f"{guidance}{_EXPAND_NOTE if expanded else ''}\n\nPARTIALS:\n\n"
                + "\n\n---\n\n".join(blocks))
    else:
        user = (f"TOPIC: {topic}\n\nPRIOR COVERAGE (last {CONTEXT_DAYS} days, context "
                f"only):\n{context or '(none available)'}"
                f"{guidance}{_EXPAND_NOTE if expanded else ''}\n\nEMAILS:\n\n"
                + "\n\n".join(blocks))
    return complete(
        system=SYNTH_SYSTEM, user=user,
        max_tokens=4200 if expanded else 2600, thinking_level="low",
        anthropic_model="claude-sonnet-4-6",
        project="daily_brief", script="daily_brief_v2.py", label="synthesize",
    ).strip()


def _apply_tags(md: str, tagmap: dict[str, str]) -> str:
    """Turn every email-tag citation into a real clickable URL, whatever form the
    model emitted — proper `[desc](E#)`, bare `(E#)`, or bare `[E#]`. Unknown tags
    are dropped entirely. Guarantees no fabricated/cross-wired URL and no raw tag
    marker leaking into the brief."""
    # 1) proper [desc](E#) → [desc](url); unknown → keep descriptor as plain text
    md = re.sub(
        r"\[([^\]]+)\]\((E\d+)\)",
        lambda m: f"[{m.group(1)}]({tagmap[m.group(2)]})" if m.group(2) in tagmap else m.group(1),
        md)
    # 2) bare (E#) → a compact ([↗](url)); unknown → drop
    md = re.sub(
        r"\((E\d+)\)",
        lambda m: f"([↗]({tagmap[m.group(1)]}))" if m.group(1) in tagmap else "",
        md)
    # 3) bare [E#] → [↗](url); unknown → drop
    md = re.sub(
        r"\[(E\d+)\]",
        lambda m: f"[↗]({tagmap[m.group(1)]})" if m.group(1) in tagmap else "",
        md)
    # 4) unwrap any leftover [text](target) whose target isn't a real URL — the
    #    model sometimes "links" cross-day context to a date or source name
    #    (e.g. [US strikes on Iran](20260724)); those have no clickable target.
    md = re.sub(r"\[([^\]]+)\]\((?!https?://)[^)]*\)", r"\1", md)
    return re.sub(r"[ ]{2,}", " ", md)  # tidy any double spaces left by a drop


def synthesize_topic(topic: str, emails: list[dict], expanded: bool,
                     tagmap: dict[str, str] | None = None) -> str:
    context = recent_context(topic)
    blocks = [_synth_block(e) for e in emails]
    # Sub-batch (map-reduce) whenever the bucket is large — a natural multi-day
    # catch-up gets the same completeness treatment as an explicit --backlog run.
    if len(blocks) > SYNTH_SUBBATCH:
        partials = [_synth_call(topic, blocks[s:s + SYNTH_SUBBATCH], context, False, expanded)
                    for s in range(0, len(blocks), SYNTH_SUBBATCH)]
        raw = _synth_call(topic, partials, context, is_reduce=True, expanded=expanded)
    else:
        raw = _synth_call(topic, blocks, context, is_reduce=False, expanded=expanded)

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
    missing = [e for e in emails if e["tag"] not in cited]
    body = _apply_tags(raw, tagmap)
    if missing:
        body += "\n\n### Also noted\n"
        for e in missing:
            body += f"- [{e['subject']}]({e['link']}) — {_sender_name(e['sender'])}\n"
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
            max_tokens=1500, thinking_level="minimal",
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
    The box sits IMMEDIATELY under the heading — no blank line between them.
    """
    lines = body.splitlines()
    out: list[str] = []
    found = False
    for line in lines:
        out.append(line)
        if re.match(r"^###\s+\S", line):
            found = True
            out += ["- [ ] Reviewed"]
    return "\n".join(out), found


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
                f"_Carried forward from {since} ({pretty}) — not yet reviewed._",
                "", it["body"], ""]
    return out


def assemble_brief(sections: dict[str, str], events: list[dict],
                   non_relevant: list[dict], personal: list[dict],
                   n_relevant: int, n_total: int, window_label: str,
                   carry: dict[str, list[dict]] | None = None,
                   title: str | None = None) -> str:
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
            out += [f"## {topic}", "- [ ] Reviewed",
                    "_No significant developments._", ""]
            continue
        out += [f"## {topic}"]
        if body:
            # Checkboxes live on the SUBSECTIONS when a section has any; only a
            # section with no `###` subsections carries its own checkbox.
            boxed, has_subs = _checkbox_body(body)
            if not has_subs:
                out += ["- [ ] Reviewed"]
            out += [boxed, ""]
        elif carried:
            out += ["_No new developments today; unreviewed items below._", ""]
        out += _render_carried(carried)

    out += ["## Upcoming Events (DC Metro & Virtual)", "- [ ] Reviewed"]
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
        out.append(f"- [{e['subject']}]({e['link']}) — {_sender_name(e['sender'])}")
    out += ["", "</details>", ""]

    # Personal/professional mail is listed by SUBJECT ONLY, never summarized, and
    # never marked read — it's the reader's own correspondence to answer.
    if personal:
        out += [f"<details><summary>{len(personal)} personal/professional email(s) "
                f"— left untouched for you to handle</summary>", ""]
        for e in personal:
            out.append(f"- [{e['subject']}]({e['link']}) — {_sender_name(e['sender'])}")
        out += ["", "</details>", ""]
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Trend-synthesis Daily Intelligence Brief")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=60)
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
    emails = fetch_all_unread(service, limit, extra, include_read=args.include_read)
    print(f"Fetched {len(emails)} unread email(s)"
          f"{f' [{extra}]' if extra else ''}.")
    if not emails:
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

    print("Stage 2 — synthesizing per topic…")
    global_tagmap = {e["tag"]: e["link"] for e in emails}
    sections: dict[str, str] = {}
    for topic in TOPICS_ORDERED:
        bucket = by_topic.get(topic)
        if bucket:
            expanded = (args.backlog or len(bucket) >= EXPAND_EMAIL_THRESHOLD
                        or window_days >= 2)
            print(f"  {topic}: {len(bucket)} email(s){' [expanded]' if expanded else ''}")
            sections[topic] = synthesize_topic(topic, bucket, expanded, global_tagmap)

    print("Extracting events…")
    events = extract_events(emails)
    print(f"  {len(events)} event(s).")

    carry = {} if args.no_carry else collect_carryover()
    if carry:
        print(f"Carrying forward {sum(len(v) for v in carry.values())} unreviewed "
              f"block(s) from the last brief.")

    brief = assemble_brief(sections, events, non_relevant, personal,
                           len(relevant), len(emails), window_label, carry,
                           title=args.title)

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
            db.mark_gmail_read(service, [e["id"] for e in relevant])
        except Exception as exc:
            print(f"  ⚠ Could not mark read: {exc}")
    else:
        print(f"  (backlog run: re-run with --mark-read to clear the {len(relevant)} briefed emails)")
    print("\nAll done.")


if __name__ == "__main__":
    main()
