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
import json
import logging
import re
import sys
import time
from pathlib import Path

import anthropic

from backends import call_backend
from dow import extract_from_dow_pdf
from search import search_office, _CUTOFF_YEAR
from utils import (
    INTER_REQUEST_DELAY,
    OrgEntry,
    PdfOfficeData,
    SearchResult,
    claude,
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
    """Use Claude vision to extract offices and their hierarchy from an org chart."""
    logger.info("[1/5] Parsing org chart: %s", image_path)
    img_data, media_type = encode_image(image_path)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_data}},
                {"type": "text", "text": "Parse this organizational chart. Return the JSON array of offices as instructed."},
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
# Stage 3 — Parse PDF (Functional Organization Manual)
# ---------------------------------------------------------------------------

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


def _extract_pdf_text(pdf_path: Path) -> str:
    """Extract all text from a PDF using pdfplumber; returns '' if not installed."""
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber not installed — run: pip install pdfplumber")
        return ""
    with pdfplumber.open(pdf_path) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def extract_from_pdf(
    client: anthropic.Anthropic,
    pdf_path: Path,
    office_names: list[str],
) -> dict[str, PdfOfficeData]:
    """Parse a Functional Organization Manual and extract office descriptions.

    Large PDFs are split into 60,000-character chunks to stay within Claude's
    context window. Results from all chunks are merged, with sub-programs
    de-duplicated by name.
    """
    logger.info("[4/5] Parsing FOM PDF: %s", pdf_path)
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
# Stage 5 — Merge all sources and build CSV rows
# ---------------------------------------------------------------------------

