#!/usr/bin/env python3
"""
Org Chart → CSV Pipeline

Converts a list of org names (or an org chart image) into a structured CSV
ready for CRM import, with leadership, contact info, and descriptions sourced
from official government websites.

Pipeline stages:
  1. Parse an org chart image with Claude vision → offices + parent hierarchy
     (or accept a comma-separated list of names via --orgs)
  2. DoW Directory 2026 PDF — offline first pass, High-confidence leaders, no API quota
  3. Web search (Gemini) — supplements DoW data using a 4-tier source strategy
  4. Functional Organization Manual PDF — optional richer org descriptions
  5. Merge all sources and export a CRM-ready CSV

Usage:
  python pipeline.py --orgs "Office of Naval Research, NavalX"
  python pipeline.py --image chart.png
  python pipeline.py --image chart.jpg --pdf fom.pdf --output outputs/orgs.csv
  python pipeline.py --orgs "NAVSEA" --backend gemini --verbose
  python pipeline.py --orgs "Army Material Command" --dow-pdf data/2026_DoW_Directory.pdf
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import anthropic

from utils import (
    INTER_REQUEST_DELAY,
    OrgEntry,
    PdfOfficeData,
    SearchResult,
    claude,
    claude_with_search,
    encode_image,
    get_anthropic_client,
    load_env,
    strip_json_fences,
    write_csv,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stage 1 — Parse org chart image
# ---------------------------------------------------------------------------

_PARSE_SYSTEM = """You are an expert at reading organizational chart images.
Extract ONLY offices, departments, and programs — not individual roles or positions.
Skip any box that represents a named role (e.g. "Principal Deputy Under Secretary", "Chief of Staff").
Keep boxes that are clearly offices, services, centers, or programs.
Return a JSON array. Each element:
{
  "name": "Full office name without the org code",
  "code": "Org code if shown (e.g. VBA-20F), else null",
  "parent": "Name of the direct parent office, or null if top-level"
}
Return ONLY the JSON array, no other text."""


def parse_org_chart(client: anthropic.Anthropic, image_path: Path) -> list[OrgEntry]:
    """Use Claude vision to extract offices and their hierarchy from an org chart.

    Args:
        client:     Authenticated Anthropic client.
        image_path: Path to the org chart image (PNG, JPG, or WebP).

    Returns:
        List of :class:`OrgEntry` dicts with keys: ``name``, ``code``, ``parent``.

    Raises:
        json.JSONDecodeError: If Claude's response cannot be parsed as JSON.
    """
    logger.info("[1/5] Parsing org chart: %s", image_path)
    img_data, media_type = encode_image(image_path)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_data}},
                {"type": "text",  "text": "Parse this organizational chart. Return the JSON array of offices as instructed."},
            ],
        }
    ]
    raw = claude(client, messages, system=_PARSE_SYSTEM, max_tokens=4096)
    parsed = json.loads(strip_json_fences(raw))
    if not isinstance(parsed, list):
        raise ValueError(f"Expected JSON array from org chart parse, got {type(parsed).__name__}")
    offices: list[OrgEntry] = parsed
    logger.info("    Found %d offices/units", len(offices))
    return offices


# ---------------------------------------------------------------------------
# Stage 2 — Web search for each office
# ---------------------------------------------------------------------------

# Team decision (agreed 2026-06-29): sources older than _SOURCE_CUTOFF_YEARS are
# excluded from the output.  Government leadership turns over frequently and stale
# data creates more CRM noise than it resolves.  Any entry we cannot confirm within
# this window is simply omitted rather than included with a low-confidence flag.
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


def _is_program_office(org_name: str) -> bool:
    """Return True when the org name indicates a program office (PM/PdM/PEO/PMW…)."""
    return bool(_PROGRAM_OFFICE_RE.search(org_name))


def _build_search_prompt(name: str, parent: str, is_program: bool) -> str:
    """Construct the per-org research prompt injected below the system instructions."""
    base = (
        f'Research the government organization "{name}" (part of {parent}). '
        f'Return ALL current leaders confirmed by a source dated {_CUTOFF_YEAR} or later.'
    )
    if is_program:
        base += (
            f' This is a program office — in Step 3 specifically search for '
            f'"program manager" and "program executive officer" by name.'
        )
    return base


# Single system prompt reused across all orgs in a run.
# Embeds the cutoff year so every call reflects the team's recency policy.
_COMBINED_SYSTEM = f"""You are a government organization researcher specializing in DoD and federal agencies.

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


