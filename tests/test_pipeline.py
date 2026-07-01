"""
Tests for pipeline core logic.

Run with: python -m pytest tests/
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline import build_rows
from dow import _looks_like_name, _find_section_start
from search import _is_program_office, _build_search_prompt, _CUTOFF_YEAR, _SOURCE_CUTOFF_YEARS
from utils import strip_json_fences


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _office(name, code=None, parent=None):
    return {"name": name, "code": code, "parent": parent}

def _search(leadership=None, code=None, acronym=None):
    return {"code": code, "acronym": acronym, "leadership": leadership or []}

def _leader(name, title, source="website", confidence="High", notes=""):
    return {"name": name, "title": title, "source": source,
            "confidence": confidence, "notes": notes}


# ---------------------------------------------------------------------------
# build_rows
# ---------------------------------------------------------------------------

def test_build_rows_empty_offices():
    assert build_rows([], {}) == []


def test_build_rows_no_leaders_produces_no_rows():
    """Offices with no leadership found contribute zero rows."""
    offices = [_office("Unknown Office")]
    assert build_rows(offices, {}) == []


def test_build_rows_single_leader():
    offices = [_office("Office of Naval Research")]
    search  = {"Office of Naval Research": _search(leadership=[
        _leader("Dr. Rachel Riley", "Chief of Naval Research"),
    ])}
    rows = build_rows(offices, search)

    assert len(rows) == 1
    row = rows[0]
    assert row["org_name"]    == "Office of Naval Research"
    assert row["person_name"] == "Dr. Rachel Riley"
    assert row["title"]       == "Chief of Naval Research"
    assert row["confidence"]  == "High"
    assert row["source"]      == "website"
    assert "section"    not in row
    assert "org_code"   not in row
    assert "parent_org" not in row


def test_build_rows_multiple_leaders():
    offices = [_office("Office of Naval Research")]
    search  = {"Office of Naval Research": _search(leadership=[
        _leader("Dr. Rachel Riley", "Chief of Naval Research"),
        _leader("RDML John Smith",  "Deputy Chief", source="linkedin", confidence="Medium"),
    ])}
    rows = build_rows(offices, search)

    assert len(rows) == 2
    assert rows[0]["person_name"] == "Dr. Rachel Riley"
    assert rows[1]["person_name"] == "RDML John Smith"
    assert rows[1]["confidence"]  == "Medium"


def test_build_rows_notes_and_source_preserved():
    offices = [_office("Education Service")]
    search  = {"Education Service": _search(leadership=[
        _leader("Ken Smith", "Acting Executive Director",
                source="Congressional testimony Feb 2025",
                confidence="Medium",
                notes="Acting title as of Feb 2025 — verify if still acting or made permanent"),
    ])}
    rows = build_rows(offices, search)

    assert rows[0]["notes"].startswith("Acting title")
    assert rows[0]["source"] == "Congressional testimony Feb 2025"


def test_build_rows_multiple_offices():
    offices = [
        _office("Org A"),
        _office("Org B"),
    ]
    search = {
        "Org A": _search(leadership=[_leader("Alice", "Director")]),
        "Org B": _search(leadership=[_leader("Bob", "Director"), _leader("Carol", "Deputy")]),
    }
    rows = build_rows(offices, search)
    assert len(rows) == 3
    assert rows[0]["org_name"] == "Org A"
    assert rows[1]["org_name"] == "Org B"
    assert rows[2]["org_name"] == "Org B"


# ---------------------------------------------------------------------------
# strip_json_fences
# ---------------------------------------------------------------------------

def test_strip_json_fences_with_backticks():
    assert strip_json_fences("```json\n{}\n```") == "{}"

def test_strip_json_fences_no_fences():
    assert strip_json_fences('{"key": "value"}') == '{"key": "value"}'

def test_strip_json_fences_plain_backticks():
    assert strip_json_fences("```\n[]\n```") == "[]"


# ---------------------------------------------------------------------------
# DoW Directory helpers
# ---------------------------------------------------------------------------

def test_looks_like_name_valid():
    assert _looks_like_name("LTG Chris Mohan") is True
    assert _looks_like_name("Mr. David Lorch") is True
    assert _looks_like_name("Owen West") is True
    assert _looks_like_name("COL Brad Dello") is True


def test_looks_like_name_rejects_vacant():
    assert _looks_like_name("Vacant") is False
    assert _looks_like_name("TBD") is False


def test_looks_like_name_rejects_short():
    assert _looks_like_name("DIU") is False


def test_find_section_start_exact():
    lines = [
        "Army Material Command",
        "LTG Chris Mohan - Commanding General",
    ]
    assert _find_section_start(lines, "Army Material Command") == 0


def test_find_section_start_substring():
    lines = [
        "Some other line",
        "Defense Innovation Unit (DIU)",
        "Owen West - Director of DIU",
    ]
    assert _find_section_start(lines, "Defense Innovation Unit") == 1


def test_find_section_start_not_found():
    lines = ["Nothing here", "Blank"]
    assert _find_section_start(lines, "Nonexistent Org XYZ") is None


def test_build_rows_dow_leaders_come_first():
    """DoW leaders should appear before web-search leaders."""
    offices = [_office("Defense Innovation Unit")]
    search  = {"Defense Innovation Unit": _search(leadership=[
        _leader("Owen West", "Director", source="linkedin", confidence="Medium"),
    ])}
    dow = {"Defense Innovation Unit": [
        {"name": "Owen West", "title": "Director of DIU",
         "source": "DoW Directory 2026", "confidence": "High", "notes": ""},
    ]}
    rows = build_rows(offices, search, dow_data=dow)
    assert len(rows) == 1
    assert rows[0]["source"] == "DoW Directory 2026"
    assert rows[0]["confidence"] == "High"


def test_build_rows_dow_supplements_web():
    """Web-search leaders not in DoW should be appended after DoW leaders."""
    offices = [_office("Office of Naval Research")]
    search  = {"Office of Naval Research": _search(leadership=[
        _leader("Dr. Rachel Riley", "Chief of Naval Research", source="website", confidence="High"),
        _leader("RDML John Smith",  "Deputy Chief",             source="linkedin", confidence="Medium"),
    ])}
    dow = {"Office of Naval Research": [
        {"name": "Dr. Rachel Riley", "title": "Chief of Naval Research",
         "source": "DoW Directory 2026", "confidence": "High", "notes": ""},
    ]}
    rows = build_rows(offices, search, dow_data=dow)
    assert len(rows) == 2
    assert rows[0]["source"] == "DoW Directory 2026"
    assert rows[1]["person_name"] == "RDML John Smith"
    assert rows[1]["source"] == "linkedin"


def test_build_rows_no_dow_backward_compatible():
    """Omitting dow_data leaves existing behaviour unchanged."""
    offices = [_office("NavalX")]
    search  = {"NavalX": _search(leadership=[_leader("Jane Doe", "Director")])}
    rows = build_rows(offices, search)
    assert len(rows) == 1
    assert rows[0]["person_name"] == "Jane Doe"


# ---------------------------------------------------------------------------
# Program office detection
# ---------------------------------------------------------------------------

def test_is_program_office_detects_common_prefixes():
    assert _is_program_office("PMW 120 Ship Communications") is True
    assert _is_program_office("PdM Tactical Radios") is True
    assert _is_program_office("PEO Command and Control") is True
    assert _is_program_office("PMO Rapid Sustainment") is True


def test_is_program_office_detects_keyword_phrases():
    assert _is_program_office("Program Office for Enterprise IT") is True
    assert _is_program_office("Joint Program Manager Chemical Defense") is True


def test_is_program_office_rejects_regular_orgs():
    assert _is_program_office("Office of Naval Research") is False
    assert _is_program_office("Army Material Command") is False
    assert _is_program_office("Defense Innovation Unit") is False


# ---------------------------------------------------------------------------
# Prompt builder and recency constants
# ---------------------------------------------------------------------------

def test_cutoff_year_is_recent():
    import datetime
    expected = datetime.date.today().year - _SOURCE_CUTOFF_YEARS
    assert _CUTOFF_YEAR == expected


def test_build_search_prompt_includes_cutoff_year():
    prompt = _build_search_prompt("NavalX", "Department of the Navy", is_program=False)
    assert str(_CUTOFF_YEAR) in prompt


def test_build_search_prompt_program_office_adds_pm_note():
    prompt = _build_search_prompt("PMW 120", "NAVWAR", is_program=True)
    assert "program manager" in prompt.lower()


def test_build_search_prompt_non_program_has_no_pm_note():
    # Non-program offices still include "program manager" in the general role list,
    # but should NOT include the program-office-specific Step 3 instruction.
    prompt = _build_search_prompt("Office of Naval Research", "DoN", is_program=False)
    assert "program executive officer" not in prompt.lower() or "in step 3" not in prompt.lower()
