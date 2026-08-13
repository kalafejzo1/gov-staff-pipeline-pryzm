"""
Tests for pae_watch.py.

Run with: python -m pytest tests/
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pae_watch import find_recent_pae_changes, announcements_to_leadership_rows, PAE_ANNOUNCEMENT_FIELDS
from utils import CSV_FIELDS


def test_find_recent_pae_changes_returns_announcements(monkeypatch):
    import pae_watch as pae_watch_module

    def fake_call_backend(client, prompt, system, backend, **kwargs):
        return (
            '{"announcements": [{"person_name": "Vice Adm. John Dougherty", '
            '"portfolio": "Aviation", "service": "Navy", "status": "interim/acting", '
            '"date": "2026-05-11", "source": "https://seapowermagazine.org/x", '
            '"summary": "Assigned as interim PAE Aviation."}]}'
        )

    monkeypatch.setattr(pae_watch_module, "call_backend", fake_call_backend)
    result = find_recent_pae_changes(None, backend="gemini", lookback_days=90)

    assert len(result) == 1
    assert result[0]["person_name"] == "Vice Adm. John Dougherty"
    assert result[0]["portfolio"] == "Aviation"


def test_find_recent_pae_changes_retries_once_on_malformed_json(monkeypatch):
    import pae_watch as pae_watch_module

    responses = iter([
        '{"announcements": [',  # truncated/invalid
        '{"announcements": [{"person_name": "Jane Doe", "portfolio": "Munitions", '
        '"service": "Navy", "status": "confirmed", "date": "2026-01-01", '
        '"source": "https://navy.mil/x", "summary": "..."}]}',
    ])
    calls = []

    def fake_call_backend(client, prompt, system, backend, **kwargs):
        calls.append(1)
        return next(responses)

    monkeypatch.setattr(pae_watch_module, "call_backend", fake_call_backend)
    result = find_recent_pae_changes(None, backend="gemini")

    assert len(calls) == 2
    assert len(result) == 1
    assert result[0]["person_name"] == "Jane Doe"


def test_find_recent_pae_changes_gives_up_and_returns_empty(monkeypatch):
    import pae_watch as pae_watch_module

    def fake_call_backend(client, prompt, system, backend, **kwargs):
        raise RuntimeError("GEMINI_API_KEY not set")

    monkeypatch.setattr(pae_watch_module, "call_backend", fake_call_backend)
    result = find_recent_pae_changes(None, backend="gemini")

    assert result == []


def test_find_recent_pae_changes_drops_entries_without_a_name(monkeypatch):
    import pae_watch as pae_watch_module

    def fake_call_backend(client, prompt, system, backend, **kwargs):
        return (
            '{"announcements": ['
            '{"person_name": "", "portfolio": "Missing name"}, '
            '{"portfolio": "No name field at all"}, '
            '{"person_name": "Real Person", "portfolio": "Fires"}'
            ']}'
        )

    monkeypatch.setattr(pae_watch_module, "call_backend", fake_call_backend)
    result = find_recent_pae_changes(None, backend="gemini")

    assert len(result) == 1
    assert result[0]["person_name"] == "Real Person"


def test_pae_announcement_fields_shape():
    assert "person_name" in PAE_ANNOUNCEMENT_FIELDS
    assert "source" in PAE_ANNOUNCEMENT_FIELDS


# ---------------------------------------------------------------------------
# announcements_to_leadership_rows
# ---------------------------------------------------------------------------

def test_announcements_to_leadership_rows_shape_matches_csv_fields():
    announcements = [{
        "person_name": "Vice Adm. John Dougherty", "portfolio": "Aviation", "service": "Navy",
        "status": "interim/acting", "date": "2026-05-11",
        "source": "https://www.navair.navy.mil/news/x", "summary": "Assigned as interim PAE Aviation.",
    }]
    rows = announcements_to_leadership_rows(announcements)
    assert len(rows) == 1
    assert set(rows[0].keys()) == set(CSV_FIELDS)


def test_announcements_to_leadership_rows_official_domain_is_high_confidence():
    announcements = [{
        "person_name": "Jane Doe", "portfolio": "Munitions", "service": "Navy",
        "status": "confirmed", "date": "2026-01-01",
        "source": "https://www.navy.mil/press/x", "summary": "...",
    }]
    rows = announcements_to_leadership_rows(announcements)
    assert rows[0]["confidence"] == "High"
    assert rows[0]["org_name"] == "PAE Munitions"
    assert "(Interim)" not in rows[0]["title"]


def test_announcements_to_leadership_rows_trade_press_is_medium_confidence():
    announcements = [{
        "person_name": "Jane Doe", "portfolio": "Munitions", "service": "Navy",
        "status": "interim/acting", "date": "2026-01-01",
        "source": "https://seapowermagazine.org/x", "summary": "...",
    }]
    rows = announcements_to_leadership_rows(announcements)
    assert rows[0]["confidence"] == "Medium"
    assert "(Interim)" in rows[0]["title"]
    assert "confirm if made permanent" in rows[0]["notes"].lower()


def test_announcements_to_leadership_rows_handles_missing_fields():
    rows = announcements_to_leadership_rows([{"person_name": "Jane Doe"}])
    assert len(rows) == 1
    assert rows[0]["person_name"] == "Jane Doe"
    assert rows[0]["confidence"] == "Medium"  # no source -> not an official domain match
    assert rows[0]["org_name"] == "PAE Unknown Portfolio"