def build_rows(
    offices: list[OrgEntry],
    search_results: dict[str, SearchResult],
    dow_data: dict[str, list[dict]] | None = None,
    fom_data: dict[str, PdfOfficeData] | None = None,
) -> list[dict]:
    """Build one CSV row per leader found across all offices.

    Leadership sources are merged in priority order:
      1. DoW Directory 2026 (High confidence, parsed offline)
      2. Web search results (Gemini / Anthropic)
    Duplicate names (case-insensitive) from the lower-priority source are dropped.
    Offices with no leaders produce no rows.
    """
    rows: list[dict] = []
    dow = dow_data or {}
    fom = fom_data or {}

    for office in offices:
        name = office["name"]
        search = search_results.get(name, {})
        fom_office = fom.get(name, {})
        description = fom_office.get("description", "")
        acronym = search.get("acronym") or ""

        # DoW leaders first (High confidence, offline source)
        dow_leaders: list[dict] = list(dow.get(name, []))

        # Web-search leaders — skip any already found in DoW (dedup by lowercase name)
        raw_web = search.get("leadership")
        web_leaders: list[dict] = raw_web if isinstance(raw_web, list) else []
        seen_names = {l["name"].lower() for l in dow_leaders if isinstance(l, dict) and l.get("name")}
        extra = [l for l in web_leaders if isinstance(l, dict) and l.get("name")
                 and l["name"].lower() not in seen_names]

        for leader in dow_leaders + extra:
            if not isinstance(leader, dict) or not leader.get("name"):
                logger.debug("    Skipping malformed leader entry for '%s': %r", name, leader)
                continue
            rows.append({
                "org_name":    name,
                "person_name": leader.get("name") or "",
                "title":       leader.get("title") or "",
                "source":      leader.get("source") or "",
                "confidence":  leader.get("confidence") or "Medium",
                "notes":       leader.get("notes") or "",
                "description": description,
                "acronym":     acronym,
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
# Programmatic entry point — importable by other codebases
# ---------------------------------------------------------------------------

def run_pipeline(
    orgs: list[str] | None = None,
    image: Path | None = None,
    pdf: Path | None = None,
    dow_pdf: Path | None = None,
    output: Path | None = None,
    backend: str = "gemini",
    no_search: bool = False,
) -> list[dict]:
    """Run the full pipeline and return the CSV rows.

    This is the importable API surface. All CLI concerns live in ``main()``.
    Other codebases can call this directly::

        from pipeline import run_pipeline
        rows = run_pipeline(orgs=["Office of Naval Research", "NavalX"])

    Args:
        orgs:     List of org names (mutually exclusive with ``image``).
        image:    Path to an org chart image for Claude vision parsing.
        pdf:      Path to a Functional Organization Manual PDF.
        dow_pdf:  Path to the DoW Directory PDF.
        output:   Output CSV path. Defaults to ``outputs/org_output.csv``.
        backend:  AI backend — ``gemini`` (default) or ``anthropic``.
        no_search: Skip web search; use DoW PDF only.

    Returns:
        List of row dicts (same schema as the CSV columns).
    """
    if not orgs and not image:
        raise ValueError("Provide either orgs or image")
    if orgs and image:
        raise ValueError("orgs and image are mutually exclusive")

    output_path = output or Path("outputs/org_output.csv")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    needs_anthropic = image or pdf or backend == "anthropic"
    client = get_anthropic_client() if needs_anthropic else None

    # Stage 1
    if orgs:
        offices: list[OrgEntry] = [{"name": n, "code": None, "parent": None} for n in orgs]
        logger.info("[1/5] Using %d orgs from list", len(offices))
    else:
        if not image.exists():
            raise FileNotFoundError(f"Image not found: {image}")
        offices = parse_org_chart(client, image)

    if pdf and not pdf.exists():
        raise FileNotFoundError(f"PDF not found: {pdf}")

    # Stage 2
    dow_data: dict[str, list[dict]] = {}
    if dow_pdf:
        if not dow_pdf.exists():
            logger.warning(
                "[2/5] DoW PDF not found at %s — skipping "
                "(place the file at data/2026_DoW_Directory.pdf)", dow_pdf,
            )
        else:
            dow_data = extract_from_dow_pdf(dow_pdf, [o["name"] for o in offices])
            total_dow = sum(len(v) for v in dow_data.values())
            logger.info("[2/5] DoW PDF: %d leaders across %d orgs", total_dow, len(dow_data))

    # Stage 3
    search_results: dict[str, SearchResult] = {}
    if not no_search:
        checkpoint = _checkpoint_path(output_path)
        search_results = _load_checkpoint(checkpoint)
        if search_results:
            logger.info("[3/5] Resuming — %d/%d orgs already searched", len(search_results), len(offices))
        else:
            logger.info("[3/5] Web-searching %d offices...", len(offices))

        for i, office in enumerate(offices, 1):
            name = office["name"]
            if name in search_results:
                logger.info("    [%d/%d] Cached: %s", i, len(offices), name)
                continue
            search_results[name] = search_office(client, office, i, len(offices), backend)
            checkpoint.write_text(json.dumps(search_results, default=str))
            time.sleep(5 if backend == "gemini" else INTER_REQUEST_DELAY)

        checkpoint.unlink(missing_ok=True)
    else:
        logger.info("[3/5] Skipping web search (--no-search)")

    # Stage 4
    fom_data: dict[str, PdfOfficeData] = {}
    if pdf:
        fom_data = extract_from_pdf(client, pdf, [o["name"] for o in offices])
        n_programs = sum(len(v.get("programs", [])) for v in fom_data.values())
        logger.info("[4/5] FOM PDF: descriptions for %d offices, %d programs", len(fom_data), n_programs)
    else:
        logger.info("[4/5] No FOM PDF provided — skipping")

    # Stage 5
    logger.info("[5/5] Building CSV...")
    rows = build_rows(offices, search_results, dow_data=dow_data, fom_data=fom_data)
    write_csv(rows, output_path)
    logger.info("Done. Output: %s", output_path)
    return rows


# ---------------------------------------------------------------------------
# CLI entry point
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
    group.add_argument("--orgs", help="Comma-separated list of org names to search directly (no image needed)")
    parser.add_argument("--pdf", help="Functional Organization Manual PDF for org descriptions")
    parser.add_argument("--dow-pdf", dest="dow_pdf",
                        default="data/2026_DoW_Directory.pdf",
                        help="DoW Directory 2026 PDF (default: data/2026_DoW_Directory.pdf)")
    parser.add_argument("--output", default="outputs/org_output.csv",
                        help="Output CSV path (default: outputs/org_output.csv)")
    parser.add_argument("--backend", default="gemini",
                        choices=["gemini", "anthropic"],
                        help="AI backend (default: gemini — Google Search grounding)")
    parser.add_argument("--no-search", action="store_true",
                        help="Skip web search — use DoW PDF only, no API quota consumed")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("pdfminer").setLevel(logging.WARNING)

    load_env()

    try:
        run_pipeline(
            orgs=[n.strip() for n in args.orgs.split(",") if n.strip()] if args.orgs else None,
            image=Path(args.image) if args.image else None,
            pdf=Path(args.pdf) if args.pdf else None,
            dow_pdf=Path(args.dow_pdf) if args.dow_pdf else None,
            output=Path(args.output),
            backend=args.backend,
            no_search=args.no_search,
        )
    except (FileNotFoundError, ValueError) as exc:
        sys.exit(f"Error: {exc}")


if __name__ == "__main__":
    main()
