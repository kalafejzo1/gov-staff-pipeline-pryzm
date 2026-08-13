"""
backends.py — AI backend dispatch for the org-pipeline.

Provides a Backend protocol and a call_backend() entry point that routes to
Gemini (with Google Search grounding) or Anthropic (with built-in web
search tool).

To add a new backend:
  1. Write a class that implements the Backend protocol (a ``search`` method).
  2. Register it in ``call_backend``'s dispatch block.
"""
from __future__ import annotations

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
            if response.text is None:
                raise RuntimeError(
                    f"Gemini returned no text content "
                    f"(finish_reason={getattr(response, 'prompt_feedback', 'unknown')})"
                )
            return response.text
        except Exception as exc:
            exc_str = str(exc)
            is_rate_limit = "ResourceExhausted" in type(exc).__name__ or "429" in exc_str
            # "No text content" is a transient generation glitch (empty/blocked
            # response), not a real failure — observed in production to succeed
            # immediately on retry, same as the malformed-JSON case in search.py.
            is_empty_response = "no text content" in exc_str.lower()
            # "503 UNAVAILABLE — high demand" is Google's model being temporarily
            # overloaded, not our request being wrong — also observed in
            # production, also transient. ServerError covers other 5xx too.
            is_server_overloaded = (
                "ServerError" in type(exc).__name__
                or "503" in exc_str or "UNAVAILABLE" in exc_str or "high demand" in exc_str.lower()
            )
            if not (is_rate_limit or is_empty_response or is_server_overloaded):
                raise
            if is_rate_limit and "PerDay" in exc_str:
                raise RuntimeError("Gemini daily quota exhausted — resets at midnight Pacific") from exc
            if attempt == 3:
                raise
            if is_rate_limit or is_server_overloaded:
                wait = 30 * attempt
                reason = "rate limit" if is_rate_limit else "temporarily overloaded"
            else:
                wait = 2 * attempt
                reason = "returned an empty response"
            logger.warning("Gemini %s — waiting %ds before retry (%d/3)...", reason, wait, attempt)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def call_backend(
    client: anthropic.Anthropic | None,
    prompt: str,
    system: str,
    backend: str,
    max_search_uses: int = 5,
) -> str:
    """Dispatch to the selected backend: 'gemini' or 'anthropic'."""
    if backend == "gemini":
        return call_gemini(prompt, system)

    # anthropic — explicit only
    if client is None:
        raise RuntimeError("Anthropic backend requires a valid API key in .env")
    return claude_with_search(client, prompt, system=system, max_search_uses=max_search_uses)
