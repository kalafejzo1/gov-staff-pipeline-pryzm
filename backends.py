"""
backends.py — AI backend dispatch for the org-pipeline.

Provides a Backend protocol and a call_backend() entry point that routes to
Gemini (with Google Search grounding), Ollama (local, no web access), or
Anthropic (with built-in web search tool).

To add a new backend:
  1. Write a class that implements the Backend protocol (a ``search`` method).
  2. Register it in ``call_backend``'s dispatch block.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Protocol, runtime_checkable

import anthropic

from utils import claude_with_search

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backend protocol — implement this to add a new AI backend
# ---------------------------------------------------------------------------

@runtime_checkable
class Backend(Protocol):
    """A callable AI backend that performs a single prompt/system call."""

    def search(self, prompt: str, system: str, **kwargs) -> str:
        """Run the prompt and return the raw text response."""
        ...


# ---------------------------------------------------------------------------
# Concrete backends
# ---------------------------------------------------------------------------

def call_gemini(prompt: str, system: str) -> str:
    """Call Gemini 2.5 Flash with Google Search grounding enabled."""
    from google import genai as google_genai
    from google.genai import types as genai_types

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    gc = google_genai.Client(api_key=api_key)
    full_prompt = f"{system}\n\n{prompt}" if system else prompt

    for attempt in range(1, 4):
        try:
            response = gc.models.generate_content(
                model="gemini-2.5-flash",
                contents=full_prompt,
                config=genai_types.GenerateContentConfig(
                    tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())]
                ),
            )
            return response.text
        except Exception as exc:
            exc_str = str(exc)
            is_rate_limit = "ResourceExhausted" in type(exc).__name__ or "429" in exc_str
            if not is_rate_limit:
                raise
            if "PerDay" in exc_str:
                raise RuntimeError("Gemini daily quota exhausted — resets at midnight Pacific") from exc
            if attempt == 3:
                raise
            wait = 30 * attempt
            logger.warning("Gemini rate limit — waiting %ds before retry (%d/3)...", wait, attempt)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def call_ollama(prompt: str, system: str) -> str:
    """Call a local Ollama instance. No web access — training knowledge only."""
    import urllib.request
    import urllib.error

    payload = json.dumps({
        "model": "llama3.1",
        "prompt": f"{system}\n\n{prompt}" if system else prompt,
        "stream": False,
        "options": {"num_predict": 4000},
    }).encode()
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read()).get("response", "")
    except urllib.error.URLError:
        raise RuntimeError("Ollama is not running. Start it with: ollama serve")


def call_backend(
    client: anthropic.Anthropic | None,
    prompt: str,
    system: str,
    backend: str,
    max_search_uses: int = 5,
) -> str:
    """Dispatch to the selected backend.

    Backend priority for 'auto': Gemini (with Google Search) → Ollama (no web access).
    Ollama fallback is flagged as potentially stale since it has no live search.
    """
    if backend in ("gemini", "auto"):
        try:
            return call_gemini(prompt, system)
        except Exception as exc:
            if backend == "gemini":
                raise
            logger.warning(
                "Gemini unavailable (%s) — falling back to Ollama "
                "(no web search; results may be stale)", exc
            )
            return call_ollama(prompt, system)

    if backend == "ollama":
        return call_ollama(prompt, system)

    # anthropic — explicit only
    if client is None:
        raise RuntimeError("Anthropic backend requires a valid API key in .env")
    return claude_with_search(client, prompt, system=system, max_search_uses=max_search_uses)
