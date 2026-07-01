"""
dow.py — DoW Directory PDF parser for the org-pipeline.

Extracts leadership entries from the DoW Directory 2026 PDF using heuristic
text matching. For each requested org name, finds the best-matching section
header and parses 'Name - Title' lines below it.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_DOW_SOURCE = "DoW Directory 2026"

# Matches "Name - Title" lines. Requires at least one space either side of the
# dash so hyphenated org codes like "G-3" don't split incorrectly.
_STAFF_RE = re.compile(r'^(.+?)\s+[-–]\s+(.+)$')
_FOOTER_RE = re.compile(r'^Page \d+ of \d+')
_TOC_LINE_RE = re.compile(r'[.\s]{3,}\d+\s*$')  # e.g. "Office of X.........99"

# First words that reliably indicate the line is not a person's name.
_SKIP_FIRST_WORDS = frozenset(
    {"vacant", "tbd", "n/a", "see", "for", "and", "the", "note", "source", "website"}
)

# Words anywhere in the pre-hyphen text that indicate an org name, not a person.
_ORG_KEYWORDS = frozenset({
    "office", "offices", "command", "department", "division", "center", "program",
    "programs", "directorate", "corps", "branch", "section", "group", "unit",
    "management", "operations", "systems", "services", "agency", "committee",
    "staff", "headquarters", "laboratory", "labs", "code", "codes",
})


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
    'Name - Title' lines below it. Returns a dict keyed by org name;
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
            # Stop if we hit any new section header — all-caps line with no dash
            # (catches sections not in the input list, preventing bleed-over)
            if (stripped == stripped.upper() and len(stripped) > 6
                    and "-" not in stripped and stripped.lower() != org_name.lower()):
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
