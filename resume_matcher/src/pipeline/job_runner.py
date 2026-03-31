from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.extractor.jd_extractor import extract_jd_skills
from src.extractor.resume_extractor import extract_resume_with_llm
from src.llm.client import get_llm_client
from src.matcher.match_engine import build_candidate_jd_summary_row, match_candidate_to_jd
from src.parser.jd_parser import load_jd
from src.parser.resume_parser import extract_text_from_resume
from src.resume_enriched.publish import export_recruiter_summary_pdfs
from src.utils.table import write_candidate_jd_summary


def _error_summary_row(
    *,
    jd_label: str,
    jd_file_name: str,
    resume_file_name: str,
    reason: str,
) -> dict[str, Any]:
    short_reason = (reason or "Unknown error").strip()[:500]
    return {
        "JD": jd_label,
        "Candidate": Path(resume_file_name).stem,
        "JD file": jd_file_name,
        "Resume file": resume_file_name,
        "Required skills": "",
        "Candidate skills": "",
        "Resume score": 0,
        "Profile score": 0,
        "Why select": "",
        "Why not select": f"Processing failed for this resume: {short_reason}",
        "recruiter_page": {
            "candidate_summary": "Resume processing failed for this candidate. See failure reason in verdict.",
            "verdict_lines": [f"Processing failed: {short_reason}"],
            "overall_match_out_of_5": 0.0,
            "overall_match_percent_approx": 0,
            "dimension_rows": [],
        },
        "skill_matrix": [],
        "processing_error": short_reason,
    }


def process_match_job(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Background worker job:
    - parse JD (file or pasted text)
    - parse resumes
    - run matching
    - write CSV/JSON/PDF outputs
    """
    run_output_dir = Path(str(payload["run_output_dir"])).resolve()
    run_output_dir.mkdir(parents=True, exist_ok=True)

    jd_mode = str(payload.get("jd_mode", "upload"))
    jd_text = ""
    jd_label = "Pasted JD"
    jd_file_name = "pasted_jd.txt"
    if jd_mode == "upload":
        jd_path = Path(str(payload["jd_path"])).resolve()
        jd_text = load_jd(jd_path)
        jd_label = jd_path.stem
        jd_file_name = jd_path.name
    else:
        jd_text = str(payload.get("jd_text", "") or "").strip()
        if not jd_text:
            raise ValueError("Pasted JD text is empty.")

    resume_items = payload.get("resume_items") or []
    if not isinstance(resume_items, list) or not resume_items:
        raise ValueError("No resume files were provided.")

    llm = get_llm_client()
    jd_skills = extract_jd_skills(llm, jd_text)

    content_hash_to_candidate: dict[str, dict[str, Any]] = {}
    failed_hash_to_reason: dict[str, str] = {}
    resume_order: list[tuple[Path, str, str]] = []
    for item in resume_items:
        if not isinstance(item, dict):
            continue
        rpath = Path(str(item["path"])).resolve()
        display_name = str(item.get("display_name") or rpath.name)
        h = str(item.get("hash") or "")
        if not h:
            h = f"nohash::{display_name}"
        resume_order.append((rpath, display_name, h))
        if h not in content_hash_to_candidate and h not in failed_hash_to_reason:
            try:
                text = extract_text_from_resume(rpath)
                content_hash_to_candidate[h] = extract_resume_with_llm(llm, text)
            except Exception as e:
                failed_hash_to_reason[h] = f"{type(e).__name__}: {e}"

    summary_rows: list[dict[str, Any]] = []
    for rpath, display_name, h in resume_order:
        if h in failed_hash_to_reason:
            summary_rows.append(
                _error_summary_row(
                    jd_label=jd_label,
                    jd_file_name=jd_file_name,
                    resume_file_name=display_name,
                    reason=failed_hash_to_reason[h],
                )
            )
            continue
        try:
            candidate = content_hash_to_candidate[h]
            match_rows = match_candidate_to_jd(llm, candidate, jd_text, jd_skills)
            row = build_candidate_jd_summary_row(
                llm,
                jd_label=jd_label,
                jd_file_name=jd_file_name,
                jd_text=jd_text,
                jd_skills=jd_skills,
                resume_file_name=display_name,
                candidate=candidate,
                match_rows=match_rows,
            )
            summary_rows.append(row)
        except Exception as e:
            summary_rows.append(
                _error_summary_row(
                    jd_label=jd_label,
                    jd_file_name=jd_file_name,
                    resume_file_name=display_name,
                    reason=f"{type(e).__name__}: {e}",
                )
            )

    candidate_by_resume_key = {
        str(t[0].resolve()): content_hash_to_candidate[t[2]]
        for t in resume_order
        if t[2] in content_hash_to_candidate
    }
    resume_targets = [(t[0].resolve(), t[1]) for t in resume_order if t[2] in content_hash_to_candidate]
    written_pdf = export_recruiter_summary_pdfs(
        summary_rows,
        resume_targets,
        candidate_by_resume_key,
        out_root=run_output_dir,
    )

    write_candidate_jd_summary(summary_rows, run_output_dir)
    csv_path = run_output_dir / "candidate_vs_jd_summary.csv"
    summary_path = run_output_dir / "pipeline_summary.json"
    summary_path.write_text(
        json.dumps(summary_rows, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    return {
        "run_output_dir": str(run_output_dir),
        "csv_path": str(csv_path),
        "json_path": str(summary_path),
        "written_pdf": [str(p) for p in written_pdf],
        "count": len(summary_rows),
        "error_count": len([r for r in summary_rows if r.get("processing_error")]),
    }

