"""
Streamlit UI: upload one JD + multiple resumes, run matcher, show summary table.
Run:  streamlit run streamlit_app.py
"""

from __future__ import annotations

import hashlib
import tempfile
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

_RESUME_MATCHER_ROOT = Path(__file__).resolve().parent
_OUTPUTS_DIR = _RESUME_MATCHER_ROOT / "outputs"

st.set_page_config(page_title="JD–Resume Matcher", layout="wide")
st.title("JD–Resume Matcher")

with st.sidebar:
    st.markdown("Set `DEEPSEEK_API_KEY` in `.env` at the repo root (optional: `DEEPSEEK_MODEL`, default `deepseek-chat`).")

jd_file = st.file_uploader(
    "Job description (.txt, .md, or .docx) — one file only",
    type=["txt", "md", "docx"],
    accept_multiple_files=False,
)
resume_files = st.file_uploader("Resumes (.pdf, .docx, .txt)", type=["pdf", "docx", "txt"], accept_multiple_files=True)

if st.button("Run match", type="primary", key="run_match"):
    if not jd_file or not resume_files:
        st.warning("Upload one job description and at least one resume.")
    else:
        llm = get_llm_client()
        summary_rows: list[dict] = []
        written_pdf: list[Path] = []
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)

            jd_path = tdir / jd_file.name
            jd_path.write_bytes(jd_file.getvalue())
            jd_text = load_jd(jd_path)
            with st.spinner("Extracting JD skills…"):
                jd_skills = extract_jd_skills(llm, jd_text)

            jd_label = Path(jd_file.name).stem
            orig_jd_name = jd_file.name

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
            _OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
            with st.spinner("Writing recruiter summary PDFs to outputs/…"):
                written_pdf = export_recruiter_summary_pdfs(
                    summary_rows,
                    resume_targets,
                    candidate_by_resume_key,
                    out_root=_OUTPUTS_DIR,
                )

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
        if written_pdf:
            st.subheader("Saved PDFs (with recruiter summary page)")
            for w in written_pdf:
                st.code(str(w), language=None)
