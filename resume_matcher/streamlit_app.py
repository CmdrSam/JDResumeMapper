"""
Streamlit UI: upload one JD + multiple resumes, run matcher, show summary table.
Run:  streamlit run streamlit_app.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

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
# Auto-download embeds PDFs as base64 in the page; keep limits to avoid huge HTML / browser issues.
_MAX_AUTO_PDF_BYTES_PER_FILE = 12 * 1024 * 1024
_MAX_AUTO_PDF_BYTES_TOTAL = 28 * 1024 * 1024


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


def _recruiter_summary_markdown(row: dict) -> str:
    """Readable markdown for UI + optional .md download."""
    raw_rp = row.get("recruiter_page")
    rp: dict[str, Any] = raw_rp if isinstance(raw_rp, dict) else {}
    name = str(row.get("Candidate") or "Candidate").strip()
    summary = str(rp.get("candidate_summary") or "").strip()
    verdict_lines = rp.get("verdict_lines") or []
    if isinstance(verdict_lines, str):
        verdict_lines = [verdict_lines] if verdict_lines.strip() else []
    parts = [
        f"## {name}",
        "",
        "### Candidate highlight (per current job description)",
        "",
        summary or "_No summary generated._",
        "",
        "### Verdict",
        "",
    ]
    if verdict_lines:
        for line in verdict_lines:
            parts.append(f"- {line}")
    else:
        parts.append("_No verdict lines._")
    return "\n".join(parts)


def _inject_autodownload_pdfs(paths: list[Path]) -> tuple[int, int]:
    """
    Ask the browser to save each PDF (data-URL + programmatic click).
    Returns (attempted_count, skipped_due_to_limits_count).
    """
    items: list[dict[str, str]] = []
    total = 0
    skipped = 0
    for idx, p in enumerate(paths):
        if not p.is_file():
            skipped += 1
            continue
        try:
            raw = p.read_bytes()
        except OSError:
            skipped += 1
            continue
        if len(raw) > _MAX_AUTO_PDF_BYTES_PER_FILE:
            skipped += 1
            continue
        if total + len(raw) > _MAX_AUTO_PDF_BYTES_TOTAL:
            skipped += len(paths) - idx
            break
        total += len(raw)
        items.append({"name": p.name, "b64": base64.b64encode(raw).decode("ascii")})
    if not items:
        return 0, skipped
    payload = json.dumps(items)
    html = f"""
<!DOCTYPE html>
<html><head><meta charset="utf-8"></head><body>
<script>
const items = {payload};
items.forEach((item, i) => {{
  setTimeout(() => {{
    const a = document.createElement("a");
    a.href = "data:application/pdf;base64," + item.b64;
    a.download = item.name;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }}, 300 + i * 700);
}});
</script>
</body></html>
"""
    components.html(html, height=0)
    return len(items), skipped


st.set_page_config(page_title="JD–Resume Matcher", layout="wide")
st.title("JD–Resume Matcher")

jd_input_mode = st.radio(
    "JD input mode",
    options=["Upload JD file", "Paste JD text"],
    horizontal=True,
    index=0,
)
jd_file = None
jd_text_input = ""
if jd_input_mode == "Upload JD file":
    jd_file = st.file_uploader(
        "Job description (.txt, .md, or .docx) — one file only",
        type=["txt", "md", "docx"],
        accept_multiple_files=False,
    )
else:
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
                    "run_output_dir": str(run_output_dir),
                    "df_rows": int(df.shape[0]),
                }
                st.session_state["_pending_pdf_autodownload"] = [
                    str(p) for p in written_pdf if p.is_file()
                ]
            finally:
                st.session_state["_run_in_progress"] = False
                _RUN_SEMAPHORE.release()

last = st.session_state.get("_last_run")
if last:
    summary_rows = last.get("summary_rows", [])
    written_pdf = [Path(p) for p in last.get("written_pdf", [])]

    st.subheader("Results")
    display_rows = [
        {
            "Candidate Name": r.get("Candidate", ""),
            "Profile Score": r.get("Profile score", ""),
        }
        for r in summary_rows
    ]
    st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True)

    st.info(
        "**For recruiters:** After each run, your browser should **save the recruiter-ready PDFs automatically** "
        "(one file per candidate). Each PDF is the original resume with a **summary page prepended** — skill table, "
        "verdict, overall match score, and role context. "
        "If downloads do not start (popup blocker, size limits, or browser policy), use **Download again** below."
    )

    pending = st.session_state.pop("_pending_pdf_autodownload", None)
    if pending:
        ad_paths = [Path(p) for p in pending if Path(p).is_file()]
        if ad_paths:
            attempted, skipped_limits = _inject_autodownload_pdfs(ad_paths)
            if attempted:
                st.success(
                    f"Triggered automatic download for **{attempted}** PDF(s). "
                    "Check your downloads folder; allow multiple downloads if the browser asks."
                )
            if skipped_limits:
                st.warning(
                    f"**{skipped_limits}** PDF(s) were not auto-downloaded (size or read limits). "
                    "Use the buttons below to download them manually."
                )
            if not attempted and not skipped_limits:
                st.warning("Could not auto-download PDFs. Use the buttons below.")

    if written_pdf:
        st.subheader("Recruiter-ready resumes (PDF)")
        st.caption(
            "Download again if automatic save did not work — use these PDFs for screening, sharing, or your ATS."
        )
        n_pdf = len([w for w in written_pdf if w.is_file()])
        cols = st.columns(min(3, max(1, n_pdf)))
        col_i = 0
        for i, w in enumerate(written_pdf):
            if not w.is_file():
                continue
            cand = (
                summary_rows[i].get("Candidate", w.stem)
                if i < len(summary_rows)
                else w.stem
            )
            safe_label = "".join(c if c.isalnum() or c in " -_" else "_" for c in str(cand))[:40]
            with cols[col_i % len(cols)]:
                st.markdown(f"**{cand}**")
                st.download_button(
                    label=f"Download again — {w.name}",
                    data=w.read_bytes(),
                    file_name=w.name,
                    mime="application/pdf",
                    type="primary",
                    key=f"dl_pdf_{i}_{safe_label}",
                    use_container_width=True,
                )
            col_i += 1

    st.subheader("Written summary (per candidate)")
    for idx, r in enumerate(summary_rows):
        cand = str(r.get("Candidate", f"Candidate {idx + 1}"))
        md_body = _recruiter_summary_markdown(r)
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in cand)[:48]
        with st.expander(f"{cand} — highlights & verdict", expanded=False):
            st.markdown(md_body)

    st.subheader("Other downloads")
    csv_path = Path(last["csv_path"])
    run_dir = Path(last["run_output_dir"])

    if csv_path.is_file():
        st.download_button(
            label="Download summary CSV",
            data=csv_path.read_bytes(),
            file_name=csv_path.name,
            mime="text/csv",
            key="dl_csv",
        )