def _call_gemini(prompt: str, system: str) -> str:
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


def _call_ollama(prompt: str, system: str) -> str:
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


def _call_backend(
    client: anthropic.Anthropic | None,
    prompt: str,
    system: str,
    backend: str,
    max_search_uses: int = 5,
) -> str:
    """Dispatch to the selected backend. Default order: Gemini → Ollama."""
    if backend in ("gemini", "auto"):
        try:
            return _call_gemini(prompt, system)
        except Exception as exc:
            if backend == "gemini":
                raise
            logger.warning("Gemini unavailable (%s) — falling back to Ollama", exc)
            return _call_ollama(prompt, system)

    if backend == "ollama":
        return _call_ollama(prompt, system)

    # anthropic — explicit only
    if client is None:
        raise RuntimeError("Anthropic backend requires a valid API key in .env")
    return claude_with_search(client, prompt, system=system, max_search_uses=max_search_uses)


def search_office(
    client: anthropic.Anthropic | None,
    office: OrgEntry,
    index: int,
    total: int,
    backend: str = "auto",
) -> SearchResult:
    """Search for leadership and org metadata for a single office.

    Builds a tailored prompt (detecting program offices automatically), calls the
    configured backend, and returns a normalized SearchResult.  On parse failure
    the function logs a warning and returns an empty result rather than crashing
    the whole run.
    """
    name      = office["name"]
    parent    = office.get("parent") or "unknown parent organization"
    is_program = _is_program_office(name)
    mode_tag  = " [program-office mode]" if is_program else ""
    logger.info("    [%d/%d] %s%s", index, total, name, mode_tag)

    prompt = _build_search_prompt(name, parent, is_program)
    try:
        raw = _call_backend(client, prompt, _COMBINED_SYSTEM, backend, max_search_uses=10)
        match = re.search(r"\{.*\}", strip_json_fences(raw), re.DOTALL)
        if match:
            data    = json.loads(match.group())
            leaders = data.get("leadership") or []
            if isinstance(leaders, str):
                leaders = []
            logger.info("        └─ %d leader(s) found (sources: %s–present)",
                        len(leaders), _CUTOFF_YEAR)
            return {
                "code":        data.get("code"),
                "website":     data.get("website") or "",
                "phone":       data.get("phone"),
                "email":       data.get("email"),
                "acronym":     data.get("acronym"),
                "description": data.get("description"),
                "leadership":  leaders,
            }
    except Exception as exc:
        logger.warning("    Search failed for '%s': %s", name, exc)

    logger.info("        └─ 0 leader(s) found")
    return {"code": None, "website": "", "phone": None, "email": None,
            "acronym": None, "description": None, "leadership": []}


# ---------------------------------------------------------------------------
# DoW Directory 2026 — primary leadership source (pre-web-search)
# ---------------------------------------------------------------------------

_DOW_SOURCE = "DoW Directory 2026"

# Requires at least one space on each side of the dash so "G-3" doesn't split.
_STAFF_RE = re.compile(r'^(.+?)\s+[-–]\s+(.+)$')
_FOOTER_RE = re.compile(r'^Page \d+ of \d+')

