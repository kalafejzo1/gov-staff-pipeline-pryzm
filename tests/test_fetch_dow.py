"""
Tests for fetch_dow.py's HTML-parsing logic.

These test the regexes against realistic fixtures of the two pages in the
scrape chain (siliconvalleydefense.org -> Google Sites), without making any
network calls. Run with: python -m pytest tests/
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from fetch_dow import _find_drive_file, _find_sites_url

SVD_PAGE_FIXTURE = '''
<html><body>
<a href="/dow-directory">DoW Directory</a>
<a href="https://sites.google.com/lookleft.com/index/home">Download the Full Directory</a>
</body></html>
'''

SITES_PAGE_FIXTURE = '''
<html><body>
<iframe data-src="https://drive.google.com/file/d/1CAv7dyMU5LeYCCd69AYEFg6rBLK1eNVq/preview"
        aria-label="Drive, 2026 DoW Directory August 2026 Update 8.pdf"></iframe>
<a class="FKF6mc TpQm9d QmpIrf"
   href="https://drive.google.com/file/d/1CAv7dyMU5LeYCCd69AYEFg6rBLK1eNVq/view?usp=sharing"
   target="_blank" aria-label="Download the Directory">
  <div class="NsaAfc"><p>Download the Directory</p></div>
</a>
</body></html>
'''


def test_find_sites_url():
    assert _find_sites_url(SVD_PAGE_FIXTURE) == "https://sites.google.com/lookleft.com/index/home"


def test_find_sites_url_missing_raises():
    with pytest.raises(RuntimeError, match="sites.google.com"):
        _find_sites_url("<html><body>no link here</body></html>")


def test_find_drive_file():
    file_id, filename = _find_drive_file(SITES_PAGE_FIXTURE)
    assert file_id == "1CAv7dyMU5LeYCCd69AYEFg6rBLK1eNVq"
    assert filename == "2026 DoW Directory August 2026 Update 8.pdf"


def test_find_drive_file_missing_raises():
    with pytest.raises(RuntimeError, match="Download the Directory"):
        _find_drive_file("<html><body>no download button</body></html>")


def test_find_drive_file_falls_back_to_generated_filename():
    html = (
        '<a href="https://drive.google.com/file/d/ABC123/view" '
        'aria-label="Download the Directory"></a>'
    )
    file_id, filename = _find_drive_file(html)
    assert file_id == "ABC123"
    assert filename == "ABC123.pdf"
