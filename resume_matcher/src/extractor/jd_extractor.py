import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

from src.llm.client import LLMClient
from src.utils.json_utils import parse_llm_json
from src.utils.normalize import normalize_token


def extract_jd_skills(llm: LLMClient, jd_text: str) -> dict[str, Any]:
    truncated = jd_text[:12000] if len(jd_text) > 12000 else jd_text
    prompt = f"""
Read this job description and split skills into what is **required** (must-have, mandatory, minimum qualifications)
versus **optional** (nice-to-have, preferred, plus).

For each skill, assign a category: Programming Languages, Frameworks, Tools, Cloud, Data, Soft Skills, or Other.

JD:
{truncated}

Return a single JSON object only (no markdown), with this exact shape:
{{
  "required_skills": [
    {{ "category": "", "skill": "" }}
  ],
  "optional_skills": [
    {{ "category": "", "skill": "" }}
  ]
}}

Rules:
- Put skills explicitly marked as required, must-have, minimum, or core to the role in required_skills.
- Put preferred, nice-to-have, bonus, or "familiarity with" items in optional_skills when the JD softens them.
- Do not duplicate the same skill in both lists; if unclear, prefer required_skills.
- Use concise skill names (e.g. "Python", "AWS", "Kubernetes").
- **JSON only**: straight double quotes for all keys and strings; no trailing commas; escape any double quote inside a string as \\"; no raw line breaks inside string values.
"""
    raw = llm.invoke(prompt)
    try:
        data = parse_llm_json(raw)
    except json.JSONDecodeError as e:
        logger.warning("JD skills JSON parse failed: %s", e)
        data = {"required_skills": [], "optional_skills": []}
    if not isinstance(data, dict):
        data = {"required_skills": [], "optional_skills": []}
    return _normalize_jd_skills_payload(data)


def _normalize_skill_entry(item: Any) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    sk = str(item.get("skill") or "").strip()
    if not sk:
        return None
    cat = str(item.get("category") or "").strip()
    return {"category": cat, "skill": normalize_token(sk)}


def _normalize_jd_skills_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Build required_skills, optional_skills, and a combined ``skills`` list for backward compatibility."""
    required: list[dict[str, str]] = []
    optional: list[dict[str, str]] = []

    for x in data.get("required_skills") or []:
        ne = _normalize_skill_entry(x)
        if ne:
            required.append(ne)
    for x in data.get("optional_skills") or []:
        ne = _normalize_skill_entry(x)
        if ne:
            optional.append(ne)

    legacy = data.get("skills") or []
    if not required and not optional and legacy:
        for item in legacy:
            if not isinstance(item, dict):
                continue
            ne = _normalize_skill_entry(item)
            if not ne:
                continue
            imp = str(item.get("importance", "")).lower()
            if imp == "high":
                required.append(ne)
            else:
                optional.append(ne)

    seen_req = {e["skill"].lower() for e in required}
    optional = [e for e in optional if e["skill"].lower() not in seen_req]

    combined: list[dict[str, Any]] = []
    for e in required:
        combined.append({**e, "importance": "required"})
    for e in optional:
        combined.append({**e, "importance": "optional"})

    return {
        "required_skills": required,
        "optional_skills": optional,
        "skills": combined,
    }


def extract_jd_skills_bulk(llm: LLMClient, jd_items: list[tuple[str, str]]) -> dict[str, dict[str, Any]]:
    """
    One LLM call for multiple job descriptions.

    ``jd_items``: ``(jd_id, full_text)`` — use a stable unique id (e.g. file name).

    Returns ``jd_id -> normalized JD skills dict``.
    """
    if not jd_items:
        return {}
    if len(jd_items) == 1:
        jid, text = jd_items[0]
        return {jid: extract_jd_skills(llm, text)}

    blocks: list[str] = []
    for jid, text in jd_items:
        cap = 6000 if len(jd_items) > 2 else 12000
        t = text[:cap] if len(text) > cap else text
        blocks.append(f"=== JD_ID: {jid} ===\n{t}\n")
    bundle = "\n".join(blocks)

    prompt = f"""
For EACH job description below, extract **required** (must-have) vs **optional** (nice-to-have / preferred) skills.
Use categories: Programming Languages, Frameworks, Tools, Cloud, Data, Soft Skills, or Other.

Each block starts with "=== JD_ID: <id> ===" — use that exact id in your output.

Job descriptions:
{bundle}

Return a single JSON object only (no markdown), with this exact shape:
{{
  "results": [
    {{
      "jd_id": "<must match the JD_ID from the header>",
      "required_skills": [ {{ "category": "", "skill": "" }} ],
      "optional_skills": [ {{ "category": "", "skill": "" }} ]
    }}
  ]
}}

There must be exactly one results entry per JD_ID. Do not duplicate a skill in both lists for the same JD.
Use valid JSON only: double quotes for keys/strings, no trailing commas, escape internal quotes as \\", no raw line breaks inside strings.
"""
    raw = llm.invoke(prompt)
    try:
        data = parse_llm_json(raw)
    except json.JSONDecodeError as e:
        logger.warning("JD skills bulk JSON parse failed: %s", e)
        data = {}
    if not isinstance(data, dict):
        data = {}
    results = data.get("results") or []
    out: dict[str, dict[str, Any]] = {}
    for entry in results:
        if not isinstance(entry, dict):
            continue
        jid = str(entry.get("jd_id", "") or "").strip()
        if not jid:
            continue
        body = {k: v for k, v in entry.items() if k != "jd_id"}
        out[jid] = _normalize_jd_skills_payload(body)

    for jid, text in jd_items:
        if jid not in out:
            out[jid] = extract_jd_skills(llm, text)
    return {jid: out[jid] for jid, _ in jd_items}


def jd_skills_summary(jd_skills: dict[str, Any]) -> str:
    return json.dumps(jd_skills, ensure_ascii=False, indent=2)


def format_required_skills_column(jd_skills: dict[str, Any] | None) -> str:
    """Human-readable required vs optional skills for CSV/summary."""

    def _fmt_items(items: list[Any]) -> str:
        parts: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            skill = (item.get("skill") or "").strip()
            if not skill:
                continue
            cat = (item.get("category") or "").strip()
            parts.append(f"{skill} ({cat})" if cat else skill)
        return ", ".join(parts)

    if not jd_skills:
        return ""

    req = jd_skills.get("required_skills") or []
    opt = jd_skills.get("optional_skills") or []
    if not req and not opt:
        return _fmt_items(jd_skills.get("skills") or [])

    chunks: list[str] = []
    rs = _fmt_items(req)
    if rs:
        chunks.append(f"Required: {rs}")
    os_ = _fmt_items(opt)
    if os_:
        chunks.append(f"Optional: {os_}")
    return " | ".join(chunks)
