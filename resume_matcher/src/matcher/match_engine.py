import json
from pathlib import Path
from typing import Any

from src.extractor.jd_extractor import format_required_skills_column, jd_skills_summary
from src.extractor.resume_extractor import candidate_profile_json, format_candidate_skills_column
from src.llm.client import LLMClient
from src.utils.json_utils import parse_llm_json
from src.utils.normalize import normalize_token
from src.utils.recruiter_scores import overall_match_from_dimension_rows


def _candidate_shows_skill_evidence(candidate: dict[str, Any], jd_skill: str) -> bool:
    """True if the JD skill appears in structured skills or experience text."""
    jd_skill = (jd_skill or "").strip()
    if not jd_skill:
        return False

    norm_jd = normalize_token(jd_skill).lower()
    jd_lower = jd_skill.lower()

    for sk in candidate.get("skills") or []:
        if normalize_token(str(sk)).lower() == norm_jd:
            return True

    blob_parts: list[str] = []
    for sk in candidate.get("skills") or []:
        blob_parts.append(str(sk))
    for e in candidate.get("experience") or []:
        if isinstance(e, dict):
            for k in ("company", "role", "description", "duration"):
                blob_parts.append(str(e.get(k, "") or ""))
    blob = " ".join(blob_parts).lower()

    if jd_lower in blob:
        return True
    if norm_jd and norm_jd in blob:
        return True

    return False


