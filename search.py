"""
search.py — Web search stage for the org-pipeline.

Builds per-org research prompts and dispatches to the configured AI backend
(Gemini, Ollama, or Anthropic) to find current leadership for each office.
"""
from __future__ import annotations

import datetime
import json
import logging
import re

import anthropic

from backends import call_backend
from utils import OrgEntry, SearchResult, strip_json_fences

logger = logging.getLogger(__name__)

# Team decision (agreed 2026-06-29): sources older than _SOURCE_CUTOFF_YEARS are
# excluded from the output. Government leadership turns over frequently and stale
# data creates more CRM noise than it resolves.
_SOURCE_CUTOFF_YEARS: int = 3
_CUTOFF_YEAR: int = datetime.date.today().year - _SOURCE_CUTOFF_YEARS

# Patterns that identify a program office (PM, PdM, PEO, PMW, etc.).
# These orgs often lack a public leadership page, so the search strategy
# shifts to acquisition data and explicitly targets "program manager" roles.
_PROGRAM_OFFICE_RE = re.compile(
    r"""
    \b(?:PM|PdM|PMW|PMR|PMO|PEO)\b   # common program-office abbreviations
    | \bProgram\s+(?:Office|Manager|Management|Executive)\b
    | \bProgram\b.{0,20}\bOffice\b    # e.g. "Rapid Capabilities Program Office"
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Single system prompt reused across all orgs in a run.
# Embeds the cutoff year so every call reflects the team's recency policy.
_SEARCH_SYSTEM = f"""You are a government organization researcher specializing in DoD and federal agencies.

RECENCY POLICY (team decision): Only include leaders whose position is confirmed by a source
dated {_CUTOFF_YEAR} or later. Omit anyone you cannot verify within this window — do not
include them with a Low confidence flag, just leave them out entirely.

Return a single JSON object:
{{
  "code":    "official org code (e.g. 'PMW 130', 'Code 35', 'PdM ABC'), or null",
  "acronym": "known acronym, or null",
  "leadership": [
    {{
      "name":       "Full Name",
      "title":      "Exact title",
      "source":     "URL or brief source description",
      "confidence": "High|Medium|Low",
      "notes":      "Source date. Acting vs permanent. Who they replaced if known. Any caveats."
    }}
  ]
}}

LEADERSHIP ROLES TO CAPTURE (cast a wide net — include all of these if found):
  Program Executive Officer, Deputy Program Executive Officer, Assistant Program Executive Officer,
  Deputy for Acquisition and Systems Management,
  Program Manager, Deputy Program Manager, Assistant Program Manager,
  Executive Director, Technical Director,
  Commander, Deputy Commander, Commanding Officer,
  Director, Deputy Director, Associate Director, Principal Deputy,
  Chief of Staff, Chief of Operations, Chief of Engineering,
  Chief Information Officer, Chief Technology Officer,
  Senior Advisor, Senior Leader,
  and leads for communications, strategy, legislative affairs, public affairs, and finance.

CONFIDENCE LEVELS:
  High   — named on an official .mil or .gov leadership/bio/staff page within {_SOURCE_CUTOFF_YEARS} years
  Medium — named in a dvidshub.net article, SAM.gov award, congressional testimony, or
           .gov press release within {_SOURCE_CUTOFF_YEARS} years
  Low    — LinkedIn only (after passing all validation checks below); must note "LinkedIn only"

SEARCH STRATEGY — follow these steps IN ORDER. Move to the next step only when the
current step yields no confirmed current leadership.

STEP 1 — Official .mil / .gov leadership page (always attempt first)
  Query: [org name] site:.mil OR site:.gov
  Check URL paths: /leadership /about/leadership /team /organization /staff /bios /directory
  → If a current leader is found here, set confidence = "High" and stop searching.

STEP 2 — DoD press releases and official news (only if Step 1 yields nothing)
  Query dvidshub.net, defense.gov, and the parent command's .mil site for:
    "[org name]" (assumes command OR appointed OR "change of command" OR director OR commander)
  Accept only articles dated {_CUTOFF_YEAR} or later.
  → confidence = "High" for dvidshub.net / .mil; "Medium" for .gov press releases.

STEP 3 — Acquisition and government data sources (deeper dive when no leadership page exists)
  This step is especially important for program offices (PM / PdM / PEO / PMW):
  • SAM.gov: search "[org name]" — program managers and contracting officers are often
    named in recent contract awards and solicitations.
  • USASpending.gov: look for the named program manager on recent obligations.
  • Congressional testimony and GAO reports: search "[org name]" site:congress.gov OR site:gao.gov
  • Parent command directory or org chart if publicly available.
  → confidence = "Medium" for all Step 3 sources.

STEP 4 — LinkedIn (LAST RESORT — only when Steps 1–3 yield zero leadership data)
  Query: LinkedIn "[org name]" director OR commander OR "program manager" OR "executive director"
         OR "deputy director" OR "deputy commander" OR "deputy program manager"
         OR "assistant program manager" OR "program executive officer"
         OR "deputy program executive officer" OR "assistant program executive officer"
         OR "technical director" OR "chief of staff" OR "chief of operations"
         OR "chief of engineering" OR "chief information officer" OR "chief technology officer"
         OR "senior advisor" OR "senior leader" OR "associate director" OR "principal deputy"
         OR "deputy for acquisition"
  STRICT VALIDATION — exclude a person if ANY of the following is true:
    ✗ Current employer on their profile is not this org or its parent command
    ✗ The role has an end date (they have moved on)
    ✗ Profile says "retired", "former", or "previously" in the context of this role
    ✗ The role description does not name this specific org
    ✗ The profile was last active more than {_SOURCE_CUTOFF_YEARS} years ago
  For each LinkedIn leader that passes validation, add this exact note:
    "LinkedIn only — verify current assignment before outreach"
  → confidence = "Low" for all LinkedIn sources.

Return ONLY the JSON object. No markdown fences."""


def _is_program_office(org_name: str) -> bool:
    """Return True when the org name indicates a program office (PM/PdM/PEO/PMW…)."""
    return bool(_PROGRAM_OFFICE_RE.search(org_name))


def _build_search_prompt(name: str, parent: str, is_program: bool) -> str:
    """Construct the per-org research prompt."""
    base = (
        f'Research the government organization "{name}" (part of {parent}). '
        f'Return ALL current leaders confirmed by a source dated {_CUTOFF_YEAR} or later. '
        f'Include: program executive officers, deputy/assistant PEOs, program managers, '
        f'deputy/assistant program managers, executive directors, technical directors, '
        f'commanders, deputy commanders, directors, deputy directors, associate directors, '
        f'principal deputies, chiefs of staff, chiefs of operations, chiefs of engineering, '
        f'CIOs, CTOs, senior advisors, senior leaders, and functional leads for '
        f'communications, strategy, operations, legislative affairs, public affairs, and finance.'
    )
    if is_program:
        base += (
            f' This is a program office — in Step 3 specifically search for '
            f'"program manager" and "program executive officer" by name.'
        )
    return base


def search_office(
    client: anthropic.Anthropic | None,
    office: OrgEntry,
    index: int,
    total: int,
    backend: str = "auto",
    system_prompt: str | None = None,
) -> SearchResult:
    """Search for leadership for a single office.

    Builds a tailored prompt (detecting program offices automatically), calls the
    configured backend, and returns a normalized SearchResult. On any failure,
    logs a warning and returns an empty result rather than crashing the run.

    Args:
        system_prompt: Override the default ``_SEARCH_SYSTEM`` prompt. Useful
                       when pulling this into a larger codebase that supplies
                       its own research instructions.
    """
    name = office["name"]
    parent = office.get("parent") or "unknown parent organization"
    is_program = _is_program_office(name)
    mode_tag = " [program-office mode]" if is_program else ""
    logger.info("    [%d/%d] %s%s", index, total, name, mode_tag)

    prompt = _build_search_prompt(name, parent, is_program)
    active_system = system_prompt if system_prompt is not None else _SEARCH_SYSTEM
    try:
        raw = call_backend(client, prompt, active_system, backend, max_search_uses=10)
        match = re.search(r"\{.*\}", strip_json_fences(raw), re.DOTALL)
        if match:
            data = json.loads(match.group())
            leaders = data.get("leadership") or []
            if isinstance(leaders, str):
                leaders = []
            logger.info("        └─ %d leader(s) found (sources: %s–present)", len(leaders), _CUTOFF_YEAR)
            return {
                "code":       data.get("code"),
                "acronym":    data.get("acronym"),
                "leadership": leaders,
            }
    except Exception as exc:
        logger.warning("    Search failed for '%s': %s", name, exc)
        logger.debug("    Full traceback for '%s':", name, exc_info=True)

    logger.info("        └─ 0 leader(s) found")
    return {"code": None, "acronym": None, "leadership": []}
