"""Dedupe LLM resume extraction by file content hash (same bytes → one call)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from src.extractor.resume_extractor import extract_resume_with_llm
from src.llm.client import LLMClient
from src.parser.resume_parser import extract_text_from_resume


def build_resume_candidate_map(llm: LLMClient, resume_paths: list[Path]) -> dict[str, dict[str, Any]]:
    """
    Map ``str(path.resolve())`` → structured candidate dict.
    Identical file content (any path) shares a single ``extract_resume_with_llm`` call.
    """
    by_hash: dict[str, dict[str, Any]] = {}
    result: dict[str, dict[str, Any]] = {}
    for p in resume_paths:
        rp = p.resolve()
        key = str(rp)
        digest = hashlib.sha256(rp.read_bytes()).hexdigest()
        if digest not in by_hash:
            text = extract_text_from_resume(rp)
            by_hash[digest] = extract_resume_with_llm(llm, text)
        result[key] = by_hash[digest]
    return result