def _jd_skill_items_for_scoring(jd_skills: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Skills that count toward deterministic resume coverage: required when present, else legacy fallbacks."""
    if not jd_skills:
        return []
    req = jd_skills.get("required_skills")
    if isinstance(req, list) and req:
        return [x for x in req if isinstance(x, dict)]
    skills = jd_skills.get("skills") or []
    if not isinstance(skills, list):
        return []
    high_only = [
        x for x in skills if isinstance(x, dict) and str(x.get("importance", "")).lower() == "high"
    ]
    if high_only:
        return high_only
    tagged = [
        x for x in skills if isinstance(x, dict) and str(x.get("importance", "")).lower() == "required"
    ]
    if tagged:
        return tagged
    return [x for x in skills if isinstance(x, dict)]


def candidate_vs_jd_skills_score_percent(
    candidate: dict[str, Any],
    jd_skills: dict[str, Any] | None,
) -> float:
    """
    Score 0–100: share of **required** JD skills (when extracted that way) for which the candidate shows
    literal evidence in structured skills or experience text. Implicit ecosystem inference is **not**
    applied here (see LLM match ratings and profile score).
    """
    if not jd_skills:
        return 0.0
    items = _jd_skill_items_for_scoring(jd_skills)
    jd_skill_names: list[str] = []
    for item in items:
        sk = str(item.get("skill") or "").strip()
        if sk:
            jd_skill_names.append(sk)
    if not jd_skill_names:
        return 0.0
    matched = sum(1 for sk in jd_skill_names if _candidate_shows_skill_evidence(candidate, sk))
    return round(100.0 * matched / len(jd_skill_names), 1)


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

Structured JD skills (JSON). Lists ``required_skills`` and ``optional_skills`` when present; otherwise a flat ``skills`` array:
{skills_block}

Candidate profile (JSON):
{candidate_block}

If the profile includes non-empty "recruiter_notes", treat it as hiring-team context and use it as evidence when rating skills and writing match reasons.

Produce one row per **required** JD skill (from ``required_skills`` or clearly mandatory in the JD). Also include **optional** JD skills as separate rows when they are listed in ``optional_skills`` or as nice-to-haves — use a slightly lower bar for optional items but still rate honestly.

For each skill row:
- Compare against the candidate's skills **and** experience narrative.
- **Infer reasonable ecosystem evidence**: if the JD asks for a parent technology and the resume shows a normal stack built on it, count that as evidence even when the parent name is not spelled out (e.g. Spring Boot / Spring Framework → Java; React / Next.js → JavaScript/TypeScript; .NET Core → C#; PyTorch on resume → Python ML ecosystem). State the inference briefly in the reason when you use it.
- Use rating 0 only when there is **no** plausible relevant experience or adjacent stack evidence.
- Rating integer 0–10. Do not use 1 as a substitute for "no match" when you mean zero evidence.

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
        skill_name = str(row.get("Skill", row.get("skill", "")) or "")
        out.append(
            {
                "Skill Category": row.get("Skill Category", row.get("skill category", "")),
                "Skill": skill_name,
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


def llm_profile_score_percent(
    llm: LLMClient,
    candidate: dict[str, Any],
    jd_text: str,
    jd_skills: dict[str, Any] | None,
    match_rows: list[dict[str, Any]],
    resume_coverage_percent: float,
) -> float:
    """
    Holistic 0–100 fit from the LLM: can the person do the job given experience and transferability,
    not only keyword overlap (see ``resume_coverage_percent``).
    """
    jd_part = jd_text[:6000] if len(jd_text) > 6000 else jd_text
    skills_json = jd_skills_summary(jd_skills) if jd_skills else "{}"
    cand_json = candidate_profile_json(candidate)
    ratings_json = json.dumps(match_rows, ensure_ascii=False)[:4000]

    prompt = f"""
You are an expert technical recruiter. Judge overall role fit from the FULL candidate profile
(experience narrative, seniority, adjacent and transferable skills), not only explicit keyword overlap.

Job description (excerpt):
{jd_part}

Structured JD skills (JSON):
{skills_json}

Candidate profile (JSON):
{cand_json}

If "recruiter_notes" is present, factor it into holistic fit like verified updates from the hiring team.

Per-skill assessment notes (JSON array):
{ratings_json}

Resume score (deterministic keyword/JD-skill coverage on the resume): {resume_coverage_percent}%

Return a single JSON object only (no markdown) with exactly this key:
{{ "profile_score": <integer 0-100> }}

profile_score meaning:
- 0–20: poor fit for the role as described
- 40–60: plausible fit with gaps; may succeed with onboarding
- 70–85: strong fit; missing keywords may still be OK if experience transfers
- 90–100: exceptional alignment

Base your number on whether the person could plausibly perform the job soon, even if several JD keywords are absent from the resume.
Treat ecosystem and stack evidence as valid (e.g. framework experience implying language/platform competence) when professionally reasonable.
"""
    raw = llm.invoke(prompt)
    data = parse_llm_json(raw)
    try:
        v = float(data.get("profile_score", data.get("Profile score", 0)))
    except (TypeError, ValueError):
        v = 0.0
    return max(0.0, min(100.0, round(v, 1)))


def selection_rationale(
    llm: LLMClient,
    candidate: dict[str, Any],
    jd_text: str,
    jd_skills: dict[str, Any] | None,
    match_rows: list[dict[str, Any]],
    resume_score: float,
    profile_score: float,
) -> dict[str, str]:
    jd_part = jd_text[:4000] if len(jd_text) > 4000 else jd_text
    skills_json = jd_skills_summary(jd_skills) if jd_skills else "{}"
    cand_json = candidate_profile_json(candidate)
    ratings_json = json.dumps(match_rows, ensure_ascii=False)[:8000]

    prompt = f"""
You are an expert recruiter.

Job description (excerpt):
{jd_part}

Structured JD skills (JSON):
{skills_json}

Candidate profile (JSON):
{cand_json}

If "recruiter_notes" is present, incorporate that context into why_select / why_not_select.

Per-skill assessment (JSON array, ratings 0-10):
{ratings_json}

Resume score (0-100%): deterministic keyword coverage vs **required** structured JD skills (does not infer parent tech from frameworks): {resume_score}%
Profile score (0-100%): holistic judgment including transferable and ecosystem-implied skills: {profile_score}%

Return a single JSON object only (no markdown) with exactly these keys:
- "why_select": 2-5 sentences on strengths, skill fit, and reasons to advance (e.g. interview).
- "why_not_select": 2-5 sentences on gaps, missing skills, risks, or reasons to hesitate. If the profile is strong, state what to validate in the process rather than inventing issues.
"""
    raw = llm.invoke(prompt)
    data = parse_llm_json(raw)
    return {
        "why_select": str(data.get("why_select", "") or "").strip(),
        "why_not_select": str(data.get("why_not_select", "") or "").strip(),
    }


def _normalize_recruiter_ready_page(
    data: Any,
    *,
    profile_score: float,
    resume_score: float,
    candidate: dict[str, Any],
    rationale: dict[str, str],
) -> dict[str, Any]:
    """Validate LLM JSON and fill gaps for the recruiter summary PDF."""
    if not isinstance(data, dict):
        data = {}
    summary = str(data.get("candidate_summary") or "").strip()
    vl = data.get("verdict_lines")
    if isinstance(vl, str):
        verdict_lines = [vl.strip()] if vl.strip() else []
    elif isinstance(vl, list):
        verdict_lines = [str(x).strip() for x in vl if str(x).strip()]
    else:
        verdict_lines = []

    try:
        om_llm = float(data.get("overall_match_out_of_5", profile_score / 20.0))
    except (TypeError, ValueError):
        om_llm = profile_score / 20.0
    om_llm = max(0.0, min(5.0, round(om_llm, 1)))

    pctx = data.get("overall_match_percent_approx")
    try:
        pct_llm = int(round(float(pctx)))
    except (TypeError, ValueError):
        pct_llm = int(round(om_llm / 5.0 * 100))
    pct_llm = max(0, min(100, pct_llm))

    raw_rows = data.get("dimension_rows") or data.get("dimensions") or []
    dim_out: list[dict[str, Any]] = []
    if isinstance(raw_rows, list):
        for r in raw_rows:
            if not isinstance(r, dict):
                continue
            sa = str(r.get("skill_area") or r.get("Skill Area") or "").strip()
            if not sa:
                continue
            try:
                sc = int(round(float(r.get("score_out_of_5", r.get("score", 0)))))
            except (TypeError, ValueError):
                sc = 0
            sc = max(0, min(5, sc))
            if sc <= 2:
                continue
            dim_out.append(
                {
                    "skill_area": sa,
                    "jd_requirement": str(r.get("jd_requirement") or r.get("JD Requirement") or "").strip(),
                    "resume_evidence": str(r.get("resume_evidence") or r.get("Resume Evidence") or "").strip(),
                    "score_out_of_5": sc,
                    "match_summary": str(r.get("match_summary") or r.get("Match Summary") or "").strip(),
                }
            )

    name = str(candidate.get("name") or "The candidate").strip()
    if not summary:
        summary = (
            f"{name}: automated narrative was incomplete. Holistic profile fit is about {profile_score:.0f}%; "
            f"resume keyword coverage vs required JD skills is about {resume_score:.0f}%."
        )
        ws = (rationale.get("why_select") or "").strip()
        if ws:
            summary += f"\n\n{ws}"
    if not verdict_lines:
        verdict_lines = [
            "Review dimension scores and validate fit in screening.",
            "Probe gaps noted in the table during interview.",
        ]
    if not dim_out:
        sc = max(0, min(5, int(round(profile_score / 20.0))))
        if sc > 2:
            dim_out.append(
                {
                    "skill_area": "Overall fit",
                    "jd_requirement": "Role requirements (see JD)",
                    "resume_evidence": "See candidate profile",
                    "score_out_of_5": sc,
                    "match_summary": "See summary above",
                }
            )

    om, pct = om_llm, pct_llm
    derived = overall_match_from_dimension_rows(dim_out)
    if derived is not None:
        om, pct = derived

    return {
        "candidate_summary": summary,
        "verdict_lines": verdict_lines,
        "overall_match_out_of_5": om,
        "overall_match_percent_approx": pct,
        "dimension_rows": dim_out,
    }


def build_recruiter_ready_page(
    llm: LLMClient,
    candidate: dict[str, Any],
    jd_text: str,
    jd_skills: dict[str, Any] | None,
    match_rows: list[dict[str, Any]],
    resume_score: float,
    profile_score: float,
    rationale: dict[str, str],
) -> dict[str, Any]:
    """
    Single structured payload for the first PDF page: brief career/strengths blurb, verdict lines,
    overall /5 score, and a dimension table (Skill Area, JD requirement, resume evidence, score, summary).
    """
    jd_part = jd_text[:7000] if len(jd_text) > 7000 else jd_text
    skills_json = jd_skills_summary(jd_skills) if jd_skills else "{}"
    cand_json = candidate_profile_json(candidate)
    match_json = json.dumps(match_rows, ensure_ascii=False)[:6000]
    ws = str(rationale.get("why_select", "") or "")[:1200]
    wn = str(rationale.get("why_not_select", "") or "")[:1200]

    prompt = f"""
You are an expert technical recruiter. Produce **one ready-to-share recruiter summary page** for this role and candidate.

Job description (excerpt):
{jd_part}

Structured JD skills (JSON):
{skills_json}

Candidate profile (JSON):
{cand_json}

If the profile includes a non-empty "recruiter_notes" field, treat it as **trusted hiring-team context** (e.g. recent role changes, skills not on the resume); weigh it alongside the structured resume fields in summary, verdict, and dimension_rows.

Per-skill LLM assessments (JSON):
{match_json}

Reference notes (keep consistent; you may polish wording):
- Strengths / reasons to advance: {ws}
- Gaps / caution: {wn}

Calibration scores: resume keyword coverage vs **required** JD skills: {resume_score}%; holistic profile fit: {profile_score}%.

Return a single JSON object only (no markdown) with exactly these keys:

1) "candidate_summary" — **short**: at most **3–5 sentences total** (you may use \\n\\n between 1–2 tight paragraphs). High-level only: years/seniority, career focus, and **core strengths** relevant to technical roles. Do **not** write a long narrative, detailed JD alignment, or gap analysis here (verdict and dimension_rows cover fit).

