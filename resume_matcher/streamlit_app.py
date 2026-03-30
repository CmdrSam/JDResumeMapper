"""
Streamlit UI: upload one JD + multiple resumes, run matcher, show summary table.
Run:  streamlit run streamlit_app.py
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from src.extractor.jd_extractor import extract_jd_skills
from src.extractor.resume_extractor import extract_resume_with_llm
from src.llm.client import get_llm_client
from src.matcher.match_engine import build_candidate_jd_summary_row, match_candidate_to_jd
from src.parser.jd_parser import load_jd
from src.parser.resume_parser import extract_text_from_resume
from src.resume_enriched.publish import export_recruiter_summary_pdfs
from src.utils.table import write_candidate_jd_summary

_RESUME_MATCHER_ROOT = Path(__file__).resolve().parent
_OUTPUTS_DIR = _RESUME_MATCHER_ROOT / "outputs"
_SESSIONS_DIR = _OUTPUTS_DIR / "sessions"
_MAX_CONCURRENT_RUNS = 4
_RETENTION_HOURS = 24
_MAX_RUN_FOLDERS = 120
_RUN_SEMAPHORE = threading.BoundedSemaphore(_MAX_CONCURRENT_RUNS)


def _get_session_id() -> str:
    sid = st.session_state.get("_session_id")
    if not sid:
        sid = uuid.uuid4().hex[:12]
        st.session_state["_session_id"] = sid
    return sid


def _safe_rmtree(path: Path) -> None:
    if not path.exists():
        return
    for p in sorted(path.rglob("*"), reverse=True):
        try:
            if p.is_file():
                p.unlink(missing_ok=True)
            elif p.is_dir():
                p.rmdir()
        except OSError:
            continue
    try:
        path.rmdir()
    except OSError:
        pass


def _cleanup_old_runs() -> None:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=_RETENTION_HOURS)
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    run_dirs: list[Path] = [p for p in _SESSIONS_DIR.glob("*/*") if p.is_dir()]

    for rdir in run_dirs:
        try:
            mtime = datetime.fromtimestamp(rdir.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            _safe_rmtree(rdir)

    run_dirs = [p for p in _SESSIONS_DIR.glob("*/*") if p.is_dir()]
    if len(run_dirs) > _MAX_RUN_FOLDERS:
        run_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for old in run_dirs[_MAX_RUN_FOLDERS:]:
            _safe_rmtree(old)

    for sdir in _SESSIONS_DIR.glob("*"):
        if sdir.is_dir() and not any(sdir.iterdir()):
            try:
                sdir.rmdir()
            except OSError:
                pass


def _new_run_output_dir() -> Path:
    sid = _get_session_id()
    run_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"run_{run_tag}_{uuid.uuid4().hex[:8]}"
    out = _SESSIONS_DIR / sid / run_id
    out.mkdir(parents=True, exist_ok=True)
    return out

st.set_page_config(page_title="JD–Resume Matcher", layout="wide")
st.title("JD–Resume Matcher")

with st.sidebar:
    st.markdown("Set `DEEPSEEK_API_KEY` in `.env` at the repo root (optional: `DEEPSEEK_MODEL`, default `deepseek-chat`).")
    st.caption(f"Concurrency guard: max {_MAX_CONCURRENT_RUNS} active run(s)")
    st.caption(f"Auto-cleanup: run outputs older than {_RETENTION_HOURS}h")

jd_file = st.file_uploader(
    "Job description (.txt, .md, or .docx) — one file only",
    type=["txt", "md", "docx"],
    accept_multiple_files=False,
)
jd_input_mode = st.radio(
    "JD input mode",
    options=["Upload JD file", "Paste JD text"],
    horizontal=True,
    index=0,
)
jd_text_input = ""
if jd_input_mode == "Paste JD text":
    jd_text_input = st.text_area(
        "Paste job description text",
        placeholder="Paste the full job description here...",
        height=220,
    )
resume_files = st.file_uploader("Resumes (.pdf, .docx, .txt)", type=["pdf", "docx", "txt"], accept_multiple_files=True)

run_clicked = st.button(
    "Run match",
    type="primary",
    key="run_match",
    disabled=bool(st.session_state.get("_run_in_progress", False)),
)
if run_clicked:
    missing_jd = (jd_input_mode == "Upload JD file" and not jd_file) or (
        jd_input_mode == "Paste JD text" and not jd_text_input.strip()
    )
    if missing_jd or not resume_files:
        st.warning("Provide one JD (upload or paste) and at least one resume.")
    else:
        if not _RUN_SEMAPHORE.acquire(blocking=False):
            st.warning(
                f"Server is busy ({_MAX_CONCURRENT_RUNS} active runs). "
                "Please retry in a minute."
            )
        else:
            st.session_state["_run_in_progress"] = True
            try:
                _cleanup_old_runs()
                run_output_dir = _new_run_output_dir()
                llm = get_llm_client()
                summary_rows: list[dict] = []
                written_pdf: list[Path] = []
                with tempfile.TemporaryDirectory() as tmp:
                    tdir = Path(tmp)

                    if jd_input_mode == "Upload JD file":
                        jd_path = tdir / jd_file.name
                        jd_path.write_bytes(jd_file.getvalue())
                        jd_text = load_jd(jd_path)
                        jd_label = Path(jd_file.name).stem
                        orig_jd_name = jd_file.name
                    else:
                        jd_text = jd_text_input.strip()
                        jd_label = "Pasted JD"
                        orig_jd_name = "pasted_jd.txt"

                    with st.spinner("Extracting JD skills…"):
                        jd_skills = extract_jd_skills(llm, jd_text)

                    content_hash_to_candidate: dict[str, dict] = {}
                    resume_order: list[tuple[Path, str, str]] = []
                    for i, up in enumerate(resume_files):
                        content = up.getvalue()
                        h = hashlib.sha256(content).hexdigest()
                        rpath = tdir / f"resume_{i}_{up.name}"
                        rpath.write_bytes(content)
                        resume_order.append((rpath, up.name, h))
                        if h not in content_hash_to_candidate:
                            text = extract_text_from_resume(rpath)
                            with st.spinner(f"Parsing resume: {up.name}…"):
                                content_hash_to_candidate[h] = extract_resume_with_llm(llm, text)

                    for rpath, display_name, h in resume_order:
                        candidate = content_hash_to_candidate[h]
                        with st.spinner(f"{orig_jd_name} × {display_name}…"):
                            match_rows = match_candidate_to_jd(llm, candidate, jd_text, jd_skills)
                            row = build_candidate_jd_summary_row(
                                llm,
                                jd_label=jd_label,
                                jd_file_name=orig_jd_name,
                                jd_text=jd_text,
                                jd_skills=jd_skills,
                                resume_file_name=display_name,
                                candidate=candidate,
                                match_rows=match_rows,
                            )
                            summary_rows.append(row)

                    candidate_by_resume_key = {
                        str(t[0].resolve()): content_hash_to_candidate[t[2]] for t in resume_order
                    }
                    resume_targets = [(t[0].resolve(), t[1]) for t in resume_order]
                    with st.spinner("Writing recruiter summary PDFs…"):
                        written_pdf = export_recruiter_summary_pdfs(
                            summary_rows,
                            resume_targets,
                            candidate_by_resume_key,
                            out_root=run_output_dir,
                        )

                df = write_candidate_jd_summary(summary_rows, run_output_dir)
                csv_path = run_output_dir / "candidate_vs_jd_summary.csv"
                summary_path = run_output_dir / "pipeline_summary.json"
                summary_path.write_text(
                    json.dumps(summary_rows, indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )

                st.session_state["_last_run"] = {
                    "summary_rows": summary_rows,
                    "written_pdf": [str(p) for p in written_pdf],
                    "csv_path": str(csv_path),
                    "json_path": str(summary_path),
                    "run_output_dir": str(run_output_dir),
                    "df_rows": int(df.shape[0]),
                }
            finally:
                st.session_state["_run_in_progress"] = False
                _RUN_SEMAPHORE.release()

last = st.session_state.get("_last_run")
if last:
    summary_rows = last.get("summary_rows", [])
    written_pdf = [Path(p) for p in last.get("written_pdf", [])]

    st.subheader("Results (one row per candidate vs this job description)")
    _skip_cols = ("skill_matrix", "recruiter_page")
    display = [{k: v for k, v in r.items() if k not in _skip_cols} for r in summary_rows]
    st.dataframe(pd.DataFrame(display), use_container_width=True)

    with st.expander("Recruiter summary page (JSON per candidate)"):
        for r in summary_rows:
            st.markdown(f"**{r.get('Candidate', '')}** — {r.get('Resume file', '')}")
            st.json(r.get("recruiter_page") or {})
    with st.expander("Dimension table rows (JSON per candidate)"):
        for r in summary_rows:
            st.markdown(f"**{r.get('Candidate', '')}** — {r.get('Resume file', '')}")
            st.json(r.get("skill_matrix", []))

    st.subheader("Downloads")
    csv_path = Path(last["csv_path"])
    json_path = Path(last["json_path"])
    run_dir = Path(last["run_output_dir"])
    st.caption(f"Session run folder: `{run_dir}`")

    if csv_path.is_file():
        st.download_button(
            label="Download summary CSV",
            data=csv_path.read_bytes(),
            file_name=csv_path.name,
            mime="text/csv",
            key="dl_csv",
        )
    if json_path.is_file():
        st.download_button(
            label="Download summary JSON",
            data=json_path.read_bytes(),
            file_name=json_path.name,
            mime="application/json",
            key="dl_json",
        )
    if written_pdf:
        st.markdown("**Recruiter PDFs**")
        for i, w in enumerate(written_pdf):
            if not w.is_file():
                continue
            st.download_button(
                label=f"Download {w.name}",
                data=w.read_bytes(),
                file_name=w.name,
                mime="application/pdf",
                key=f"dl_pdf_{i}",
            )
