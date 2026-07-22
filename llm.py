"""llm.py — provider-agnostic chat completion (Anthropic or Gemini).

Identical copy in each project (daily_brief/, natsec_jobs/, seanipedia/). Pick the
provider with AI_PROVIDER in .env: "anthropic" (default) or "gemini". Every call is
logged via usage_log.log_usage, so token/cost tracking works on either provider.

    from llm import complete
    text = complete(system="You are…", user="Summarize X",
                    max_tokens=700, anthropic_model="claude-sonnet-4-6",
                    project="natsec_jobs", script="score_jobs.py", label="cover")

Gemini setup (free tier): get a key at aistudio.google.com, then in .env set:
    AI_PROVIDER=gemini
    GEMINI_API_KEY=AQ...
    GEMINI_MODEL=gemini-2.5-flash       # optional; this is the default

Gemini calls go straight to the REST "Interactions API" (generativelanguage.
googleapis.com/v1beta/interactions) via `requests` — NOT the `google-generativeai`
SDK, which Google has fully deprecated ("no longer receiving updates or bug
fixes"). No extra package needed; every caller here already depends on `requests`.

IMPORTANT: unlike Claude, Gemini's "thinking" tokens are deducted from the SAME
max_output_tokens budget as the actual answer — a long system prompt (e.g. a
full resume) can burn most or all of a tight max_tokens on thinking alone,
truncating the real output mid-JSON with no error, just a short/malformed
string. Pass thinking_level="minimal" for trivial single-judgment calls (a
score, a classification) and leave generous max_tokens headroom (200 was not
enough for one real case — 190 of 200 tokens went to thinking). "low" (the
default) is fine for genuine synthesis work that benefits from more reasoning
(validated against real weekly-report generation).
"""
from __future__ import annotations

import os
import time

import requests

try:
    from usage_log import log_usage
except Exception:  # pragma: no cover
    def log_usage(*a, **k):
        pass

_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"


def get_ai_provider() -> str:
    """Read AI_PROVIDER fresh on every call — NOT a module-level constant.

    `from llm import AI_PROVIDER` would bind a snapshot of os.environ at
    IMPORT time. Every caller in this codebase imports llm before calling
    load_dotenv() (or, in natsec_jobs, never calls load_dotenv() in Python
    at all — env vars arrive via the shell wrapper sourcing .env first), so
    a frozen constant silently stuck on the "anthropic" default regardless
    of .env. Call this function instead of importing a constant.
    """
    return os.environ.get("AI_PROVIDER", "anthropic").strip().lower()


def _default_gemini_model() -> str:
    return os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()

# Lazily-initialised, cached Anthropic client (so importing this module never
# requires a key for the provider you're NOT using).
_anthropic_client = None


def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _anthropic_client


def _gemini_output_text(resp_json: dict) -> str:
    """Walk an Interactions API response for its model_output text."""
    chunks: list[str] = []
    for step in resp_json.get("steps", []):
        if step.get("type") != "model_output":
            continue
        for part in step.get("content", []):
            if part.get("type") == "text" and part.get("text"):
                chunks.append(part["text"])
    if not chunks:
        raise RuntimeError(f"Gemini response had no model_output text: {resp_json}")
    return "\n".join(chunks)


def _log_gemini(resp_json: dict, model: str, project: str, script: str, label: str) -> None:
    """Adapt the Interactions API's usage block to the usage_log row shape."""
    usage = resp_json.get("usage", {}) or {}

    class _U:
        input_tokens = int(usage.get("total_input_tokens", 0) or 0)
        output_tokens = int(usage.get("total_output_tokens", 0) or 0)
        cache_creation_input_tokens = 0
        cache_read_input_tokens = int(usage.get("total_cached_tokens", 0) or 0)

    class _R:
        usage = _U()

    log_usage(_R(), project=project, script=script, model=model, label=label)


def _call_gemini(system: str, user: str, max_tokens: int, model: str, thinking_level: str) -> dict:
    resp = requests.post(
        _GEMINI_URL,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": os.environ["GEMINI_API_KEY"],
        },
        json={
            "model": model,
            "system_instruction": system or None,
            "input": user,
            "generation_config": {
                "max_output_tokens": max_tokens,
                "thinking_level": thinking_level,
            },
        },
        timeout=120,
    )
    if resp.status_code >= 400:
        # Keep "rate"/429/503-shaped errors recognizable to the retry loop below.
        raise RuntimeError(f"Gemini API error {resp.status_code}: {resp.text}")
    return resp.json()


def complete(*, system: str, user: str, max_tokens: int, anthropic_model: str,
             gemini_model: str | None = None, project: str, script: str,
             label: str = "", max_retries: int = 6, thinking_level: str = "low") -> str:
    """Run one completion on the active provider; log usage; return the text.

    `anthropic_model` is used when AI_PROVIDER=anthropic; `gemini_model`
    (defaulting to GEMINI_MODEL) is used when AI_PROVIDER=gemini.

    `thinking_level` (Gemini only): "minimal", "low" (default), "medium", or
    "high". Use "minimal" for a trivial single-judgment call (a score, a
    classification) to guarantee max_tokens headroom for the actual answer —
    see the module docstring for why this matters on Gemini specifically.
    """
    gem_model = gemini_model or _default_gemini_model()

    for attempt in range(max_retries):
        try:
            if get_ai_provider() == "gemini":
                resp_json = _call_gemini(system, user, max_tokens, gem_model, thinking_level)
                _log_gemini(resp_json, gem_model, project, script, label)
                return _gemini_output_text(resp_json)

            client = _get_anthropic()
            resp = client.messages.create(
                model=anthropic_model,
                max_tokens=max_tokens,
                # Cache the (stable) system prompt to cut input cost on Anthropic.
                system=[{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}] if system else [],
                messages=[{"role": "user", "content": user}],
            )
            log_usage(resp, project=project, script=script,
                      model=anthropic_model, label=label)
            return resp.content[0].text

        except Exception as exc:  # retry transient rate/availability errors
            err = str(exc).lower()
            transient = any(s in err for s in
                            ("rate", "429", "quota", "overloaded", "529", "503"))
            if transient and attempt < max_retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            raise

    raise RuntimeError("LLM call exceeded max retries")
