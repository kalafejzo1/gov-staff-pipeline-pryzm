"""
fetch_dow.py — Fetches the latest DoW Directory PDF from siliconvalleydefense.org.

The directory itself isn't published at a stable URL: the site links out to a
Google Sites page, which links out to a Google Drive file that gets replaced
in place whenever a new revision is published. This module follows that
chain and downloads whatever the current file is.

Because it depends on the HTML structure of two third-party pages, any step
can break if either site is redesigned — every step raises a specific,
readable RuntimeError rather than failing silently, so a broken chain is
obvious instead of quietly falling back to stale or empty data.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_DIRECTORY_PAGE = "https://www.siliconvalleydefense.org/dow-directory"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; gov-staff-pipeline-pryzm)"}
_TIMEOUT = 30


def _find_sites_url(html: str) -> str:
    """Find the Google Sites link the directory page points to."""
    match = re.search(r'href="(https://sites\.google\.com/[^"]+)"', html)
    if not match:
        raise RuntimeError(
            f"Couldn't find a sites.google.com link on {_DIRECTORY_PAGE} — "
            "the page layout may have changed."
        )
    return match.group(1)


def _find_drive_file(html: str) -> tuple[str, str]:
    """Find the Drive file ID and filename behind the "Download the Directory" button."""
    match = re.search(
        r'href="https://drive\.google\.com/file/d/([A-Za-z0-9_-]+)/[^"]*"'
        r'[^>]*aria-label="Download the Directory"',
        html,
    )
    if not match:
        raise RuntimeError(
            "Couldn't find the 'Download the Directory' button on the Google Sites "
            "page — it may have been redesigned."
        )
    file_id = match.group(1)

    label_match = re.search(r'aria-label="Drive, ([^"]+)"', html)
    filename = label_match.group(1) if label_match else f"{file_id}.pdf"
    return file_id, filename


def fetch_latest_dow_pdf(dest: Path) -> str:
    """Download the current DoW Directory PDF to ``dest``.

    Follows siliconvalleydefense.org -> Google Sites -> Google Drive, then
    downloads and overwrites ``dest``.

    Returns:
        The filename Google Drive reports for the file (e.g.
        ``"2026 DoW Directory August 2026 Update 8.pdf"``), useful for
        showing which revision was fetched.

    Raises:
        RuntimeError: If any step of the chain fails or the download isn't
                      a valid PDF.
    """
    logger.info("Fetching directory page: %s", _DIRECTORY_PAGE)
    page = requests.get(_DIRECTORY_PAGE, headers=_HEADERS, timeout=_TIMEOUT)
    page.raise_for_status()
    sites_url = _find_sites_url(page.text)

    logger.info("Fetching Google Sites page: %s", sites_url)
    sites_page = requests.get(sites_url, headers=_HEADERS, timeout=_TIMEOUT)
    sites_page.raise_for_status()
    file_id, filename = _find_drive_file(sites_page.text)

    download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    logger.info("Downloading %s", filename)
    response = requests.get(download_url, headers=_HEADERS, timeout=60)
    response.raise_for_status()

    if not response.content.startswith(b"%PDF"):
        raise RuntimeError(
            "Google Drive didn't return a PDF (got a confirmation/interstitial "
            "page instead) — the file may now be too large for a direct link, "
            "or access may have changed."
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)
    logger.info("Saved %s -> %s", filename, dest)
    return filename
