"""
app.py — Web dashboard for the org-pipeline.

A non-technical front end for pipeline.run_pipeline(): type organization
names (or upload an org chart image) in a browser, click a button, and
download a CRM-ready CSV. No terminal, no CLI flags.

Run locally:   streamlit run app.py
Deploy:        push to GitHub, deploy on Streamlit Community Cloud, add
                GEMINI_API_KEY / ANTHROPIC_API_KEY under app Secrets.
"""
from __future__ import annotations

import datetime
import logging
import os
import tempfile
import uuid
from pathlib import Path

import streamlit as st

from fetch_dow import fetch_latest_dow_pdf
from pipeline import run_pipeline
from utils import CSV_FIELDS, rows_to_xlsx_bytes

st.set_page_config(page_title="Gov Org Leadership Lookup", page_icon="🗂️", layout="wide")

# ---------------------------------------------------------------------------
# Theme — matches the Pryzm CRM's look (Inter Tight, warm near-black/white,
# barely-rounded corners, subtle hairline borders instead of heavy fills).
# Colors/radii are set in .streamlit/config.toml; this fills in what config.toml
# can't reach (custom font, border weight/opacity, content width, alert style).
# ---------------------------------------------------------------------------
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;500;600;700&display=swap');

    html, body, [class*="css"], .stApp, .stMarkdown, .stButton button,
    .stTextArea textarea, .stTextInput input, [data-testid="stWidgetLabel"] p {
        font-family: 'Inter Tight', ui-sans-serif, system-ui, -apple-system,
                      'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif !important;
    }

    [data-testid="stMainBlockContainer"] {
        max-width: 880px;
        margin-left: auto;
        margin-right: auto;
        padding-top: 3rem;
    }

    h1, h2, h3, [data-testid="stHeading"] {
        color: #211F1C !important;
        font-weight: 600 !important;
        letter-spacing: -0.01em;
    }

    [data-testid="stBaseButton-primary"], [data-testid="stBaseButton-secondary"],
    [data-testid="stDownloadButton"] button {
        border-radius: 4px !important;
        font-weight: 500 !important;
        transition: background-color 120ms ease, border-color 120ms ease;
    }
    [data-testid="stBaseButton-secondary"], [data-testid="stDownloadButton"] button[kind="secondary"] {
        border: 1px solid rgba(33, 31, 28, 0.15) !important;
    }
    [data-testid="stBaseButton-secondary"]:hover {
        border-color: rgba(33, 31, 28, 0.4) !important;
        background-color: #FAFAF9 !important;
    }
    [data-testid="stBaseButton-primary"]:hover, [data-testid="stDownloadButton"] button:hover {
        background-color: #3A3733 !important;
        border-color: #3A3733 !important;
    }

    .stTextArea textarea, .stTextInput input, [data-testid="stFileUploaderDropzone"] {
        border-radius: 4px !important;
        border-color: rgba(33, 31, 28, 0.14) !important;
    }
    .stTextArea textarea:focus, .stTextInput input:focus {
        border-color: #211F1C !important;
        box-shadow: 0 0 0 1px #211F1C !important;
    }

    [data-testid="stExpander"] {
        border: 1px solid rgba(33, 31, 28, 0.10) !important;
        border-radius: 6px !important;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
    }

    [data-testid="stAlertContainer"] {
        border-radius: 6px !important;
        border: 1px solid rgba(33, 31, 28, 0.08) !important;
        background-color: #FFFFFF !important;
    }
    [data-testid="stAlertContainer"] p { color: #211F1C !important; }

    [data-testid="stDataFrame"] {
        border: 1px solid rgba(33, 31, 28, 0.10) !important;
        border-radius: 6px !important;
    }

    [data-testid="stMetric"] {
        background-color: #FFFFFF;
        border: 1px solid rgba(33, 31, 28, 0.10);
        border-radius: 6px;
        padding: 0.75rem 1rem;
    }
    [data-testid="stMetricLabel"] { color: #6B6A63 !important; }

    [data-testid="stStatusWidget"] {
        border: 1px solid rgba(33, 31, 28, 0.10) !important;
        border-radius: 6px !important;
    }

    hr { border-color: rgba(33, 31, 28, 0.08) !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# API keys — pulled from Streamlit Secrets so end users never see or enter
# them. Falls back to a local .env for `streamlit run app.py` on a laptop.
# ---------------------------------------------------------------------------
try:
    for _key in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        if _key in st.secrets and _key not in os.environ:
            os.environ[_key] = st.secrets[_key]
except st.errors.StreamlitSecretNotFoundError:
    pass  # no secrets.toml yet — fine for local dev; .env still works below

from utils import load_env  # noqa: E402  (after os.environ is seeded)
load_env()

_DOW_PDF_DEFAULT = Path("data/2026_DoW_Directory.pdf")

st.title("Gov Org Leadership Lookup")
st.caption(
    "Type organization names, click **Generate**, and download a spreadsheet of "
    "current leadership — ready to import into the CRM."
)

with st.expander("What do the columns mean?"):
    st.markdown(
        "- **confidence** — `High` = official .mil/.gov site or the DoW Directory PDF · "
        "`Medium` = DVIDS/SAM.gov/press release · `Low` = LinkedIn only\n"
        "- **notes** — acting status, appointment date, or caveats\n"
        "- **source** — where the leader's name/title came from\n"
        "- Sources older than 3 years are excluded automatically."
    )

mode = st.radio(
    "How do you want to provide organizations?",
    ["Type organization names", "Upload an org chart image"],
    horizontal=True,
)

orgs_text = ""
image_file = None

if mode == "Type organization names":
    orgs_text = st.text_area(
        "Organization names (one per line, or comma-separated)",
        placeholder="Office of Naval Research\nDefense Innovation Unit\nNavalX",
        height=140,
    )
else:
    image_file = st.file_uploader("Org chart image", type=["png", "jpg", "jpeg", "webp"])
    st.caption("The chart is parsed automatically to find the offices in it.")

with st.expander("Advanced options"):
    fom_file = st.file_uploader(
        "Functional Organization Manual PDF (optional — adds office descriptions)",
        type=["pdf"],
    )
    st.markdown("**DoW Directory 2026** (offline reference — checked before web search)")
    if _DOW_PDF_DEFAULT.exists():
        updated = datetime.datetime.fromtimestamp(_DOW_PDF_DEFAULT.stat().st_mtime)
        st.success(f"Loaded — last fetched {updated:%Y-%m-%d %H:%M}.")
    else:
        st.info("Not loaded yet — fetch it below, or leave it and rely on web search only.")

    if st.button("Fetch latest from siliconvalleydefense.org", icon=":material/refresh:"):
        with st.spinner("Fetching the current DoW Directory..."):
            try:
                filename = fetch_latest_dow_pdf(_DOW_PDF_DEFAULT)
                st.success(f"Downloaded: {filename}")
                st.rerun()
            except Exception as exc:  # noqa: BLE001 — surface any scrape/download failure
                st.error(f"Couldn't fetch the directory: {exc}")

    dow_pdf_path: Path | None = _DOW_PDF_DEFAULT if _DOW_PDF_DEFAULT.exists() else None
    no_search = st.checkbox(
        "Skip web search (offline / DoW Directory only — fastest, uses no API quota)",
        value=False,
    )

run_clicked = st.button("Generate CSV", type="primary")

if run_clicked:
    orgs = [o.strip() for o in orgs_text.replace(",", "\n").splitlines() if o.strip()]

    if mode == "Type organization names" and not orgs:
        st.error("Enter at least one organization name.")
        st.stop()
    if mode == "Upload an org chart image" and not image_file:
        st.error("Upload an org chart image.")
        st.stop()

    missing_keys = []
    if not no_search and not os.environ.get("GEMINI_API_KEY"):
        missing_keys.append("GEMINI_API_KEY (needed for web search)")
    if (mode == "Upload an org chart image" or fom_file) and not os.environ.get("ANTHROPIC_API_KEY"):
        missing_keys.append("ANTHROPIC_API_KEY (needed to read the image/PDF)")
    if missing_keys:
        st.error(
            "This dashboard isn't fully set up yet — missing " + " and ".join(missing_keys) +
            ". Ask whoever deployed it to add these under the app's Settings → Secrets."
        )
        st.stop()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        image_path = None
        if image_file:
            image_path = tmpdir_path / image_file.name
            image_path.write_bytes(image_file.getvalue())

        fom_path = None
        if fom_file:
            fom_path = tmpdir_path / fom_file.name
            fom_path.write_bytes(fom_file.getvalue())

        output_path = tmpdir_path / f"{uuid.uuid4().hex}.csv"

        rows = []
        pipeline_error = None

        with st.status("Working — this can take a minute or two for several organizations...", expanded=True) as status:
            log_area = st.empty()
            log_lines: list[str] = []

            class _UIHandler(logging.Handler):
                def emit(self, record: logging.LogRecord) -> None:
                    log_lines.append(self.format(record))
                    log_area.code("\n".join(log_lines[-20:]) or " ")

            handler = _UIHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            root_logger = logging.getLogger()
            prev_level = root_logger.level
            root_logger.setLevel(logging.INFO)
            root_logger.addHandler(handler)

            try:
                rows = run_pipeline(
                    orgs=orgs or None,
                    image=image_path,
                    pdf=fom_path,
                    dow_pdf=dow_pdf_path,
                    output=output_path,
                    backend="auto",
                    no_search=no_search,
                )
            except FileNotFoundError as exc:
                pipeline_error = f"File not found: {exc}"
            except ValueError as exc:
                pipeline_error = f"Invalid input: {exc}"
            except Exception as exc:  # noqa: BLE001 — surface any pipeline failure as a friendly message
                pipeline_error = f"The pipeline hit an error: {exc}"
            finally:
                root_logger.removeHandler(handler)
                root_logger.setLevel(prev_level)

            # Individual org searches never raise (search_office logs and returns
            # empty instead) — so a real per-org failure only shows up in this log,
            # not as an exception. Keep it visible whenever there's nothing else
            # to show the user; collapse it away on a normal successful run.
            if pipeline_error:
                status.update(label="Failed", state="error", expanded=True)
            elif not rows:
                status.update(label="Finished — nothing found", state="error", expanded=True)
            else:
                status.update(label=f"Done — found {len(rows)} record(s)", state="complete", expanded=False)
                log_area.empty()

        if pipeline_error:
            st.error(pipeline_error)
            st.stop()

        if not rows:
            st.warning(
                "No leadership found for these organizations. Double-check spelling, "
                "or that they're covered by the DoW Directory / web search."
            )
        else:
            confidence_counts = {"High": 0, "Medium": 0, "Low": 0}
            for row in rows:
                if row.get("confidence") in confidence_counts:
                    confidence_counts[row["confidence"]] += 1

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Leaders found", len(rows))
            m2.metric("Organizations", len(orgs) or 1)
            m3.metric("High confidence", confidence_counts["High"])
            m4.metric("Low confidence", confidence_counts["Low"])

            st.dataframe(rows, use_container_width=True, column_order=CSV_FIELDS)

            dl1, dl2 = st.columns(2)
            dl1.download_button(
                "Download CSV",
                data=output_path.read_bytes(),
                file_name="org_leadership.csv",
                mime="text/csv",
                icon=":material/download:",
                use_container_width=True,
            )
            dl2.download_button(
                "Download Excel",
                data=rows_to_xlsx_bytes(rows),
                file_name="org_leadership.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                icon=":material/download:",
                type="primary",
                use_container_width=True,
            )
