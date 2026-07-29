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
from datetime import datetime
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
SYNTH_BODY_CHARS = 1800
SYNTH_BODY_CHARS_TRUSTED = 6000
ROUTE_BATCH = 12
SYNTH_SUBBATCH = 12


# ─────────────────────────────────────────────────────────────────────────────
# Fetch — all unread (not date-windowed)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_all_unread(service, limit: int) -> list[dict]:
    result = service.users().messages().list(
        userId="me", q="is:unread", maxResults=limit
    ).execute(num_retries=db.API_RETRIES)
    messages = result.get("messages", [])[:limit]
    emails = []
    for ref in messages:
        msg = service.users().messages().get(
            userId="me", id=ref["id"], format="full"
        ).execute(num_retries=db.API_RETRIES)
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        try:
            date = parsedate_to_datetime(headers.get("Date", ""))
        except Exception:
            date = datetime.now()
        emails.append({
            "id": ref["id"],
            "account": "ND Alumni (alumni.nd.edu)",
            "subject": db.decode_mime_words(headers.get("Subject", "(no subject)")),
            "sender": headers.get("From", "Unknown"),
            "date": date,
            "body": db._extract_gmail_body(msg["payload"])[:db.MAX_BODY_CHARS_FETCH],
            "link": f"https://mail.google.com/mail/u/0/#inbox/{ref['id']}",
        })
    # Stable tags for citation/substitution (E1, E2, …).
    for i, e in enumerate(emails):
        e["tag"] = f"E{i + 1}"
    return emails


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

Set relevant=FALSE — do not force into "Other" — for anything WITHOUT that nexus, even if substantive and worth the reader's time: philosophy/ideas essays, personal finance (credit cards, student loans), religious/devotional readings, purely domestic partisan politics with no national-security angle, personal or lifestyle newsletters, marketing, logistics, fundraising. The reader keeps these unread in their own inbox as a to-do list; the brief must leave them alone.

Emails tagged [TRUSTED SOURCE] are curated intel/policy/tech publications — lean relevant, but still require the nexus.

