# gov-staff-pipeline-pryzm

Converts a list of government or military organization names into a structured CSV of current leadership, ready for CRM import.

---

## Web Dashboard (no terminal required)

For non-technical use, `app.py` wraps the pipeline in a Streamlit dashboard: type organization names in a browser, click **Generate CSV**, download the result. No install, no API keys to manage — those are configured once by whoever deploys it.

**If it's already deployed,** just open the shared link.

**To deploy it yourself** (~5 minutes, one-time):

1. Push this repo to GitHub (already done if you're reading this on GitHub).
2. Go to [share.streamlit.io](https://share.streamlit.io), sign in with GitHub, and click **New app**.
3. Pick this repo, branch `main`, and set the main file to `app.py`.
4. Under **Advanced settings → Secrets**, paste:
   ```toml
   GEMINI_API_KEY = "your_gemini_key"
   ANTHROPIC_API_KEY = "your_anthropic_key"
   ```
5. Click **Deploy**. You'll get a `*.streamlit.app` URL to share with your team.
6. Optional but recommended for a shared/internal tool: under app **Settings → Sharing**, restrict viewers to specific email addresses so the (shared) API quota isn't open to the public internet.

**DoW Directory PDF:** the dashboard's Advanced options has a **Fetch latest from siliconvalleydefense.org** button — it follows that site's link chain (siliconvalleydefense.org → Google Sites → Google Drive) and downloads whichever revision is currently posted, caching it at `data/2026_DoW_Directory.pdf` so every run after that uses it automatically. On Streamlit Community Cloud, that cache is cleared whenever the app restarts (redeploy, or after enough idle time), so whoever's using it may need to click it again occasionally — it's fast (a few seconds).

The scraper depends on the HTML structure of two third-party pages it doesn't control, so if siliconvalleydefense.org or the linked Google Sites page gets redesigned, the fetch will fail with a specific error rather than silently returning nothing. If that happens, the fallback is to download the PDF yourself from the site and either upload it via a file uploader (not currently in the dashboard — ask for it if you want it) or place it at `data/2026_DoW_Directory.pdf` locally / `git add -f` it into the repo before deploying (only do this if the repo is private, or you've confirmed the PDF is fine to publish — it's excluded from git by default).

To run the dashboard on your own laptop instead of deploying it:

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then fill in your keys
streamlit run app.py
```

**PAE Leadership Watch:** a separate section below the main org-lookup form. Unlike the main
flow (which asks "who leads org X" for a known org), this asks a broad question — "what PAE
(Portfolio Acquisition Executive) changes have been announced recently" — across official
`.mil`/`defense.gov` press releases *and* defense trade press (Seapower Magazine, Breaking
Defense, AFCEA, ExecutiveGov, MeriTalk). PAE is a newer, actively-rolling-out acquisition-
leadership construct (broader authority than the older PEO model), and new appointments often
show up in press coverage well before any per-org directory or website catches up — this exists
specifically to surface those before they'd otherwise be found. Pick a lookback window (30–365
days) and click **Scan for PAE changes**; results download the same way as the main flow (CSV or
Excel). Logic lives in `pae_watch.py`.

---

## Command-line usage

Everything below describes the original CLI (`pipeline.py`). Use it if you want scripting/automation instead of the dashboard.

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
| `confidence` | `High` = official .mil/.gov or DoW PDF · `Medium` = DVIDS/SAM.gov/press release · `Low` = LinkedIn only (strict validation applied — excluded if moved on, end-dated, retired, or showing Open to Work) |
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

### Use as a library (no CLI required)

```python
from pathlib import Path
from pipeline import run_pipeline

rows = run_pipeline(
    orgs=["Office of Naval Research", "NavalX"],
    dow_pdf=Path("data/2026_DoW_Directory.pdf"),
    output=Path("outputs/leadership.csv"),
)
```

### All arguments

| Argument | Default | Description |
|---|---|---|
| `--orgs` | — | Comma-separated org names *(required unless `--image`)* |
| `--image` | — | Org chart image PNG/JPG/WebP *(required unless `--orgs`)* |
| `--dow-pdf` | `data/2026_DoW_Directory.pdf` | DoW Directory PDF — scanned before web search |
| `--backend` | `gemini` | `gemini` (Google Search grounding) · `anthropic` (built-in web search tool) |
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
├── app.py                — Streamlit dashboard (non-technical front end)
├── pipeline.py           — Orchestration: run_pipeline() API + CLI entry point
├── backends.py           — AI backend dispatch (Gemini, Anthropic)
├── dow.py                — DoW Directory PDF parser
├── fetch_dow.py           — Downloads the latest DoW Directory PDF from siliconvalleydefense.org
├── search.py             — Web search stage (prompt builder + backend dispatch)
├── pae_watch.py          — Scans press/trade press for recent PAE leadership changes
├── utils.py              — Shared infrastructure (API clients, CSV/xlsx writers, retry logic)
├── data/
│   └── 2026_DoW_Directory.pdf  — DoW Directory (fetched, not committed — see Web Dashboard section)
├── tests/
│   ├── test_pipeline.py  — Unit tests
│   ├── test_fetch_dow.py — Tests for the DoW-fetch HTML parsing
│   └── test_pae_watch.py — Tests for the PAE watch feature
├── requirements.txt      — Python dependencies
├── .streamlit/
│   └── secrets.toml.example — API key template for the dashboard
├── .env.example          — API key template (copy to .env and fill in)
└── .gitignore            — Excludes .env, outputs/, fetched PDFs, and generated files
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
