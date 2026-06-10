"""llm.py — provider-agnostic chat completion (Anthropic or Gemini).

Identical copy in each project (daily_brief/, natsec_jobs/, seanipedia/). Pick the
provider with AI_PROVIDER in .env: "anthropic" (default) or "gemini". Every call is
logged via usage_log.log_usage, so token/cost tracking works on either provider.

    from llm import complete
    text = complete(system="You are…", user="Summarize X",
                    max_tokens=700, anthropic_model="claude-sonnet-4-6",
                    project="natsec_jobs", script="score_jobs.py", label="cover")

Gemini setup (free tier): get a key at aistudio.google.com, then
`pip3 install google-generativeai`, and in .env set:
    AI_PROVIDER=gemini
    GEMINI_API_KEY=AIza...
    GEMINI_MODEL=gemini-2.5-flash       # optional; this is the default

NOTE: the Gemini path is written from the proven archived pattern but must be
validated on a first real run — Gemini formats structured output (JSON, custom
delimiters) slightly differently than Claude, which the callers parse.
"""
from __future__ import annotations

import os
import time

try:
    from usage_log import log_usage
except Exception:  # pragma: no cover
    def log_usage(*a, **k):
        pass

AI_PROVIDER = os.environ.get("AI_PROVIDER", "anthropic").strip().lower()
_DEFAULT_GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()

# Lazily-initialised, cached provider clients (so importing this module never
# requires a key for the provider you're NOT using).
_anthropic_client = None
_gemini_models: dict[str, object] = {}


def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _anthropic_client


def _get_gemini(model_name: str):
    if model_name not in _gemini_models:
        import google.generativeai as genai
        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
        _gemini_models[model_name] = genai.GenerativeModel(model_name)
    return _gemini_models[model_name]


def _log_gemini(resp, model: str, project: str, script: str, label: str) -> None:
    """Adapt Gemini's usage_metadata to the usage_log row shape."""
    um = getattr(resp, "usage_metadata", None)
    prompt_toks = int(getattr(um, "prompt_token_count", 0) or 0)
    out_toks    = int(getattr(um, "candidates_token_count", 0) or 0)
    cached_toks = int(getattr(um, "cached_content_token_count", 0) or 0)

    class _U:
        input_tokens = prompt_toks
        output_tokens = out_toks
        cache_creation_input_tokens = 0
        cache_read_input_tokens = cached_toks

    class _R:
        usage = _U()

    log_usage(_R(), project=project, script=script, model=model, label=label)


def complete(*, system: str, user: str, max_tokens: int, anthropic_model: str,
             gemini_model: str | None = None, project: str, script: str,
             label: str = "", max_retries: int = 6) -> str:
    """Run one completion on the active provider; log usage; return the text.

    `anthropic_model` is used when AI_PROVIDER=anthropic; `gemini_model`
    (defaulting to GEMINI_MODEL) is used when AI_PROVIDER=gemini.
    """
    gem_model = gemini_model or _DEFAULT_GEMINI_MODEL

    for attempt in range(max_retries):
        try:
            if AI_PROVIDER == "gemini":
                model = _get_gemini(gem_model)
                # Gemini takes a single prompt; prepend the system instruction.
                prompt = f"{system}\n\n{user}" if system else user
                resp = model.generate_content(
                    prompt, generation_config={"max_output_tokens": max_tokens}
                )
                _log_gemini(resp, gem_model, project, script, label)
                return resp.text

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