Return ONLY a JSON array, one object per email in order:
{"email_index": <int>, "relevant": <bool>, "topic": "<area or 'Other'>", "is_event": <bool>}."""

ROUTE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "email_index": {"type": "integer"},
            "relevant": {"type": "boolean"},
            "topic": {"type": "string"},
            "is_event": {"type": "boolean"},
        },
        "required": ["email_index", "relevant", "topic", "is_event"],
    },
}


def route_emails(emails: list[dict]) -> None:
    for start in range(0, len(emails), ROUTE_BATCH):
        batch = emails[start:start + ROUTE_BATCH]
        blocks = []
        for i, e in enumerate(batch):
            trusted = any(p in e.get("sender", "").lower() for p in db.TRUSTED_SENDERS)
            tag = " [TRUSTED SOURCE]" if trusted else ""
            blocks.append(
                f"EMAIL {i}{tag}\nFrom: {e['sender']}\nSubject: {e['subject']}\n"
                f"---\n{e['body'][:1200]}\n"
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
            for e in batch:
                e["relevant"], e["topic"], e["is_event"] = True, "Other", False
            print(f"  ⚠ routing batch @{start} unparseable — kept as Other")
            continue
        for v in verdicts:
            idx = v.get("email_index", -1)
            if 0 <= idx < len(batch):
                topic = v.get("topic", "Other")
                batch[idx]["relevant"] = bool(v.get("relevant"))
                batch[idx]["topic"] = topic if topic in _VALID_TOPICS else "Other"
                batch[idx]["is_event"] = bool(v.get("is_event"))
        for e in batch:
            e.setdefault("relevant", False)
            e.setdefault("topic", None)
            e.setdefault("is_event", False)


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
    trusted = any(p in e.get("sender", "").lower() for p in db.TRUSTED_SENDERS)
    limit = SYNTH_BODY_CHARS_TRUSTED if trusted else SYNTH_BODY_CHARS
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


def _synth_call(topic: str, blocks: list[str], context: str,
                is_reduce: bool, expanded: bool) -> str:
    if is_reduce:
        user = (f"TOPIC: {topic}\n\nMERGE these partial write-ups into one clean, "
                f"well-organized section. Remove repetition, KEEP every [E#] tag "
                f"citation and every distinct development."
                f"{_EXPAND_NOTE if expanded else ''}\n\nPARTIALS:\n\n"
                + "\n\n---\n\n".join(blocks))
    else:
        user = (f"TOPIC: {topic}\n\nPRIOR COVERAGE (last {CONTEXT_DAYS} days, context "
                f"only):\n{context or '(none available)'}"
                f"{_EXPAND_NOTE if expanded else ''}\n\nEMAILS:\n\n"
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


def synthesize_topic(topic: str, emails: list[dict], expanded: bool) -> str:
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
    tagmap = {e["tag"]: e["link"] for e in emails}
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

From each email labeled with a TAG, pull any specific upcoming event (talk, panel, conference, briefing, webinar) on AI/emerging tech, national security/defense, China/Indo-Pacific, economics/geopolitics, or Russia/Ukraine — that is EITHER in the Washington DC metro area (DC, Arlington, Alexandria, Bethesda, etc.) OR virtual/online. Skip past events, purely commercial webinars with no substance, and events clearly elsewhere with no virtual option.

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


def extract_events(emails: list[dict]) -> list[dict]:
    """Scan ALL fetched email (event invites are often routed non-relevant) for
    DC-area / virtual upcoming events. Returns event dicts with a real link."""
    candidates = [e for e in emails if e.get("is_event")]
    if not candidates:
        return []
    tagmap = {e["tag"]: e["link"] for e in candidates}
    events: list[dict] = []
    for start in range(0, len(candidates), ROUTE_BATCH):
        batch = candidates[start:start + ROUTE_BATCH]
        blocks = [f"[{e['tag']}] From: {e['sender']}\nSubject: {e['subject']}\n"
                  f"---\n{e['body'][:2000]}\n" for e in batch]
        raw = complete(
            system=EVENTS_SYSTEM, user="Extract events:\n\n" + "\n\n".join(blocks),
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

def assemble_brief(sections: dict[str, str], events: list[dict],
                   non_relevant: list[dict], n_relevant: int, n_total: int,
                   window_label: str) -> str:
    now = datetime.now()
    out = [
        f"# Daily Intelligence Brief — {now.strftime('%B %d, %Y')}", "",
        f"*Generated {now.strftime('%H:%M')} | {n_relevant} relevant of {n_total} "
        f"emails{window_label} · trend synthesis*", "",
    ]
    for topic in TOPICS_ORDERED:
        body = sections.get(topic)
        out += [f"## {topic}", "", "- [ ] Reviewed", "",
                body if body else "_No significant developments._", ""]

    out += ["## Upcoming Events (DC Metro & Virtual)", "", "- [ ] Reviewed", ""]
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
    out += ["## Coverage", "",
            f"<details><summary>{len(non_relevant)} email(s) filtered as off-domain "
            f"(left unread in your inbox, not surfaced)</summary>", ""]
    for e in non_relevant:
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
    args = ap.parse_args(argv)

    if get_ai_provider() == "gemini" and not os.environ.get("GEMINI_API_KEY"):
        print("ERROR: AI_PROVIDER=gemini but GEMINI_API_KEY not set"); sys.exit(1)

    limit = max(args.limit, 300) if args.backlog else args.limit
    if not db.ND_EMAIL:
        print("ND_EMAIL_ADDRESS not set — nothing to do."); return
    service = db.get_gmail_service()
    emails = fetch_all_unread(service, limit)
    print(f"Fetched {len(emails)} unread email(s).")
    if not emails:
        print("Nothing to brief."); return

    print("Stage 1 — routing…")
    route_emails(emails)
    relevant = [e for e in emails if e.get("relevant")]
    non_relevant = [e for e in emails if not e.get("relevant")]
    print(f"  {len(relevant)} relevant of {len(emails)} (nexus-filtered).")

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
    sections: dict[str, str] = {}
    for topic in TOPICS_ORDERED:
        bucket = by_topic.get(topic)
        if bucket:
            expanded = (args.backlog or len(bucket) >= EXPAND_EMAIL_THRESHOLD
                        or window_days >= 2)
            print(f"  {topic}: {len(bucket)} email(s){' [expanded]' if expanded else ''}")
            sections[topic] = synthesize_topic(topic, bucket, expanded)

    print("Extracting events…")
    events = extract_events(emails)
    print(f"  {len(events)} event(s).")

    brief = assemble_brief(sections, events, non_relevant,
                           len(relevant), len(emails), window_label)

    if args.dry_run:
        preview = db.OUTPUT_DIR / f"brief_preview_{datetime.now().strftime('%Y-%m-%d')}.md"
        preview.write_text(brief, encoding="utf-8")
        print(f"\n[DRY RUN] Preview → {preview}\n[DRY RUN] Nothing marked read.\n")
        print("=" * 70); print(brief)
        return

    outfile = db.OUTPUT_DIR / f"brief_{datetime.now().strftime('%Y-%m-%d')}.md"
    outfile.write_text(brief, encoding="utf-8")
    print(f"Brief saved → {outfile.name}")
    if db.VAULT_TODAY_PATH:
        db.insert_into_today(brief, Path(db.VAULT_TODAY_PATH))
    # Mark read ONLY the emails that were briefed (relevant/synthesized). Off-domain
    # mail is LEFT UNREAD on purpose — the reader parks credit-card/student-loan/
    # philosophy mail in the inbox as a to-do list, and the brief must not touch it.
    if args.mark_read or not args.backlog:
        try:
            db.mark_gmail_read(service, [e["id"] for e in relevant])
        except Exception as exc:
            print(f"  ⚠ Could not mark read: {exc}")
    else:
        print(f"  (backlog run: re-run with --mark-read to clear the {len(relevant)} briefed emails)")
    print("\nAll done.")


if __name__ == "__main__":
    main()
