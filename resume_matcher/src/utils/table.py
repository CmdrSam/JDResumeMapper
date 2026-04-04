import json
from pathlib import Path

import pandas as pd

SUMMARY_CSV_NAME = "candidate_vs_jd_summary.csv"

SUMMARY_COLUMNS = [
    "JD",
    "Candidate",
    "JD file",
    "Resume file",
    "Required skills",
    "Candidate skills",
    "Resume score",
    "Profile score",
    "Why select",
    "Why not select",
    "Recruiter notes",
    "Skill matrix JSON",
]


def write_candidate_jd_summary(rows: list[dict], output_dir: str | Path, filename: str = SUMMARY_CSV_NAME) -> pd.DataFrame:
    """Write one row per (candidate, JD) pair. ``skill_matrix`` is serialized to ``Skill matrix JSON``."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    if not rows:
        df = pd.DataFrame(columns=SUMMARY_COLUMNS)
    else:
        flat: list[dict] = []
        for r in rows:
            entry = {c: r.get(c, "") for c in SUMMARY_COLUMNS if c != "Skill matrix JSON"}
            entry["Skill matrix JSON"] = json.dumps(r.get("skill_matrix", []), ensure_ascii=False)
            flat.append(entry)
        df = pd.DataFrame(flat)
        for col in SUMMARY_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        df = df[SUMMARY_COLUMNS]
    df.to_csv(out / filename, index=False)
    return df