_SKIP_FIRST_WORDS = frozenset(
    {"vacant", "tbd", "n/a", "see", "for", "and", "the", "note", "source", "website"}
)
# Words that signal an org name, not a person — if present anywhere in the pre-hyphen text
_ORG_KEYWORDS = frozenset({
    "office", "offices", "command", "department", "division", "center", "program",
    "programs", "directorate", "corps", "branch", "section", "group", "unit",
    "management", "operations", "systems", "services", "agency", "committee",
    "staff", "headquarters", "laboratory", "labs", "code", "codes",
})
_TOC_LINE_RE = re.compile(r'[.\s]{3,}\d+\s*$')  # e.g. "Office of X.........99"


def _looks_like_name(text: str) -> bool:
    """Heuristic: does the pre-hyphen portion of a line look like a person's name?"""
    words = text.strip().split()
    if len(words) < 2 or len(words) > 8:
        return False
    if words[0].lower() in _SKIP_FIRST_WORDS:
        return False
    if "/" in text or "http" in text.lower():
        return False
    # All-caps block → section header, not a name
    if text.upper() == text and len(text) > 8:
        return False
    # If org-indicator keywords appear → org name, not a person
    lower_words = {w.lower().rstrip(".,;") for w in words}
    if lower_words & _ORG_KEYWORDS:
        return False
    # "Word Number" pattern → org code, not a name (e.g. "Code 31", "Bldg 4")
    if len(words) >= 2 and re.match(r'^\d+[A-Za-z]?$', words[1]):
        return False
    # Numbered list items (e.g. "1) PAE Maritime", "2. Some Office") → not a name
    if re.match(r'^\d+[.)]\s', text):
        return False
    return text[0].isupper()


def _staff_density(lines: list[str], start: int, window: int = 30) -> int:
    """Count how many 'Name - Title' lines appear in the next `window` lines."""
    count = 0
    for raw in lines[start + 1: start + 1 + window]:
        if _STAFF_RE.match(raw.strip()):
            count += 1
    return count


def _find_section_start(lines: list[str], org_name: str) -> int | None:
    """Return the index of the line that best matches org_name as a section header.

    Prefers candidates that have actual 'Name - Title' staff entries immediately
    after them (i.e., the real section header, not a TOC entry or passing mention).
    """
    query = org_name.lower().strip()
    candidates: list[tuple[float, int]] = []  # (text_score, line_index)

    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        line_lower = line.lower()

        text_score = 0.0

        if query in line_lower:
            if _TOC_LINE_RE.search(line):
                continue
            coverage = len(query) / max(len(line_lower), 1)
            text_score = 0.70 + 0.30 * coverage
        else:
            q_words = set(query.split())
            l_words = set(line_lower.split())
            if len(q_words) >= 2:
                overlap = len(q_words & l_words) / len(q_words)
                if overlap >= 0.75:
                    text_score = overlap

        if text_score >= 0.70:
            candidates.append((text_score, i))

    if not candidates:
        return None

    # Among candidates with good text match, prefer the one with the most
    # staff lines immediately following (real section header, not a mention/intro).
    best_idx = None
    best_combined = -1.0
    for text_score, idx in candidates:
        density = _staff_density(lines, idx)
        combined = text_score + 0.5 * density  # density heavily breaks ties
        if combined > best_combined:
            best_combined = combined
            best_idx = idx

    return best_idx


