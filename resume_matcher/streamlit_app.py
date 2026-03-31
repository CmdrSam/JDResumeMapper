"""
Streamlit UI frontend: upload inputs, enqueue match jobs, poll status, and download artifacts.
Run: streamlit run streamlit_app.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from rq.job import Job

# Support both local package-style runs and Docker `/app` script runs.
if __package__ in (None, ""):
    here = Path(__file__).resolve().parent
    sys.path.append(str(here))
    sys.path.append(str(here.parent))

try:
    from resume_matcher.src.pipeline.queueing import get_match_queue, get_redis_connection
except ModuleNotFoundError:
    from src.pipeline.queueing import get_match_queue, get_redis_connection

_ROOT = Path(__file__).resolve().parent
_OUTPUTS_DIR = _ROOT / "outputs"
_SESSIONS_DIR = _OUTPUTS_DIR / "sessions"
_MAX_CONCURRENT_SUBMITS = 20
_RETENTION_HOURS = 24
_MAX_RUN_FOLDERS = 120
_RUN_SEMAPHORE = threading.BoundedSemaphore(_MAX_CONCURRENT_SUBMITS)
_MAX_AUTO_PDF_BYTES_PER_FILE = 12 * 1024 * 1024
_MAX_AUTO_PDF_BYTES_TOTAL = 28 * 1024 * 1024
_STATUS_AUTO_REFRESH_MS = 4000


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
    run_dirs = [p for p in _SESSIONS_DIR.glob("*/*") if p.is_dir()]
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


def _recruiter_summary_markdown(row: dict[str, Any]) -> str:
    rp_raw = row.get("recruiter_page")
    rp = rp_raw if isinstance(rp_raw, dict) else {}
    name = str(row.get("Candidate") or "Candidate").strip()
    summary = str(rp.get("candidate_summary") or "").strip()
    verdict_lines = rp.get("verdict_lines") or []
    if isinstance(verdict_lines, str):
        verdict_lines = [verdict_lines] if verdict_lines.strip() else []
    elif not isinstance(verdict_lines, list):
        verdict_lines = []
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
<html><body>
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


def _inject_status_autorefresh(ms: int = _STATUS_AUTO_REFRESH_MS) -> None:
    """Auto-refresh Streamlit page while a job is active."""
    html = f"""
