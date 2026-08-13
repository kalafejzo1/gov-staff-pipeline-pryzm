"""
Tests for app.py — the Streamlit dashboard.

Uses Streamlit's own AppTest framework to drive the real script (no browser
needed, no API keys needed — these tests never click Generate/Scan, so they
never make a network call).

Run with: python -m pytest tests/
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from streamlit.testing.v1 import AppTest

_APP_PATH = str(Path(__file__).parent.parent / "app.py")


def test_app_loads_without_error():
    at = AppTest.from_file(_APP_PATH)
    at.run(timeout=30)
    assert not at.exception


def test_uploading_org_list_file_prefills_text_area():
    # Regression test for the bulk-upload feature: uploading a .txt/.csv of
    # org names should populate the text area with its contents, so a large
    # batch doesn't have to be pasted by hand.
    at = AppTest.from_file(_APP_PATH)
    at.run(timeout=30)
    assert not at.exception

    org_list_uploader = at.file_uploader[0]
    assert "upload a list" in org_list_uploader.label.lower()

    content = b"Office of Naval Research\nDefense Innovation Unit\nNavalX\n"
    org_list_uploader.upload("orgs.txt", content, "text/plain")
    at.run(timeout=30)
    assert not at.exception

    text_area = at.text_area(key="orgs_text_area")
    assert text_area.value == content.decode()


def test_uploading_csv_org_list_also_works():
    at = AppTest.from_file(_APP_PATH)
    at.run(timeout=30)
    assert not at.exception

    org_list_uploader = at.file_uploader[0]
    content = b"Office of Naval Research\nNavalX\n"
    org_list_uploader.upload("orgs.csv", content, "text/csv")
    at.run(timeout=30)
    assert not at.exception

    text_area = at.text_area(key="orgs_text_area")
    assert text_area.value == content.decode()
