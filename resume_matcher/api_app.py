from __future__ import annotations

import hashlib
import json
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from rq.job import Job

from src.pipeline.queueing import get_job_meta, get_match_queue, get_redis_connection, set_job_meta

APP_ROOT = Path(__file__).resolve().parent
OUTPUTS_DIR = APP_ROOT / "outputs"
SESSIONS_DIR = OUTPUTS_DIR / "sessions"

app = FastAPI(title="Resume Matcher API", version="1.0.0")


def _new_run_output_dir() -> Path:
    sid = uuid.uuid4().hex[:12]
    run_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"run_{run_tag}_{uuid.uuid4().hex[:8]}"
    out = SESSIONS_DIR / sid / run_id
    out.mkdir(parents=True, exist_ok=True)
    return out


def _normalize_status(raw_status: Any) -> str:
    if hasattr(raw_status, "value"):
        try:
            return str(raw_status.value).strip().lower()
        except Exception:
            pass
    s = str(raw_status or "").strip().lower()
    if "." in s:
        s = s.rsplit(".", 1)[-1]
    return s


def _resolve_run_dir(job: Job) -> Path | None:
    result = job.result if isinstance(job.result, dict) else {}
    rd = result.get("run_output_dir")
    if rd:
        return Path(str(rd))
    meta = get_job_meta(job.id)
    rd_meta = meta.get("run_output_dir")
    return Path(rd_meta) if rd_meta else None


async def _build_payload_from_request(
    *,
    jd_mode: str,
    jd_text: str,
    jd_file: UploadFile | None,
    resumes: list[UploadFile],
) -> tuple[dict[str, Any], Path]:
    mode = (jd_mode or "").strip().lower()
    if mode not in {"upload", "paste"}:
        raise HTTPException(status_code=400, detail="jd_mode must be 'upload' or 'paste'")
    if mode == "upload" and jd_file is None:
        raise HTTPException(status_code=400, detail="jd_file is required when jd_mode=upload")
    if mode == "paste" and not jd_text.strip():
        raise HTTPException(status_code=400, detail="jd_text is required when jd_mode=paste")
    if not resumes:
        raise HTTPException(status_code=400, detail="At least one resume is required")

    run_output_dir = _new_run_output_dir()
    inputs_dir = run_output_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"run_output_dir": str(run_output_dir)}

    if mode == "upload":
        assert jd_file is not None
        jd_bytes = await jd_file.read()
        jd_dest = inputs_dir / jd_file.filename
        jd_dest.write_bytes(jd_bytes)
        payload["jd_mode"] = "upload"
        payload["jd_path"] = str(jd_dest.resolve())
    else:
        payload["jd_mode"] = "paste"
        payload["jd_text"] = jd_text.strip()

    resume_items: list[dict[str, str]] = []
    for i, up in enumerate(resumes):
        content = await up.read()
        h = hashlib.sha256(content).hexdigest()
        safe_name = up.filename or f"resume_{i}.pdf"
        rpath = inputs_dir / f"resume_{i}_{safe_name}"
        rpath.write_bytes(content)
        resume_items.append(
            {
                "path": str(rpath.resolve()),
                "display_name": safe_name,
                "hash": h,
            }
        )
    payload["resume_items"] = resume_items
    return payload, run_output_dir


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/jobs")
async def create_job(
    jd_mode: str = Form(...),
    jd_text: str = Form(""),
    jd_file: UploadFile | None = File(default=None),
    resumes: list[UploadFile] = File(...),
) -> dict[str, Any]:
    payload, run_output_dir = await _build_payload_from_request(
        jd_mode=jd_mode,
        jd_text=jd_text,
        jd_file=jd_file,
        resumes=resumes,
    )

    q = get_match_queue()
    job = q.enqueue(
        "src.pipeline.job_runner.process_match_job",
        payload,
        job_timeout="60m",
        result_ttl=24 * 3600,
        failure_ttl=24 * 3600,
    )
    set_job_meta(job.id, {"run_output_dir": str(run_output_dir)})
    return {"job_id": job.id, "status": "queued", "run_output_dir": str(run_output_dir)}


