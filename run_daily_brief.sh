#!/bin/bash
# Run email_scan.py first, then the brief. Both edit Today.md (email_scan writes
# ## Notes, daily_brief writes ## Daily Intelligence Brief). On a late wake both
# missed jobs fire at once, so serialize them here to avoid a read-modify-write
# clobber of Today.md. email_scan is idempotent, so re-running it is safe.
#
# Paths below are resolved relative to this script's own location, so no
# machine-specific absolute path needs editing — clone this repo as a sibling
# of `seanipedia/` under the same parent folder and it works as-is.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_CODE_DIR="$(dirname "$SCRIPT_DIR")"

# Wait for the network before hitting IMAP / Gmail API / Anthropic. This job
# fires at 17:00, but if that was missed it runs on wake-from-sleep when DNS is
# often not ready yet — which on Jun 2 produced a DNS gaierror in email_scan and
# a 0-email brief. Mirror the DNS-wait used by run_daily.sh / run_weekly.sh.
# We proceed anyway after the timeout (no exit): daily_brief.py self-heals by
# back-filling missed dates on its next run, so a hard abort buys us nothing.
echo "[INFO] $(date '+%F %T') Waiting 20s for DNS to stabilise after network reconnect..."
sleep 20
for i in $(seq 1 60); do
    if host generativelanguage.googleapis.com >/dev/null 2>&1 && host gmail.googleapis.com >/dev/null 2>&1; then
        echo "[INFO] DNS ready after $((20 + i * 5))s."
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "[WARN] DNS unavailable after 320s — running anyway (daily_brief back-fills next run)."
        break
    fi
    sleep 5
done

/usr/bin/python3 "$AI_CODE_DIR/seanipedia/email_scan.py" \
  >> "$HOME/Library/Logs/email-scan.log" 2>&1

cd "$SCRIPT_DIR"
# daily_brief_v2.py — trend-synthesis brief (replaced the per-email daily_brief.py
# on 2026-07-28). v2 fetches all unread, routes by geopolitical/intel/tech nexus,
# synthesizes one trend narrative per topic, marks ONLY the briefed emails read
# (off-domain mail stays unread — Sean's to-do list), and auto-expands detail on
# multi-day catch-ups. The old daily_brief.py is kept in the repo for reference.
#
# Log target is LOCAL disk, not output/cron.log on the Drive mount. On 2026-08-04
# every one of the 3 resilient_run retries did all its real work (brief written,
# inserted into Today.md, relevant email marked read) but still exited 120 — the
# exact code CPython returns when Py_FinalizeEx() fails to flush stdout/stderr at
# shutdown. The flush target was output/cron.log, on the same Drive mount that
# throws EDEADLK on placeholder files elsewhere in this pipeline (see
# resilient_run.sh's header comment). resilient_run.sh then retried the "failed"
# run two more times, each re-fetching only what was STILL unread and overwriting
# the previous attempt's (larger, correct) brief_2026-08-04.md with a smaller one
# — the earlier attempts' already-marked-read email was gone for good. Logging to
# local disk removes the flush failure at the source, so a successful run reports
# rc=0 and never gets cannibalized by a retry.
/usr/bin/python3 daily_brief_v2.py >> "$HOME/Library/Logs/daily-brief-detail.log" 2>&1
