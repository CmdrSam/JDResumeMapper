import json
from typing import Any

from src.llm.client import LLMClient
from src.utils.json_utils import parse_llm_json
from src.utils.normalize import normalize_token


def extract_jd_skills(llm: LLMClient, jd_text: str) -> dict[str, Any]:
    truncated = jd_text[:12000] if len(jd_text) > 12000 else jd_text
    prompt = f"""
Extract required and preferred skills from this job description.
Categorize each as: Programming Languages, Frameworks, Tools, Soft Skills, or Other.

JD:
{truncated}

Return a single JSON object only (no markdown), with this exact shape:
{{
  "skills": [
    {{
      "category": "",
      "skill": "",
      "importance": "high"
    }}
  ]
}}
Use importance one of: high, medium, low.
"""
    raw = llm.invoke(prompt)
    data = parse_llm_json(raw)
    skills = data.get("skills") or []
    for item in skills:
        if isinstance(item, dict) and item.get("skill"):
            item["skill"] = normalize_token(str(item["skill"]))
    data["skills"] = skills
    return data


def jd_skills_summary(jd_skills: dict[str, Any]) -> str:
    return json.dumps(jd_skills, ensure_ascii=False, indent=2)