<!DOCTYPE html>
<html><body>
<script>
setTimeout(() => {{
  window.parent.location.reload();
}}, {int(ms)});
</script>
</body></html>
"""
    components.html(html, height=0)


def _normalize_job_status(raw_status: Any) -> str:
    """Normalize RQ status to queued/started/finished/failed string."""
    if hasattr(raw_status, "value"):
        try:
            return str(raw_status.value).strip().lower()
        except Exception:
            pass
    s = str(raw_status or "").strip().lower()
    if "." in s:
        s = s.rsplit(".", 1)[-1]
    return s


def _render_last_run(last: dict[str, Any]) -> None:
    summary_rows = last.get("summary_rows", [])
    written_pdf = [Path(p) for p in last.get("written_pdf", [])]

    st.subheader("Results")
    display_rows = [
        {"Candidate Name": r.get("Candidate", ""), "Profile Score": r.get("Profile score", "")}
        for r in summary_rows
    ]
    st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True)

    st.info(
        "**For recruiters:** After each run, your browser should auto-save recruiter-ready PDFs. "
        "If auto-download fails, use **Download again** below."
    )

    pending = st.session_state.pop("_pending_pdf_autodownload", None)
    if pending:
        ad_paths = [Path(p) for p in pending if Path(p).is_file()]
        attempted, skipped = _inject_autodownload_pdfs(ad_paths) if ad_paths else (0, 0)
        if attempted:
            st.success(f"Triggered auto-download for {attempted} PDF(s).")
        if skipped:
            st.warning(f"{skipped} PDF(s) skipped for auto-download. Use buttons below.")

    if written_pdf:
        st.subheader("Recruiter-ready resumes (PDF)")
        n_pdf = len([w for w in written_pdf if w.is_file()])
        cols = st.columns(min(3, max(1, n_pdf)))
        col_i = 0
        for i, w in enumerate(written_pdf):
            if not w.is_file():
                continue
            cand = summary_rows[i].get("Candidate", w.stem) if i < len(summary_rows) else w.stem
            safe_label = "".join(c if c.isalnum() or c in " -_" else "_" for c in str(cand))[:40]
            with cols[col_i % len(cols)]:
                st.markdown(f"**{cand}**")
                st.download_button(
                    label=f"Download again - {w.name}",
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
        with st.expander(f"{cand} - highlights & verdict", expanded=False):
            st.markdown(_recruiter_summary_markdown(r))

    st.subheader("Other downloads")
    csv_path = Path(last["csv_path"])
    run_dir = Path(last["run_output_dir"])
    st.caption(f"Run folder on server: `{run_dir}`")
    if csv_path.is_file():
        st.download_button(
            label="Download summary CSV",
            data=csv_path.read_bytes(),
            file_name=csv_path.name,
            mime="text/csv",
            key="dl_csv",
        )


st.set_page_config(page_title="JD-Resume Matcher", layout="wide")
st.title("JD-Resume Matcher")
st.caption("Frontend-only mode: jobs are submitted to queue workers.")

jd_input_mode = st.radio("JD input mode", ["Upload JD file", "Paste JD text"], horizontal=True, index=0)
jd_file = None
jd_text_input = ""
if jd_input_mode == "Upload JD file":
    jd_file = st.file_uploader(
        "Job description (.txt, .md, or .docx) - one file only",
        type=["txt", "md", "docx"],
        accept_multiple_files=False,
    )
else:
    jd_text_input = st.text_area("Paste job description text", placeholder="Paste full JD here...", height=220)

resume_files = st.file_uploader("Resumes (.pdf, .docx, .txt)", type=["pdf", "docx", "txt"], accept_multiple_files=True)

run_clicked = st.button(
    "Run match",
    type="primary",
    key="run_match",
    disabled=bool(st.session_state.get("_run_in_progress", False))
    or bool(st.session_state.get("_active_job_id")),
)

if run_clicked:
    missing_jd = (jd_input_mode == "Upload JD file" and not jd_file) or (
        jd_input_mode == "Paste JD text" and not jd_text_input.strip()
    )
    if missing_jd or not resume_files:
        st.warning("Provide one JD (upload or paste) and at least one resume.")
    elif not _RUN_SEMAPHORE.acquire(blocking=False):
        st.warning(f"Server busy ({_MAX_CONCURRENT_SUBMITS} active submissions). Retry shortly.")
    else:
        st.session_state["_run_in_progress"] = True
        try:
            _cleanup_old_runs()
            run_output_dir = _new_run_output_dir()
            inputs_dir = run_output_dir / "inputs"
            inputs_dir.mkdir(parents=True, exist_ok=True)

            payload: dict[str, Any] = {"run_output_dir": str(run_output_dir)}
            if jd_input_mode == "Upload JD file":
                if jd_file is None:
                    raise ValueError("JD file is required in upload mode.")
                jd_dest = inputs_dir / jd_file.name
                jd_dest.write_bytes(jd_file.getvalue())
                payload["jd_mode"] = "upload"
                payload["jd_path"] = str(jd_dest.resolve())
            else:
                payload["jd_mode"] = "paste"
                payload["jd_text"] = jd_text_input.strip()

            resume_items: list[dict[str, str]] = []
            for i, up in enumerate(resume_files):
                content = up.getvalue()
                h = hashlib.sha256(content).hexdigest()
                rpath = inputs_dir / f"resume_{i}_{up.name}"
                rpath.write_bytes(content)
                resume_items.append({"path": str(rpath.resolve()), "display_name": up.name, "hash": h})
            payload["resume_items"] = resume_items

            q = get_match_queue()
            job = q.enqueue(
                "src.pipeline.job_runner.process_match_job",
                payload,
                job_timeout="60m",
                result_ttl=24 * 3600,
                failure_ttl=24 * 3600,
            )
            st.session_state["_active_job_id"] = job.id
            st.session_state["_active_run_dir"] = str(run_output_dir)
            st.success(f"Job submitted. Job ID: `{job.id}`")
        except Exception as e:
            st.error(f"Failed to submit job: {e}")
        finally:
            st.session_state["_run_in_progress"] = False
            _RUN_SEMAPHORE.release()

active_job_id = st.session_state.get("_active_job_id")
if active_job_id:
    st.subheader("Processing status")
    st.caption(f"Active Job ID: `{active_job_id}`")
    st.caption("Status auto-refresh is on (about every 4 seconds).")
    st.button("Refresh status now", key="refresh_status")
    try:
        conn = get_redis_connection()
        job = Job.fetch(active_job_id, connection=conn)
        status = _normalize_job_status(job.get_status(refresh=True))
        if status == "queued":
            st.info("Queued: waiting for a free worker.")
            _inject_status_autorefresh()
        elif status == "started":
            hb = job.last_heartbeat
            hb_txt = hb.isoformat() if hb else "n/a"
            st.info(f"Processing: worker picked this job. Last heartbeat: {hb_txt}")
            _inject_status_autorefresh()
        elif status == "finished":
            raw_result = job.result
            result = raw_result if isinstance(raw_result, dict) else {}
            err_count = int(result.get("error_count", 0) or 0)
            run_output_dir_raw = result.get("run_output_dir") or st.session_state.get("_active_run_dir")
            if not run_output_dir_raw:
                st.error("Job finished but run output directory was not found in session/result.")
                st.session_state.pop("_active_job_id", None)
                st.session_state.pop("_active_run_dir", None)
                st.stop()

            run_output_dir = Path(str(run_output_dir_raw))
            summary_path = Path(str(result.get("json_path") or (run_output_dir / "pipeline_summary.json")))
            csv_path = Path(str(result.get("csv_path") or (run_output_dir / "candidate_vs_jd_summary.csv")))
            written_pdf = [Path(p) for p in (result.get("written_pdf") or [])]
            if not written_pdf:
                written_pdf = sorted(run_output_dir.glob("*_recruiter_summary.pdf"))
            summary_rows: list[dict[str, Any]] = []
            if summary_path.is_file():
                try:
                    summary_rows = json.loads(summary_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    summary_rows = []
            st.session_state["_last_run"] = {
                "summary_rows": summary_rows,
                "written_pdf": [str(p) for p in written_pdf],
                "csv_path": str(csv_path),
                "run_output_dir": str(run_output_dir),
            }
            st.session_state["_pending_pdf_autodownload"] = [str(p) for p in written_pdf if p.is_file()]
            st.session_state.pop("_active_job_id", None)
            st.session_state.pop("_active_run_dir", None)
            if err_count > 0:
                st.warning(f"Processing complete with {err_count} resume-level error(s).")
            else:
                st.success("Processing complete.")
        elif status == "failed":
            st.error("Failed: worker reported an error.")
            if job.exc_info:
                st.code(str(job.exc_info))
            st.session_state.pop("_active_job_id", None)
            st.session_state.pop("_active_run_dir", None)
        else:
            st.info(f"Job status: {status or 'unknown'}")
            _inject_status_autorefresh()
    except Exception as e:
        st.error(f"Unable to fetch job status: {e}")

last = st.session_state.get("_last_run")
if last:
    _render_last_run(last)

