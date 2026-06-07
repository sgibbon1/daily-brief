#!/bin/bash
# Run email_scan.py first, then the brief. Both edit Today.md (email_scan writes
# ## Notes, daily_brief writes ## Daily Intelligence Brief). On a late wake both
# missed jobs fire at once, so serialize them here to avoid a read-modify-write
# clobber of Today.md. email_scan is idempotent, so re-running it is safe.

# Wait for the network before hitting IMAP / Gmail API / Anthropic. This job
# fires at 17:00, but if that was missed it runs on wake-from-sleep when DNS is
# often not ready yet — which on Jun 2 produced a DNS gaierror in email_scan and
# a 0-email brief. Mirror the DNS-wait used by run_daily.sh / run_weekly.sh.
# We proceed anyway after the timeout (no exit): daily_brief.py self-heals by
# back-filling missed dates on its next run, so a hard abort buys us nothing.
echo "[INFO] $(date '+%F %T') Waiting 20s for DNS to stabilise after network reconnect..."
sleep 20
for i in $(seq 1 60); do
    if host api.anthropic.com >/dev/null 2>&1 && host gmail.googleapis.com >/dev/null 2>&1; then
        echo "[INFO] DNS ready after $((20 + i * 5))s."
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "[WARN] DNS unavailable after 320s — running anyway (daily_brief back-fills next run)."
        break
    fi
    sleep 5
done

/usr/bin/python3 "/Users/seanmgibbons/Library/CloudStorage/GoogleDrive-sgibbons303@gmail.com/My Drive/Sean/Code/ai_code/seanipedia/email_scan.py" \
  >> "/Users/seanmgibbons/Library/Logs/email-scan.log" 2>&1

cd "/Users/seanmgibbons/Library/CloudStorage/GoogleDrive-sgibbons303@gmail.com/My Drive/Sean/Code/ai_code/daily_brief"
/usr/bin/python3 daily_brief.py >> output/cron.log 2>&1