2) "verdict_lines" — JSON array of **2 to 3** short strings: hiring recommendation lines (fit level, shortlist guidance). No leading bullets inside each string.

3) "overall_match_out_of_5" — number from 0 to 5 (one decimal allowed); used only if no dimension rows remain after filtering — otherwise the app recomputes it as the **mean** of kept dimension scores.

4) "overall_match_percent_approx" — integer 0-100; same fallback rule as (3).

5) "dimension_rows" — array of **12 to 18** objects, each with exactly:
   - "skill_area": short label (e.g. Experience, Location, and JD-specific themes like SRE Concepts, Cloud, Kubernetes, Observability).
   - "jd_requirement": one short phrase from JD expectations.
   - "resume_evidence": what the resume shows, including indirect evidence when fair.
   - "score_out_of_5": integer **0-5** (5 = excellent, 0 = no evidence).
   - "match_summary": very short label (e.g. "Strong match", "Gap", "Indirect via DevOps").

Include **Experience** (years/seniority) and **Location** or work model when inferable; add rows that mirror JD priorities. Order from broad requirements to specific technical areas.
"""
    raw = llm.invoke(prompt)
    data = parse_llm_json(raw)
    return _normalize_recruiter_ready_page(
        data,
        profile_score=profile_score,
        resume_score=resume_score,
        candidate=candidate,
        rationale=rationale,
    )


def _skill_matrix_from_recruiter_page(page: dict[str, Any]) -> list[dict[str, Any]]:
    """Flat rows for CSV/JSON export (column Skill matrix JSON)."""
    out: list[dict[str, Any]] = []
    for d in page.get("dimension_rows") or []:
        if not isinstance(d, dict):
            continue
        n = int(d.get("score_out_of_5", 0))
        n = max(0, min(5, n))
        if n <= 2:
            continue
        out.append(
            {
                "Skill Area": d.get("skill_area", ""),
                "JD Requirement": d.get("jd_requirement", ""),
                "Resume Evidence": d.get("resume_evidence", ""),
                "Score": f"{n}/5",
                "Match Summary": d.get("match_summary", ""),
            }
        )
    return out


def build_candidate_jd_summary_row(
    llm: LLMClient,
    jd_label: str,
    jd_file_name: str,
    jd_text: str,
    jd_skills: dict[str, Any] | None,
    resume_file_name: str,
    candidate: dict[str, Any],
    match_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    resume_score = candidate_vs_jd_skills_score_percent(candidate, jd_skills)
    profile_score = llm_profile_score_percent(
        llm, candidate, jd_text, jd_skills, match_rows, resume_score
    )
    rationale = selection_rationale(
        llm, candidate, jd_text, jd_skills, match_rows, resume_score, profile_score
    )
    recruiter_page = build_recruiter_ready_page(
        llm,
        candidate,
        jd_text,
        jd_skills,
        match_rows,
        resume_score,
        profile_score,
        rationale,
    )
    skill_matrix = _skill_matrix_from_recruiter_page(recruiter_page)
    cand_name = str(candidate.get("name") or "").strip() or Path(resume_file_name).stem

    notes = str(candidate.get("recruiter_notes") or "").strip()

    return {
        "JD": jd_label,
        "Candidate": cand_name,
        "JD file": jd_file_name,
        "Resume file": resume_file_name,
        "Required skills": format_required_skills_column(jd_skills),
        "Candidate skills": format_candidate_skills_column(candidate),
        "Resume score": resume_score,
        "Profile score": profile_score,
        "Why select": rationale["why_select"],
        "Why not select": rationale["why_not_select"],
        "recruiter_notes": notes,
        "recruiter_page": recruiter_page,
        "skill_matrix": skill_matrix,
    }