def extract_from_dow_pdf(
    pdf_path: Path,
    org_names: list[str],
) -> dict[str, list[dict]]:
    """Extract leadership from the DoW Directory 2026 PDF.

    For each org name, finds the best-matching section header and parses
    'Name - Title' lines below it.  Returns a dict keyed by org name;
    values are lists of leader dicts ready for build_rows (confidence=High).
    """
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber not installed — run: pip install pdfplumber")
        return {}

    logger.info("[DoW] Loading %s...", pdf_path.name)
    all_lines: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        for idx, page in enumerate(pdf.pages):
            if idx % 80 == 0:
                logger.info("[DoW]   pages %d-%d / %d", idx + 1, min(idx + 80, total), total)
            text = page.extract_text() or ""
            all_lines.extend(text.splitlines())

    logger.info("[DoW] %d lines extracted — searching for %d orgs", len(all_lines), len(org_names))

    # Build a lowercase lookup of target org names for stop-detection
    org_set_lower = {n.lower().strip() for n in org_names}

    results: dict[str, list[dict]] = {}

    for org_name in org_names:
        header_idx = _find_section_start(all_lines, org_name)
        if header_idx is None:
            logger.debug("[DoW]   not found: %s", org_name)
            continue

        leaders: list[dict] = []

        for raw in all_lines[header_idx + 1: header_idx + 250]:
            stripped = raw.strip()

            # Skip page footers
            if _FOOTER_RE.match(stripped):
                continue

            if not stripped:
                continue

            # Stop if we hit another target org's section header
            if stripped.lower() in org_set_lower and stripped.lower() != org_name.lower():
                break

            m = _STAFF_RE.match(stripped)
            if not m:
                continue

            name_part = m.group(1).strip()
            title_part = m.group(2).strip()

            if not _looks_like_name(name_part):
                continue

            notes = ""
            title_lower = title_part.lower()
            if "acting" in title_lower or "(a)" in title_lower:
                notes = "Acting title as of DoW Directory 2026 — confirm if made permanent"

            leaders.append({
                "name":       name_part,
                "title":      title_part,
                "source":     _DOW_SOURCE,
                "confidence": "High",
                "notes":      notes,
            })

        if leaders:
            logger.info("[DoW]   %-40s → %d leader(s)", org_name, len(leaders))
            results[org_name] = leaders
        else:
            logger.debug("[DoW]   header found but no leaders parsed: %s", org_name)

    return results


# ---------------------------------------------------------------------------
# Stage 3 — Parse PDF (Functional Organization Manual)
# ---------------------------------------------------------------------------

def _extract_pdf_text(pdf_path: Path) -> str:
    """Extract all text from a PDF using pdfplumber; returns '' if not installed."""
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber not installed — run: pip install pdfplumber")
        return ""
    with pdfplumber.open(pdf_path) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


_OFFICE_EXTRACT_SYSTEM = """You are parsing a government Functional Organization Manual (FOM).
Given the PDF text and a list of office names, for each office extract:
- Its official overview or mission statement (first 2-3 sentences)
- Named programs or sub-programs explicitly listed under an Activities section

Return JSON:
{
  "offices": {
    "Office Name": {
      "description": "...",
      "programs": [
        {"name": "Program Name", "acronym": "ACR or null", "description": "1 sentence from the FOM"}
      ]
    }
  }
}
Return ONLY the JSON."""


