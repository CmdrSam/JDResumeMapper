import json
from typing import Any

from src.extractor.jd_extractor import jd_skills_summary
from src.extractor.resume_extractor import candidate_profile_json
from src.llm.client import LLMClient
from src.utils.json_utils import parse_llm_json


def match_candidate_to_jd(
    llm: LLMClient,
    candidate: dict[str, Any],
    jd_text: str,
    jd_skills: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    jd_part = jd_text[:8000] if len(jd_text) > 8000 else jd_text
    skills_block = jd_skills_summary(jd_skills) if jd_skills else "{}"
    candidate_block = candidate_profile_json(candidate)

    prompt = f"""
You are an expert recruiter.

Job description (excerpt):
{jd_part}

Structured JD skills (JSON):
{skills_block}

Candidate profile (JSON):
{candidate_block}

For each distinct required or important skill from the JD (use the structured skills list when present, else infer from the JD text):
- Compare against the candidate's skills and experience
- Give concise reasoning
- Rating integer 1-10 for fit on that skill

Return a single JSON array only (no markdown). Each element must have these keys:
"Skill Category", "Skill", "JD Requirement", "Candidate Match Reason", "Rating"
"""
    raw = llm.invoke(prompt)
    rows = parse_llm_json(raw)
    if not isinstance(rows, list):
        raise ValueError("match_candidate_to_jd: expected JSON array")
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        r = row.get("Rating", row.get("rating", 0))
        try:
            rating = int(float(r)) if r is not None else 0
        except (TypeError, ValueError):
            rating = 0
        rating = max(0, min(10, rating))
        out.append(
            {
                "Skill Category": row.get("Skill Category", row.get("skill category", "")),
                "Skill": row.get("Skill", row.get("skill", "")),
                "JD Requirement": row.get("JD Requirement", row.get("jd requirement", "")),
                "Candidate Match Reason": row.get(
                    "Candidate Match Reason",
                    row.get("candidate match reason", ""),
                ),
                "Rating": rating,
            }
        )
    return out


def composite_score(
    rows: list[dict[str, Any]],
    skill_weight: float = 0.5,
    experience_weight: float = 0.3,
    seniority_weight: float = 0.2,
) -> dict[str, float]:
    """
    Optional aggregate from per-skill ratings.
    When only skill-level ratings exist, skill_match uses their mean; other terms default from same mean.
    """
    if not rows:
        return {"final": 0.0, "skill_match": 0.0, "experience_relevance": 0.0, "seniority_fit": 0.0}
    ratings = [float(r.get("Rating", 0) or 0) for r in rows]
    mean_r = sum(ratings) / len(ratings) / 10.0
    final = skill_weight * mean_r + experience_weight * mean_r + seniority_weight * mean_r
    return {
        "final": round(final * 100, 1),
        "skill_match": round(mean_r * 100, 1),
        "experience_relevance": round(mean_r * 100, 1),
        "seniority_fit": round(mean_r * 100, 1),
    }
