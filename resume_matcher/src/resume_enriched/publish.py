"""
Prepend a recruiter summary page to each resume PDF.

Default output directory: ``outputs/`` under the ``resume_matcher`` package root
(same area as CSV/JSON from the CLI pipeline).
"""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import HRFlowable, Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from src.parser.resume_parser import extract_text_from_resume

DEFAULT_OUTPUT_RELATIVE = Path("outputs")


def _para_html(text: str) -> str:
    """Escape for ReportLab Paragraph and preserve line breaks."""
    if text is None:
        return ""
    return escape(str(text).strip()).replace("\n", "<br/>").replace("\r", "")


def _parse_score(val: Any) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _resolve_logo_file() -> Path | None:
    """First existing logo under ``src/resources/`` (common extensions)."""
    base = Path(__file__).resolve().parents[1] / "resources"
    for name in ("logo.jpeg", "logo.jpg", "logo.png", "logo.JPEG", "logo.JPG", "logo.PNG"):
        p = base / name
        if p.is_file():
            return p
    return None


def _logo_header_flowables(doc_width: float) -> list[Any]:
    """
    Right-aligned logo as Platypus flowables (canvas-drawn images can be painted over by the frame).
    """
    logo = _resolve_logo_file()
    if not logo:
        return []
    max_w, max_h = 1.52 * inch, 0.58 * inch
    w, h = max_w, max_h * 0.75
    try:
        ir = ImageReader(str(logo))
        iw, ih = ir.getSize()
        if iw > 0 and ih > 0:
            sc = min(max_w / float(iw), max_h / float(ih))
            w, h = iw * sc, ih * sc
    except OSError:
        return []
    try:
        rl_img = Image(str(logo), width=w, height=h)
    except OSError:
        return []
    aux = Table([[Spacer(0, 0), rl_img]], colWidths=[doc_width - w, w])
    aux.setStyle(
        TableStyle(
            [
                ("ALIGN", (1, 0), (1, 0), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return [aux, Spacer(1, 0.12 * inch)]


def _cover_first_page_canvas(canvas: Any, doc: Any) -> None:
    """Reserved for future page chrome; logo is drawn via flowables so it stays visible."""
    return


def _cover_later_pages_canvas(canvas: Any, doc: Any) -> None:
    pw, ph = doc.pagesize
    canvas.saveState()
    canvas.setFont("Helvetica-Oblique", 8)
    canvas.setFillColor(colors.HexColor("#6b7280"))
    canvas.drawCentredString(pw / 2, 0.48 * inch, "Recruiter summary · continuation · confidential")
    canvas.restoreState()


def _contact_block(candidate: dict[str, Any]) -> list[str]:
    lines = []
    name = str(candidate.get("name") or "").strip()
    if name:
        lines.append(f"Name: {name}")
    email = str(candidate.get("email") or "").strip()
    if email:
        lines.append(f"Email: {email}")
    phone = str(candidate.get("phone") or "").strip()
    if phone:
        lines.append(f"Phone: {phone}")
    addr = str(candidate.get("address") or "").strip()
    if addr:
        lines.append(f"Address: {addr}")
    return lines or ["Contact: (not extracted)"]


def _percent_to_star_int(val: Any) -> int:
    try:
        p = float(str(val).replace("%", "").strip())
        return max(0, min(5, int(round(p / 20.0))))
    except (TypeError, ValueError):
        return 0


def _parse_score_slash5_from_cell(score_cell: str) -> int | None:
    """Extract n from '(n/5)' or 'n/5' in the score cell."""
    s = str(score_cell)
    m = re.search(r"\(\s*(\d)\s*/\s*5\s*\)", s)
    if m:
        return max(0, min(5, int(m.group(1))))
    m = re.search(r"(?<![\d])(\d)\s*/\s*5\b", s)
    if m:
        return max(0, min(5, int(m.group(1))))
    return None


def _recruiter_page_for_pdf(jd_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Prefer ``recruiter_page`` from the pipeline; otherwise synthesize from legacy row fields + old matrix shape.
    """
    empty: dict[str, Any] = {
        "candidate_summary": "No job description was evaluated for this candidate.",
        "verdict_lines": ["Add a JD and re-run the pipeline to generate a full summary."],
        "overall_match_out_of_5": 0.0,
        "overall_match_percent_approx": 0,
        "dimension_rows": [],
    }
    if not jd_rows:
        return empty

    row = jd_rows[0]
    rp = row.get("recruiter_page")
    if isinstance(rp, dict) and (str(rp.get("candidate_summary") or "").strip() or rp.get("dimension_rows")):
        return rp

    profile_s = _parse_score(row.get("Profile score"))
    resume_s = _parse_score(row.get("Resume score"))
    om = max(0.0, min(5.0, round(profile_s / 20.0, 1)))
    dims: list[dict[str, Any]] = []
    raw_m = row.get("skill_matrix")
    if isinstance(raw_m, list):
        for m in raw_m:
            if not isinstance(m, dict):
                continue
            if "Skill Area" in m and (
                m.get("Skill Area") or m.get("JD Requirement") or m.get("Resume Evidence")
            ):
                scell = str(m.get("Score", "") or "")
                n = _parse_score_slash5_from_cell(scell) if scell else None
                if n is None:
                    n = 3
                dims.append(
                    {
                        "skill_area": str(m.get("Skill Area", "") or ""),
                        "jd_requirement": str(m.get("JD Requirement", "") or ""),
                        "resume_evidence": str(m.get("Resume Evidence", "") or ""),
                        "score_out_of_5": n,
                        "match_summary": str(m.get("Match Summary", "") or ""),
                    }
                )
            elif m.get("Skill Category"):
                dims.append(
                    {
                        "skill_area": str(m.get("Skill Category", "") or ""),
                        "jd_requirement": str(m.get("Required Skills (JD)", "") or ""),
                        "resume_evidence": str(m.get("Candidate Skills", "") or ""),
                        "score_out_of_5": _percent_to_star_int(m.get("Match Score (%)", 0)),
                        "match_summary": str(m.get("Remarks", "") or ""),
                    }
                )

    summary = str(row.get("Why select", "") or "").strip() or (
        f"Holistic profile fit about {profile_s:.0f}%; resume keyword coverage about {resume_s:.0f}% vs required JD skills."
    )
    why_not = str(row.get("Why not select", "") or "").strip()
    verdict = [why_not] if why_not else ["Validate fit in screening using the table below."]
    if not dims:
        dims.append(
            {
                "skill_area": "Overall",
                "jd_requirement": "See JD file",
                "resume_evidence": "See resume",
                "score_out_of_5": max(0, min(5, int(round(profile_s / 20.0)))),
                "match_summary": "Legacy export — re-run for full dimension table",
            }
        )

    return {
        "candidate_summary": summary,
        "verdict_lines": verdict,
        "overall_match_out_of_5": om,
        "overall_match_percent_approx": int(round(om / 5.0 * 100)),
        "dimension_rows": dims,
    }


def _score_cell(score: int) -> str:
    n = max(0, min(5, int(score)))
    return f"{n}/5"


def _is_essential_requirement(req: str) -> bool:
    t = str(req or "").strip().lower()
    if not t:
        return False
    essential_markers = (
        "required",
        "must",
        "mandatory",
        "essential",
        "critical",
        "core",
        "key requirement",
        "minimum",
        "strong",
        "expected",
        "10+ years",
        "5+ years",
    )
    return any(m in t for m in essential_markers)


def _sort_dimension_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Sort rows for recruiter readability:
    1) Essential JD requirements first
    2) Higher score first
    3) Stable tie-break on skill_area
    """
    safe_rows = [r for r in rows if isinstance(r, dict)]

    def _score(r: dict[str, Any]) -> int:
        try:
            return max(0, min(5, int(r.get("score_out_of_5", 0))))
        except (TypeError, ValueError):
            return 0

    return sorted(
        safe_rows,
        key=lambda r: (
            0 if _is_essential_requirement(str(r.get("jd_requirement", "") or "")) else 1,
            -_score(r),
            str(r.get("skill_area", "") or "").lower(),
        ),
    )


def _summary_body_flowables(text: str, body_style: ParagraphStyle) -> list[Any]:
    parts = [p.strip() for p in str(text).split("\n\n") if p.strip()]
    if not parts:
        return [Paragraph(_para_html(text), body_style)]
    out: list[Any] = []
    for i, p in enumerate(parts):
        out.append(Paragraph(_para_html(p), body_style))
        if i < len(parts) - 1:
            out.append(Spacer(1, 0.07 * inch))
    return out


def _build_cover_pdf_bytes(
    candidate: dict[str, Any],
    jd_rows: list[dict[str, Any]],
) -> bytes:
    styles = getSampleStyleSheet()
    brand = colors.HexColor("#1e3a5f")

    title_style = ParagraphStyle(
        name="ExecTitle",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=22,
        leading=26,
        spaceAfter=6,
        textColor=brand,
    )
    body = ParagraphStyle(
        name="Body",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=12.5,
        textColor=colors.HexColor("#2d3748"),
    )
    section = ParagraphStyle(
        name="Section",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        spaceBefore=10,
        spaceAfter=6,
        textColor=brand,
    )
    small = ParagraphStyle(
        name="Small",
        parent=styles["Normal"],
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#2d3748"),
    )

    buffer = io.BytesIO()
    _side_margin = 0.75 * inch
    _content_width = letter[0] - 2 * _side_margin
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=_side_margin,
        leftMargin=_side_margin,
        topMargin=0.72 * inch,
        bottomMargin=0.7 * inch,
        onFirstPage=_cover_first_page_canvas,
        onLaterPages=_cover_later_pages_canvas,
    )
    page = _recruiter_page_for_pdf(jd_rows)

    story: list[Any] = []
    story.extend(_logo_header_flowables(_content_width))
    story.append(Paragraph("Recruiter Summary", title_style))
    story.append(HRFlowable(width="100%", thickness=0.75, color=brand, spaceAfter=12, spaceBefore=2))

    story.append(Paragraph("Candidate highlight As per Current Job Description", section))
    story.append(Spacer(1, 0.05 * inch))
    story.extend(_summary_body_flowables(page.get("candidate_summary") or "", body))

    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph("Verdict:", section))
    story.append(Spacer(1, 0.04 * inch))
    for vline in page.get("verdict_lines") or []:
        story.append(Paragraph(_para_html(f"👉 {vline}"), body))
        story.append(Spacer(1, 0.03 * inch))

    om = page.get("overall_match_out_of_5", 0)
    try:
        om_f = float(om)
    except (TypeError, ValueError):
        om_f = 0.0
    pct = page.get("overall_match_percent_approx", int(round(om_f / 5.0 * 100)))
    try:
        pct_i = int(pct)
    except (TypeError, ValueError):
        pct_i = int(round(om_f / 5.0 * 100))
    story.append(Spacer(1, 0.08 * inch))
    story.append(
        Paragraph(
            f"<b>Overall Matching Score:</b> Overall Match: {om_f} / 5 (≈ {pct_i}%)",
            body,
        )
    )

    story.append(Spacer(1, 0.12 * inch))
    headers = ["Skill Area", "JD Requirement", "Resume Evidence", "Score", "Match Summary"]
    table_data: list[list[Any]] = [
        [Paragraph(f"<b>{h.replace('&', '&amp;')}</b>", small) for h in headers]
    ]
    dims = _sort_dimension_rows(page.get("dimension_rows") or [])
    if not dims:
        table_data.append([Paragraph(_para_html("—"), small) for _ in headers])
    else:
        for d in dims:
            if not isinstance(d, dict):
                continue
            sc = d.get("score_out_of_5", 0)
            try:
                sci = int(sc)
            except (TypeError, ValueError):
                sci = 0
            table_data.append(
                [
                    Paragraph(_para_html(str(d.get("skill_area", ""))), small),
                    Paragraph(_para_html(str(d.get("jd_requirement", ""))), small),
                    Paragraph(_para_html(str(d.get("resume_evidence", ""))), small),
                    Paragraph(_para_html(_score_cell(sci)), small),
                    Paragraph(_para_html(str(d.get("match_summary", ""))), small),
                ]
            )

    tw = doc.width
    col_w = [tw * 0.14, tw * 0.22, tw * 0.24, tw * 0.14, tw * 0.26]
    tbl = Table(table_data, colWidths=col_w, repeatRows=1)
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dce6f0")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1e3a5f")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(tbl)
    doc.build(story)
    buffer.seek(0)
    return buffer.read()


def _append_body_pages_from_text(writer: PdfWriter, text: str) -> None:
    """Add plain-text continuation pages when source is not a PDF."""
    styles = getSampleStyleSheet()
    body = ParagraphStyle(name="Body2", parent=styles["Normal"], fontSize=9, leading=11)
    chunk_size = 4500
    for i in range(0, len(text), chunk_size):
        chunk = text[i : i + chunk_size]
        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf,
            pagesize=letter,
            rightMargin=0.75 * inch,
            leftMargin=0.75 * inch,
            topMargin=0.75 * inch,
            bottomMargin=0.75 * inch,
        )
        safe = chunk.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")
        doc.build([Paragraph(safe or "(empty)", body)])
        buf.seek(0)
        writer.append(PdfReader(buf))


def _merge_cover_with_source(cover_pdf: bytes, source_path: Path, writer: PdfWriter) -> None:
    cover_reader = PdfReader(io.BytesIO(cover_pdf))
    for page in cover_reader.pages:
        writer.add_page(page)

    suffix = source_path.suffix.lower()
    if suffix == ".pdf":
        src = PdfReader(str(source_path))
        for page in src.pages:
            writer.add_page(page)
        return

    text = extract_text_from_resume(source_path)
    _append_body_pages_from_text(writer, text)


def export_recruiter_summary_pdfs(
    summary_rows: list[dict[str, Any]],
    resume_targets: list[tuple[Path, str]],
    candidate_by_resume_key: dict[str, dict[str, Any]],
    out_root: Path | None = None,
) -> list[Path]:
    """
    For each resume, prepend a summary page and write a PDF under ``out_root``.

    ``resume_targets`` is ``(source_path, resume_file_label)`` per file. The label must match
    the ``Resume file`` field in ``summary_rows`` (e.g. Streamlit uses the upload filename,
    not the temp path name).

    ``candidate_by_resume_key`` maps ``str(source_path.resolve())`` to the structured candidate.
    """
    base = Path(__file__).resolve().parents[2]
    out_dir = (out_root or (base / DEFAULT_OUTPUT_RELATIVE)).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for rpath, resume_label in resume_targets:
        rp = rpath.resolve()
        key = str(rp)
        candidate = candidate_by_resume_key.get(key)
        if not candidate:
            continue
        jd_rows = [r for r in summary_rows if r.get("Resume file") == resume_label]
        if not jd_rows:
            continue

        cover = _build_cover_pdf_bytes(candidate, jd_rows)
        writer = PdfWriter()
        _merge_cover_with_source(cover, rp, writer)

        stem = Path(resume_label).stem
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in stem)[:80]
        tag = hashlib.sha256(str(rp).encode()).hexdigest()[:8]
        out_path = out_dir / f"{safe}_{tag}_recruiter_summary.pdf"
        with open(out_path, "wb") as f:
            writer.write(f)
        written.append(out_path)

    return written
