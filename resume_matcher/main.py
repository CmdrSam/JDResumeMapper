"""
CLI entry: match resumes in data/resumes against JDs in data/jds (or paths you pass).
Run from this directory:  python main.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.extractor.jd_extractor import extract_jd_skills_bulk
from src.llm.client import get_llm_client
from src.matcher.match_engine import build_candidate_jd_summary_row, match_candidate_to_jd
from src.parser.jd_parser import load_jd
from src.pipeline.resume_cache import build_resume_candidate_map
from src.resume_enriched.publish import export_recruiter_summary_pdfs
from src.utils.table import write_candidate_jd_summary


def _default_globs(folder: Path, patterns: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for pat in patterns:
        out.extend(sorted(folder.glob(pat)))
    return out


def run_pipeline(
    resume_files: list[str | Path],
    jd_files: list[str | Path],
    llm,
    output_dir: str | Path | None = None,
) -> tuple[list[dict], dict[str, dict]]:
    """Returns (summary rows per candidate vs the single JD, candidate map keyed by ``str(resume_path.resolve())``)."""
    output_dir = output_dir or Path(__file__).resolve().parent / "outputs"
    summary_rows: list[dict] = []

    jd_paths = [Path(jf) for jf in jd_files][:1]
    jd_bundle = [(p, f"{i}::{p.name}", load_jd(p)) for i, p in enumerate(jd_paths)]
    jd_skills_by_id = extract_jd_skills_bulk(llm, [(bid, text) for _, bid, text in jd_bundle])

    resume_paths = [Path(rf).resolve() for rf in resume_files]
    candidate_by_resume = build_resume_candidate_map(llm, resume_paths)

    for jd_path, _bulk_id, jd_text in jd_bundle:
        jd_skills = jd_skills_by_id[_bulk_id]
        jd_file_name = jd_path.name
        jd_label = jd_path.stem

        for resume_file in resume_files:
            rpath = Path(resume_file).resolve()
            candidate = candidate_by_resume[str(rpath)]
            match_rows = match_candidate_to_jd(llm, candidate, jd_text, jd_skills)
            row = build_candidate_jd_summary_row(
                llm,
                jd_label=jd_label,
                jd_file_name=jd_file_name,
                jd_text=jd_text,
                jd_skills=jd_skills,
                resume_file_name=rpath.name,
                candidate=candidate,
                match_rows=match_rows,
            )
            summary_rows.append(row)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    write_candidate_jd_summary(summary_rows, out)

    summary_path = out / "pipeline_summary.json"
    summary_path.write_text(
        json.dumps(summary_rows, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return summary_rows, candidate_by_resume


def main() -> None:
    base = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Resume–JD matcher pipeline")
    parser.add_argument(
        "--resumes",
        nargs="*",
        default=[],
        help="Resume file paths (pdf/docx/txt). Default: all under data/resumes/",
    )
    parser.add_argument(
        "--jds",
        nargs="*",
        default=[],
        help="JD file path(s); only the first is used if multiple are given. Default: data/jds/",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(base / "outputs"),
        help="CSV and summary output directory",
    )
    args = parser.parse_args()

    resumes = [Path(p) for p in args.resumes] if args.resumes else _default_globs(
        base / "data" / "resumes",
        ("*.pdf", "*.docx", "*.txt"),
    )
    jds = [Path(p) for p in args.jds] if args.jds else _default_globs(
        base / "data" / "jds",
        ("*.txt", "*.docx", "*.md"),
    )

    if not jds:
        raise SystemExit("No JD files found. Add .txt/.docx to data/jds/ or pass --jds paths.")
    if not resumes:
        raise SystemExit("No resume files found. Add files to data/resumes/ or pass --resumes paths.")

    if len(jds) > 1:
        print(
            f"Note: only one job description is supported; using {jds[0].name!r} "
            f"and ignoring {len(jds) - 1} other file(s)."
        )
        jds = [jds[0]]

    llm = get_llm_client()
    results, candidate_by_resume = run_pipeline(resumes, jds, llm, output_dir=args.output)
    for r in results:
        print(
            f"{r['Candidate']} vs {r['JD file']}: "
            f"resume {r['Resume score']}% | profile {r['Profile score']}%"
        )
    out = Path(args.output).resolve()
    resume_targets = [(p.resolve(), p.name) for p in resumes]
    written = export_recruiter_summary_pdfs(results, resume_targets, candidate_by_resume, out_root=out)
    for w in written:
        print(f"Recruiter PDF: {w}")


if __name__ == "__main__":
    main()
