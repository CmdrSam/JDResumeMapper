"""
CLI entry: match resumes in data/resumes against JDs in data/jds (or paths you pass).
Run from this directory:  python main.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.extractor.jd_extractor import extract_jd_skills
from src.extractor.resume_extractor import extract_resume_with_llm
from src.llm.client import get_llm_client
from src.matcher.match_engine import composite_score, match_candidate_to_jd
from src.parser.jd_parser import load_jd
from src.parser.resume_parser import extract_text_from_resume
from src.utils.table import create_table


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
) -> list[dict]:
    output_dir = output_dir or Path(__file__).resolve().parent / "outputs"
    results: list[dict] = []

    for jd_file in jd_files:
        jd_path = Path(jd_file)
        jd_text = load_jd(jd_path)
        jd_skills = extract_jd_skills(llm, jd_text)

        for resume_file in resume_files:
            rpath = Path(resume_file)
            text = extract_text_from_resume(rpath)
            candidate = extract_resume_with_llm(llm, text)
            match_rows = match_candidate_to_jd(llm, candidate, jd_text, jd_skills)
            cand_name = candidate.get("name") or rpath.stem
            jd_name = jd_path.stem
            create_table(match_rows, cand_name, jd_name, output_dir)
            scores = composite_score(match_rows)
            results.append(
                {
                    "candidate_file": str(rpath),
                    "jd_file": str(jd_path),
                    "candidate_name": cand_name,
                    "match_rows": match_rows,
                    "composite_score": scores,
                }
            )

    summary_path = Path(output_dir) / "pipeline_summary.json"
    serializable = [
        {
            "candidate_file": r["candidate_file"],
            "jd_file": r["jd_file"],
            "candidate_name": r["candidate_name"],
            "composite_score": r["composite_score"],
            "match_rows": r["match_rows"],
        }
        for r in results
    ]
    summary_path.write_text(json.dumps(serializable, indent=2, ensure_ascii=False), encoding="utf-8")
    return results


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
        help="JD file paths (txt/docx). Default: all under data/jds/",
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

    llm = get_llm_client()
    results = run_pipeline(resumes, jds, llm, output_dir=args.output)
    for r in results:
        print(f"{r['candidate_name']} vs {Path(r['jd_file']).name}: final score ~ {r['composite_score']['final']}")


if __name__ == "__main__":
    main()
