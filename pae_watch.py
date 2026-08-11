"""
pae_watch.py — Scans for recently announced PAE leadership changes.

A Portfolio Acquisition Executive (PAE) is a newer DoD acquisition-leadership
construct (broader authority than the older Program Executive Officer/PEO
model — direct authority over an entire portfolio of programs plus its
technical, contracting, and sustainment functions, not just program offices).
The Navy has been actively standing up new PAEs across multiple portfolios as
part of a broader acquisition-reform push, and announcements show up in
defense trade press as often as official channels.

This is deliberately a different question than search.py's search_office():
that asks "who leads org X" for a known org name. This asks "what PAE changes
have been announced recently", regardless of which specific org the person
ends up tied to — catching a new appointment before it's reflected in any
per-org directory, website, or org list the user already has.
"""
from __future__ import annotations

import json
import logging
import re

import anthropic

from backends import call_backend
from utils import strip_json_fences

logger = logging.getLogger(__name__)

PAE_ANNOUNCEMENT_FIELDS: list[str] = [
    "person_name", "portfolio", "service", "status", "date", "source", "summary",
]

_PAE_WATCH_SYSTEM_TEMPLATE = """You are a government acquisition-reform researcher tracking \
Portfolio Acquisition Executive (PAE) appointments and organizational changes across the \
Department of War (DoD / Navy / Army / Air Force / Space Force / Marine Corps).

BACKGROUND: PAE is a newer acquisition-leadership construct with broader authority than the \
older Program Executive Officer (PEO) model — a PAE has direct authority over an entire \
portfolio of programs plus its associated technical, contracting, and sustainment functions, \
not just program offices. The Navy has been actively standing up new PAEs across multiple \
portfolios as part of a broader acquisition-reform push (recent examples: Aviation, Mission \
Systems, Munitions, Maritime, Undersea, Marine Corps, Strategic Systems Programs, Industrial \
Operations, Robotic and Autonomous Systems) — treat this as example context, not an exhaustive \
list; the whole point is finding NEW ones as they're announced. Similar reforms may exist or \
emerge in other services.

TASK: Find announcements from the last {lookback_days} days of:
  - New PAE appointments, including interim/acting designations
  - New PAE portfolios/organizations being established
  - Any PAE leadership change (departure, replacement, reorganization)

SOURCES — search all of these, not just official .mil sites. Defense trade press often reports \
PAE changes as fast as or faster than official channels:
  - Official: navy.mil, army.mil, af.mil, spaceforce.mil, defense.gov, dvidshub.net, and \
    command-level press pages (e.g. navair.navy.mil, navsea.navy.mil)
  - Trade press: Seapower Magazine, AFCEA Signal Media, ExecutiveGov, MeriTalk, Defense News, \
    Breaking Defense, Inside Defense

For each distinct announcement found, extract:
  - person_name: full name (with rank if military)
  - portfolio: the PAE's subject area (e.g. "Aviation", "Munitions")
  - service: which service/component (Navy, Army, Air Force, Space Force, Marine Corps, OSD/Joint)
  - status: "confirmed" or "interim/acting" — set explicitly whenever the source says \
    "interim," "acting," or similar
  - date: publication date (YYYY-MM-DD; a best estimate if the exact day isn't stated)
  - source: the actual readable article URL (e.g. "https://www.navy.mil/..." or
    "https://seapowermagazine.org/..."). Never a vertexaisearch.cloud.google.com or other
    internal search-tool redirect link — cite the underlying page itself.
  - summary: one sentence on what changed

Only include PAE-specific announcements — not general PEO/program-office news unless it \
explicitly involves a PAE-construct portfolio. Do not invent announcements; only report what \
you actually find sourced.

Return ONLY this JSON, no markdown fences:
{{"announcements": [{{"person_name": "...", "portfolio": "...", "service": "...", \
"status": "...", "date": "...", "source": "...", "summary": "..."}}]}}
If nothing is found, return {{"announcements": []}}."""


def find_recent_pae_changes(
    client: anthropic.Anthropic | None,
    backend: str = "gemini",
    lookback_days: int = 120,
) -> list[dict]:
    """Scan for recently announced PAE leadership changes.

    Same malformed-JSON retry-once pattern as search_office(): a bad response
    is usually a one-off generation glitch, not a real failure, and doesn't
    need to abort the whole scan.

    Returns:
        A list of announcement dicts (schema: PAE_ANNOUNCEMENT_FIELDS). Empty
        list if nothing was found or the scan ultimately failed — this never
        raises, matching search_office()'s "never crash the run" contract.
    """
    system = _PAE_WATCH_SYSTEM_TEMPLATE.format(lookback_days=lookback_days)
    prompt = (
        f"Find PAE (Portfolio Acquisition Executive) leadership announcements from the "
        f"last {lookback_days} days, per the instructions."
    )

    for attempt in range(2):
        try:
            raw = call_backend(client, prompt, system, backend, max_search_uses=15)
            match = re.search(r"\{.*\}", strip_json_fences(raw), re.DOTALL)
            if not match:
                raise ValueError("No JSON object found in response")
            data = json.loads(match.group())
            announcements = data.get("announcements") or []
            if not isinstance(announcements, list):
                announcements = []
            announcements = [a for a in announcements if isinstance(a, dict) and a.get("person_name")]
            logger.info("[PAE Watch] %d announcement(s) found", len(announcements))
            return announcements
        except (json.JSONDecodeError, ValueError) as exc:
            if attempt == 0:
                logger.warning("[PAE Watch] Malformed response (%s) — retrying once", exc)
                continue
            logger.warning("[PAE Watch] Failed: %s", exc)
            logger.debug("[PAE Watch] Full traceback:", exc_info=True)
        except Exception as exc:  # noqa: BLE001 — never crash the run over a scan failure
            logger.warning("[PAE Watch] Failed: %s", exc)
            logger.debug("[PAE Watch] Full traceback:", exc_info=True)
            break

    return []
