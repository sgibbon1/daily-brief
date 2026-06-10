"""usage_log.py — shared token-usage + cost logger for all AI scripts.

An identical copy lives in each project (daily_brief/, natsec_jobs/, seanipedia/).
After every Claude API call, hand the response to log_usage() and it appends one
row to a central CSV, so token usage and estimated cost are tracked across every
run — scheduled or manual — ahead of any future per-token billing.

    from usage_log import log_usage
    resp = client.messages.create(...)
    log_usage(resp, project="natsec_jobs", script="score_jobs.py",
              model="claude-haiku-4-5", label="score")

The CSV defaults to <ai_code>/token_usage.csv (one shared file across all three
projects); override with the AI_USAGE_LOG env var. Logging NEVER raises — a
logging failure must never break a pipeline run.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from pathlib import Path

# Central, cross-project log. Lives at the ai_code root (outside every git repo),
# so it is never committed. Override with AI_USAGE_LOG if the path moves.
_DEFAULT_LOG = (
    "/Users/seanmgibbons/Library/CloudStorage/GoogleDrive-sgibbons303@gmail.com"
    "/My Drive/Sean/Code/ai_code/token_usage.csv"
)
USAGE_LOG = Path(os.environ.get("AI_USAGE_LOG", _DEFAULT_LOG))

# $ per 1,000,000 tokens, as (input, output). Cache-write tokens bill at 1.25×
# input and cache-read tokens at 0.10× input (Anthropic's prompt-cache rates).
# Keys are matched as a PREFIX so date-suffixed ids (…-20251001) still resolve.
_PRICES = {
    "claude-opus-4-7":   (5.0, 25.0),
    "claude-opus-4-6":   (5.0, 25.0),
    "claude-opus-4-5":   (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5":  (1.0,  5.0),
}

_FIELDS = [
    "timestamp", "project", "script", "model", "label",
    "input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens",
    "total_tokens", "est_cost_usd",
]


def _price_key(model: str) -> str:
    m = (model or "").strip()
    for key in _PRICES:
        if m.startswith(key):
            return key
    return ""


def estimate_cost(model: str, inp: int, out: int, cache_w: int, cache_r: int) -> float:
    """Estimated USD for one call. 0.0 if the model isn't in the price table."""
    key = _price_key(model)
    if not key:
        return 0.0
    in_price, out_price = _PRICES[key]
    return (
        inp     * in_price        / 1_000_000
        + cache_w * in_price * 1.25 / 1_000_000
        + cache_r * in_price * 0.10 / 1_000_000
        + out     * out_price       / 1_000_000
    )


def log_usage(response, *, project: str, script: str, model: str, label: str = "") -> None:
    """Append one token-usage row for a Claude response. Never raises.

    `response` is the object returned by client.messages.create(); its `.usage`
    carries input/output and (when prompt caching is on) cache token counts.
    """
    try:
        u = getattr(response, "usage", None)
        inp     = int(getattr(u, "input_tokens", 0) or 0)
        out     = int(getattr(u, "output_tokens", 0) or 0)
        cache_w = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
        cache_r = int(getattr(u, "cache_read_input_tokens", 0) or 0)
        total   = inp + out + cache_w + cache_r
        cost    = estimate_cost(model, inp, out, cache_w, cache_r)

        USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        new_file = not USAGE_LOG.exists()
        with USAGE_LOG.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(_FIELDS)
            writer.writerow([
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                project, script, model, label,
                inp, out, cache_w, cache_r, total, f"{cost:.6f}",
            ])
    except Exception:
        # A logging failure must never break a pipeline run.
        pass
