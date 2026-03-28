"""Load job description text from .txt or .docx."""

from pathlib import Path


def load_jd(file_path: str | Path) -> str:
    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix == ".docx":
        from docx import Document

        doc = Document(path)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    return path.read_text(encoding="utf-8", errors="replace")
