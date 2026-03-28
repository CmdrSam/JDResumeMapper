"""
Streamlit UI: upload resumes + JD, run matcher, show tables.
Run:  streamlit run streamlit_app.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from src.extractor.jd_extractor import extract_jd_skills
from src.extractor.resume_extractor import extract_resume_with_llm
from src.llm.client import get_llm_client
from src.matcher.match_engine import composite_score, match_candidate_to_jd
from src.parser.jd_parser import load_jd
from src.parser.resume_parser import extract_text_from_resume

st.set_page_config(page_title="JD–Resume Matcher", layout="wide")
st.title("JD–Resume Matcher")

with st.sidebar:
    st.markdown("Set `DEEPSEEK_API_KEY` in `.env` at the repo root (optional: `DEEPSEEK_MODEL`, default `deepseek-chat`).")

jd_file = st.file_uploader("Job description (.txt or .docx)", type=["txt", "md", "docx"])
resume_files = st.file_uploader("Resumes (.pdf, .docx, .txt)", type=["pdf", "docx", "txt"], accept_multiple_files=True)

if st.button("Run match", type="primary", key="run_match"):
    if not jd_file or not resume_files:
        st.warning("Upload a JD and at least one resume.")
    else:
        llm = get_llm_client()
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            jd_path = tdir / jd_file.name
            jd_path.write_bytes(jd_file.getvalue())
            jd_text = load_jd(jd_path)
            with st.spinner("Extracting JD skills…"):
                jd_skills = extract_jd_skills(llm, jd_text)
            st.subheader("JD skills (structured)")
            st.json(jd_skills)

            for up in resume_files:
                rpath = tdir / up.name
                rpath.write_bytes(up.getvalue())
                with st.spinner(f"Processing {up.name}…"):
                    text = extract_text_from_resume(rpath)
                    candidate = extract_resume_with_llm(llm, text)
                    rows = match_candidate_to_jd(llm, candidate, jd_text, jd_skills)
                st.subheader(candidate.get("name") or up.name)
                scores = composite_score(rows)
                st.caption(f"Composite score (weighted blend): **{scores['final']}**")
                st.dataframe(pd.DataFrame(rows), use_container_width=True)