def extract_from_pdf(
    client: anthropic.Anthropic,
    pdf_path: Path,
    office_names: list[str],
) -> dict[str, PdfOfficeData]:
    """Parse a Functional Organization Manual and extract office data.

    Large PDFs are split into 60,000-character chunks to stay within Claude's
    context window (roughly 10–15 pages of dense government prose per chunk).
    Results from all chunks are merged, with sub-programs de-duplicated by name.

    Args:
        client:       Authenticated Anthropic client.
        pdf_path:     Path to the FOM PDF.
        office_names: Office names to search for within the document.

    Returns:
        Dict keyed by office name, each value a :class:`PdfOfficeData` with
        ``description`` and ``programs`` fields.
    """
    logger.info("[3/4] Parsing PDF: %s", pdf_path)
    full_text = _extract_pdf_text(pdf_path)
    if not full_text:
        return {}

    offices_json = json.dumps(office_names)
    chunk_size = 60_000
    chunks = [full_text[i:i + chunk_size] for i in range(0, len(full_text), chunk_size)]
    results: dict[str, PdfOfficeData] = {}

    for i, chunk in enumerate(chunks, 1):
        logger.info("    Processing chunk %d/%d", i, len(chunks))
        prompt = (
            f"Office names to find: {offices_json}\n\nPDF text chunk:\n{chunk}\n\n"
            "Extract descriptions and programs for any listed offices found in this chunk."
        )
        try:
            raw = claude(
                client,
                [{"role": "user", "content": prompt}],
                system=_OFFICE_EXTRACT_SYSTEM,
                max_tokens=8192,
            )
            match = re.search(r"\{.*\}", strip_json_fences(raw), re.DOTALL)
            if not match:
                continue
            for name, data in json.loads(match.group()).get("offices", {}).items():
                if name not in results:
                    results[name] = data
                else:
                    # Merge programs discovered in later chunks, avoiding duplicates.
                    seen = {p["name"] for p in results[name].get("programs", [])}
                    for prog in data.get("programs", []):
                        if prog["name"] not in seen:
                            results[name].setdefault("programs", []).append(prog)
        except (anthropic.APIError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("    Chunk %d failed: %s", i, exc)

    return results


# ---------------------------------------------------------------------------
# Stage 4 — Merge sources and build CSV rows
# ---------------------------------------------------------------------------

def build_rows(
    offices: list[OrgEntry],
    search_results: dict[str, SearchResult],
    dow_data: dict[str, list[dict]] | None = None,
    fom_data: dict[str, PdfOfficeData] | None = None,
) -> list[dict]:
    """Build one CSV row per leader found across all offices.

    Each row represents a single person linked to their organization.
    Offices with no leaders produce no rows.

    Leadership sources are merged in priority order:
      1. DoW Directory 2026 (High confidence, parsed offline)
      2. Web search results (Gemini / Anthropic)
    Duplicate names (case-insensitive) from the lower-priority source are dropped.

    Args:
        offices:        Output of :func:`parse_org_chart`.
        search_results: Output of :func:`search_office`, keyed by office name.
        dow_data:       Output of :func:`extract_from_dow_pdf`, keyed by office name.

    Returns:
        Flat list of row dicts ready for :func:`~utils.write_csv`.
    """
    rows: list[dict] = []
    dow = dow_data or {}
    fom = fom_data or {}

    for office in offices:
        name = office["name"]
        search = search_results.get(name, {})
        fom_office = fom.get(name, {})
        description = fom_office.get("description", "")

        # DoW leaders first (High confidence, offline source)
        dow_leaders: list[dict] = list(dow.get(name, []))

        # Web-search leaders — skip any already found in DoW (dedup by lowercase name)
        web_leaders: list[dict] = search.get("leadership") or []
        if isinstance(web_leaders, str):
            web_leaders = []
        seen_names = {l["name"].lower() for l in dow_leaders}
        extra = [l for l in web_leaders if l["name"].lower() not in seen_names]

        for leader in dow_leaders + extra:
            rows.append({
                "org_name":    name,
                "person_name": leader.get("name") or "",
                "title":       leader.get("title") or "",
                "source":      leader.get("source") or "",
                "confidence":  leader.get("confidence") or "Medium",
                "notes":       leader.get("notes") or "",
                "description": description,
            })

    return rows


# ---------------------------------------------------------------------------
# Checkpoint helpers — survive mid-run crashes on large org lists
# ---------------------------------------------------------------------------

def _checkpoint_path(output_path: Path) -> Path:
    return output_path.parent / f".{output_path.stem}.checkpoint.json"

def _load_checkpoint(path: Path) -> dict[str, SearchResult]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Parse arguments, run all pipeline stages, and write the output CSV."""
    parser = argparse.ArgumentParser(
        description="Convert an org chart image (+ optional PDF) into a CRM-ready CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python pipeline.py --image aetc_orgchart.png\n"
            "  python pipeline.py --image va_chart.jpg --pdf va_fom.pdf --output va_orgs.csv\n"
            "  python pipeline.py --image chart.png --no-search --output quick.csv\n"
            "  python pipeline.py --image chart.png --verbose"
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--image", help="Org chart image file (PNG / JPG / WebP)")
    group.add_argument("--orgs",  help="Comma-separated list of org names to search directly (no image needed)")
    parser.add_argument("--pdf",      help="Functional Organization Manual PDF (optional, image mode only)")
    parser.add_argument("--dow-pdf",  dest="dow_pdf",
                        default="data/2026_DoW_Directory.pdf",
                        help="DoW Directory 2026 PDF (default: data/2026_DoW_Directory.pdf)")
    parser.add_argument("--output",   default="outputs/org_output.csv", help="Output CSV path (default: outputs/org_output.csv)")
    parser.add_argument("--backend",   default="auto",
                        choices=["auto", "gemini", "ollama", "anthropic"],
                        help="AI backend (default: auto — Gemini with Google Search grounding → Ollama fallback)")
    parser.add_argument("--no-search", action="store_true", help="Skip web search — faster but no contact details")
    parser.add_argument("--verbose",   action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("pdfminer").setLevel(logging.WARNING)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    load_env()

    # Anthropic client needed for image parsing, PDF extraction, or when explicitly selected.
    needs_anthropic = args.image or args.pdf or args.backend == "anthropic"
    client = get_anthropic_client() if needs_anthropic else None

    if args.orgs:
        names = [n.strip() for n in args.orgs.split(",") if n.strip()]
        offices: list[OrgEntry] = [{"name": n, "code": None, "parent": None} for n in names]
        logger.info("[1/5] Using %d orgs from --orgs", len(offices))
    else:
        image_path = Path(args.image)
        if not image_path.exists():
            sys.exit(f"Error: image file not found: {image_path}")
        offices = parse_org_chart(client, image_path)

    pdf_path = Path(args.pdf) if args.pdf else None
    if pdf_path and not pdf_path.exists():
        sys.exit(f"Error: PDF file not found: {pdf_path}")

    # Stage 2 — DoW Directory offline scan (no quota, always runs first)
    dow_data: dict[str, list[dict]] = {}
    if args.dow_pdf:
        dow_pdf_path = Path(args.dow_pdf)
        if not dow_pdf_path.exists():
            logger.warning(
                "[2/4] DoW PDF not found at %s — skipping "
                "(place the file at data/2026_DoW_Directory.pdf)",
                dow_pdf_path,
            )
        else:
            dow_data = extract_from_dow_pdf(dow_pdf_path, [o["name"] for o in offices])
            total_dow = sum(len(v) for v in dow_data.values())
            logger.info("[2/4] DoW PDF: %d leaders across %d orgs", total_dow, len(dow_data))

    # Stage 3 — Web search supplements what the DoW PDF didn't cover
    search_results: dict[str, SearchResult] = {}
    if not args.no_search:
        checkpoint = _checkpoint_path(output_path)
        search_results = _load_checkpoint(checkpoint)
        if search_results:
            logger.info("[3/4] Resuming — %d/%d orgs already searched", len(search_results), len(offices))
        else:
            logger.info("[3/4] Web-searching %d offices...", len(offices))

        for i, office in enumerate(offices, 1):
            name = office["name"]
            if name in search_results:
                logger.info("    [%d/%d] Cached: %s", i, len(offices), name)
                continue
            search_results[name] = search_office(client, office, i, len(offices), args.backend)
            checkpoint.write_text(json.dumps(search_results, default=str))
            time.sleep(5 if args.backend in ("gemini", "auto") else INTER_REQUEST_DELAY)

        checkpoint.unlink(missing_ok=True)
    else:
        logger.info("[3/4] Skipping web search (--no-search)")

    # Stage 4 — FOM PDF (optional, image mode only)
    fom_data: dict[str, PdfOfficeData] = {}
    if pdf_path:
        fom_data = extract_from_pdf(client, pdf_path, [o["name"] for o in offices])
        n_programs = sum(len(v.get("programs", [])) for v in fom_data.values())
        logger.info("[4/5] FOM PDF: descriptions for %d offices, %d programs", len(fom_data), n_programs)
    else:
        logger.info("[4/5] No FOM PDF provided — skipping")

    logger.info("[5/5] Building CSV...")
    rows = build_rows(offices, search_results, dow_data=dow_data, fom_data=fom_data)
    write_csv(rows, output_path)
    logger.info("Done. Output: %s", output_path)


if __name__ == "__main__":
    main()