@app.post("/match-resumes")
async def match_resumes_and_return_zip(
    jd_mode: str = Form(...),
    jd_text: str = Form(""),
    jd_file: UploadFile | None = File(default=None),
    resumes: list[UploadFile] = File(...),
    wait_timeout_seconds: int = Form(600),
    poll_interval_seconds: float = Form(2.0),
) -> FileResponse | JSONResponse:
    """
    Team-shareable single-call endpoint:
    - accepts JD + resumes
    - waits for completion (up to wait_timeout_seconds)
    - returns a ZIP of edited recruiter PDFs
    If not finished in time, returns 202 with job_id so caller can poll async endpoints.
    """
    if wait_timeout_seconds < 30 or wait_timeout_seconds > 3600:
        raise HTTPException(status_code=400, detail="wait_timeout_seconds must be between 30 and 3600")
    if poll_interval_seconds <= 0 or poll_interval_seconds > 30:
        raise HTTPException(status_code=400, detail="poll_interval_seconds must be > 0 and <= 30")

    payload, run_output_dir = await _build_payload_from_request(
        jd_mode=jd_mode,
        jd_text=jd_text,
        jd_file=jd_file,
        resumes=resumes,
    )
    q = get_match_queue()
    job = q.enqueue(
        "src.pipeline.job_runner.process_match_job",
        payload,
        job_timeout="60m",
        result_ttl=24 * 3600,
        failure_ttl=24 * 3600,
    )
    set_job_meta(job.id, {"run_output_dir": str(run_output_dir)})

    conn = get_redis_connection()
    deadline = time.time() + wait_timeout_seconds
    while time.time() < deadline:
        fresh = Job.fetch(job.id, connection=conn)
        status = _normalize_status(fresh.get_status(refresh=True))
        if status == "finished":
            result = fresh.result if isinstance(fresh.result, dict) else {}
            run_dir = Path(str(result.get("run_output_dir") or run_output_dir))
            pdfs = sorted(run_dir.glob("*_recruiter_summary.pdf"))
            if not pdfs:
                raise HTTPException(status_code=500, detail="Job finished but no recruiter PDFs were generated.")
            zip_path = run_dir / "edited_resumes.zip"
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for p in pdfs:
                    zf.write(p, arcname=p.name)
            return FileResponse(str(zip_path), filename=zip_path.name, media_type="application/zip")
        if status == "failed":
            msg = str(fresh.exc_info or "Worker failed")
            raise HTTPException(status_code=500, detail=f"Job failed: {msg}")
        time.sleep(poll_interval_seconds)

    return JSONResponse(
        status_code=202,
        content={
            "job_id": job.id,
            "status": "queued_or_started",
            "message": "Still processing. Poll /jobs/{job_id} and then download files via /jobs/{job_id}/files/{filename}.",
        },
    )


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    conn = get_redis_connection()
    try:
        job = Job.fetch(job_id, connection=conn)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Job not found: {e}")

    status = _normalize_status(job.get_status(refresh=True))
    out: dict[str, Any] = {"job_id": job_id, "status": status}
    if job.last_heartbeat:
        out["last_heartbeat"] = job.last_heartbeat.isoformat()
    if status == "failed":
        out["error"] = str(job.exc_info or "Worker failed")
    if status == "finished":
        result = job.result if isinstance(job.result, dict) else {}
        out["result"] = {
            "count": int(result.get("count", 0) or 0),
            "error_count": int(result.get("error_count", 0) or 0),
        }
    return out


@app.get("/jobs/{job_id}/results")
def get_job_results(job_id: str) -> dict[str, Any]:
    conn = get_redis_connection()
    try:
        job = Job.fetch(job_id, connection=conn)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Job not found: {e}")

    status = _normalize_status(job.get_status(refresh=True))
    if status != "finished":
        raise HTTPException(status_code=409, detail=f"Job not finished. Current status: {status}")

    run_dir = _resolve_run_dir(job)
    if not run_dir:
        raise HTTPException(status_code=500, detail="Run output directory not found")

    summary_path = run_dir / "pipeline_summary.json"
    csv_path = run_dir / "candidate_vs_jd_summary.csv"
    rows: list[dict[str, Any]] = []
    if summary_path.is_file():
        try:
            rows = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            rows = []
    pdfs = sorted(run_dir.glob("*_recruiter_summary.pdf"))
    return {
        "job_id": job_id,
        "run_output_dir": str(run_dir),
        "summary_rows": rows,
        "files": {
            "csv": csv_path.name if csv_path.is_file() else None,
            "summary_json": summary_path.name if summary_path.is_file() else None,
            "pdfs": [p.name for p in pdfs],
        },
    }


@app.get("/jobs/{job_id}/files/{filename}")
def download_job_file(job_id: str, filename: str) -> FileResponse:
    conn = get_redis_connection()
    try:
        job = Job.fetch(job_id, connection=conn)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Job not found: {e}")
    run_dir = _resolve_run_dir(job)
    if not run_dir:
        raise HTTPException(status_code=404, detail="Run output directory not found")

    target = (run_dir / filename).resolve()
    if run_dir.resolve() not in target.parents and target != run_dir.resolve():
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(target), filename=target.name)

