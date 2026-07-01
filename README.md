# gov-staff-pipeline-pryzm

Converts a list of government or military organization names into a structured CSV of current leadership, ready for CRM import.

---

## How It Works

The pipeline runs in 5 stages automatically:

1. **Parse input** — accepts a comma-separated list of org names or an org chart image
2. **DoW Directory 2026** — scans the DoW reference PDF offline (no API quota) for High-confidence leaders
3. **Gemini web search** — supplements the PDF using a 4-tier source strategy: `.mil`/`.gov` pages → DVIDS/press releases → SAM.gov/USASpending → LinkedIn (last resort only)
4. **FOM PDF** *(optional)* — parse a Functional Organization Manual for org descriptions
5. **Merge + export** — combines all sources, deduplicates by name, writes CRM-ready CSV

**Source priority:** DoW Directory leaders always come first. Web search adds anyone not already found. LinkedIn results are only included when no official source exists, and are flagged with `confidence = Low`.

**Recency policy (team decision, 2026-06-29):** Sources older than 3 years are excluded entirely. Leadership data that old creates more CRM noise than it resolves.

---

## Output

Every run produces a CSV with these 8 columns:

| Column | Description |
|---|---|
| `org_name` | The organization searched |
| `person_name` | Full name of the leader |
| `title` | Exact title as found in the source |
| `source` | Where the data came from (e.g. `DoW Directory 2026`, `navy.mil/leadership`, `dvidshub.net`) |
| `confidence` | `High` = official .mil/.gov or DoW PDF · `Medium` = DVIDS/SAM.gov/press release · `Low` = LinkedIn only |
| `notes` | Acting status, appointment date, caveats (e.g. `"LinkedIn only — verify before outreach"`) |
| `description` | Org mission/overview from the FOM PDF, if provided |
| `acronym` | Known acronym for the org (e.g. `ONR`, `DIU`), if found |

---

## Prerequisites

- **Python 3.9 or newer**
- **Gemini API key** — use `gemini-2.5-flash` (free tier); `gemini-2.0-flash` free tier has zero quota as of mid-2026
  - Get one at [aistudio.google.com](https://aistudio.google.com/) — create a key under a project with no billing ever enabled
- **Anthropic API key** *(optional)* — only needed for `--image` or `--backend anthropic`
  - Get one at [console.anthropic.com](https://console.anthropic.com/)

---

## Installation

```bash
cd gov-staff-pipeline-pryzm
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and add your Gemini key:

```
GEMINI_API_KEY=your_key_here
```

> **Security:** Never commit `.env` — it contains your API keys. The `.gitignore` already excludes it.

---

## Usage

Always activate the virtual environment first:

```bash
source .venv/bin/activate
```

### Basic — org names → CSV

```bash
python3 pipeline.py \
  --orgs "Defense Innovation Unit, Office of Naval Research" \
  --output outputs/leadership.csv
```

### With DoW PDF (recommended — adds High-confidence leaders from offline source)

```bash
python3 pipeline.py \
  --orgs "Office of Naval Research" \
  --dow-pdf data/2026_DoW_Directory.pdf \
  --output outputs/onr.csv
```

### DoW PDF only (zero API quota)

```bash
python3 pipeline.py \
  --orgs "Army Material Command, Army Software Factory" \
  --no-search \
  --output outputs/dow_only.csv
```

### From an org chart image

```bash
python3 pipeline.py \
  --image orgchart.png \
  --output outputs/orgs.csv
```

### All arguments

| Argument | Default | Description |
|---|---|---|
| `--orgs` | — | Comma-separated org names *(required unless `--image`)* |
| `--image` | — | Org chart image PNG/JPG/WebP *(required unless `--orgs`)* |
| `--dow-pdf` | `data/2026_DoW_Directory.pdf` | DoW Directory PDF — scanned before web search |
| `--backend` | `auto` | `gemini` (recommended) · `ollama` · `anthropic` · `auto` (Gemini → Ollama fallback) |
| `--pdf` | — | Functional Organization Manual PDF for org descriptions |
| `--output` | `outputs/org_output.csv` | Output CSV path |
| `--no-search` | off | Skip web search — use DoW PDF only, no API quota consumed |
| `--verbose` | off | Show debug-level logging |

---

## Resuming an interrupted run

Progress is checkpointed after every org. If a run is interrupted (quota, crash, Ctrl+C), just rerun the exact same command — it picks up where it left off automatically.

---

## Running Tests

```bash
python3 -m pytest tests/
```

25 tests covering row building, DoW PDF parsing, program office detection, and source merging.

---

## File Structure

```
gov-staff-pipeline-pryzm/
├── pipeline.py           — Orchestration: run_pipeline() API + CLI entry point
├── backends.py           — AI backend dispatch (Gemini, Ollama, Anthropic)
├── dow.py                — DoW Directory PDF parser
├── search.py             — Web search stage (prompt builder + backend dispatch)
├── utils.py              — Shared infrastructure (API clients, CSV writer, retry logic)
├── data/
│   └── 2026_DoW_Directory.pdf  — DoW Directory, used as primary source
├── tests/
│   └── test_pipeline.py  — Unit tests (25 passing)
├── requirements.txt      — Python dependencies
├── .env.example          — API key template (copy to .env and fill in)
└── .gitignore            — Excludes .env, outputs/, and generated files
```

---

## Troubleshooting

**`GEMINI_API_KEY not set`**
→ Add `GEMINI_API_KEY=your_key` to `.env` — get a free key at [aistudio.google.com](https://aistudio.google.com/)

**`429 RESOURCE_EXHAUSTED` / `limit: 0` on Gemini**
→ `gemini-2.0-flash` free tier quota is zero as of mid-2026. The pipeline uses `gemini-2.5-flash` which still has free tier access. If you're still hitting this, your Google project may have had billing enabled at some point — create a fresh API key under a new project with billing never enabled.

**`Gemini daily quota exhausted`**
→ Quota resets at midnight Pacific. Run with `--no-search` in the meantime to use the DoW PDF only.

**`ModuleNotFoundError`**
→ Activate the virtual environment: `source .venv/bin/activate`

**Run crashed mid-way**
→ Rerun the exact same command — the checkpoint file saves progress after each org.
