"""Extract plain text from resume files (PDF via pdfplumber, DOCX, plain text)."""

from pathlib import Path

import pdfplumber


def extract_text_from_pdf(file_path: str | Path) -> str:
    text = ""
    path = Path(file_path)
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    return text


def extract_text_from_docx(file_path: str | Path) -> str:
    from docx import Document

    doc = Document(file_path)
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def extract_text_from_resume(file_path: str | Path) -> str:
    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_text_from_pdf(path)
    if suffix == ".docx":
        return extract_text_from_docx(path)
    return path.read_text(encoding="utf-8", errors="replace")
