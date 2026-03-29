import json
from typing import Any

from src.llm.client import LLMClient
from src.utils.json_utils import parse_llm_json
from src.utils.normalize import normalize_skill_list


def extract_resume_with_llm(llm: LLMClient, text: str) -> dict[str, Any]:
    truncated = text[:12000] if len(text) > 12000 else text
    prompt = f"""
Extract structured information from this resume. Use only facts present in the text; use empty string or [] if unknown.

Resume:
{truncated}

Return a single JSON object only (no markdown), with this exact shape:
{{
  "name": "",
  "email": "",
  "phone": "",
  "address": "",
  "skills": [],
  "experience": [
    {{
      "company": "",
      "role": "",
      "duration": "",
      "description": ""
    }}
  ]
}}
"""
    raw = llm.invoke(prompt)
    data = parse_llm_json(raw)
    if isinstance(data.get("skills"), list):
        data["skills"] = normalize_skill_list([str(s) for s in data["skills"]])
    return data


def candidate_profile_json(candidate: dict[str, Any]) -> str:
    return json.dumps(candidate, ensure_ascii=False, indent=2)


def format_candidate_skills_column(candidate: dict[str, Any]) -> str:
    skills = candidate.get("skills") or []
    if isinstance(skills, list) and skills:
        return "; ".join(str(s).strip() for s in skills if str(s).strip())
    return ""
