import json
import re
from typing import Any


def _strip_markdown_fences(text: str) -> str:
    s = text.strip()
    if not s:
        return s
    if s.startswith("```"):
        s = re.sub(r"^```(?:json|JSON)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


def _normalize_unicode_quotes(s: str) -> str:
    """Map common curly/smart quotes to ASCII (outside-only risk is acceptable for LLM JSON)."""
    for a, b in (
        ("\u201c", '"'),
        ("\u201d", '"'),
        ("\u2018", "'"),
        ("\u2019", "'"),
        ("\u00ab", '"'),
        ("\u00bb", '"'),
    ):
        s = s.replace(a, b)
    return s


def _remove_trailing_commas(s: str) -> str:
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r",(\s*[}\]])", r"\1", s)
    return s


def parse_llm_json(text: str) -> Any:
    """
    Parse JSON from an LLM reply: markdown fences, BOM, smart quotes, trailing commas,
    leading preamble, then optional json-repair for common LLM syntax errors.
    """
    if text is None:
        raise json.JSONDecodeError("empty", "", 0)
    s = _strip_markdown_fences(str(text))
    s = s.replace("\ufeff", "").strip()
    if not s:
        raise json.JSONDecodeError("empty after strip", "", 0)

    s = _normalize_unicode_quotes(s)

    variants: list[str] = [s, _remove_trailing_commas(s)]
    for start_ch in ("{", "["):
        pos = s.find(start_ch)
        if pos >= 0:
            tail = s[pos:]
            variants.append(tail)
            variants.append(_remove_trailing_commas(tail))

    seen: set[str] = set()
    unique_variants: list[str] = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            unique_variants.append(v)

    last_err: json.JSONDecodeError | None = None
    for cand in unique_variants:
        try:
            return json.loads(cand)
        except json.JSONDecodeError as e:
            last_err = e
        try:
            dec = json.JSONDecoder()
            for start_ch in ("{", "["):
                pos = cand.find(start_ch)
                if pos >= 0:
                    obj, _end = dec.raw_decode(cand, pos)
                    return obj
        except json.JSONDecodeError as e:
            last_err = e

    try:
        import json_repair

        for cand in unique_variants:
            try:
                return json_repair.loads(cand)
            except Exception:
                continue
    except ImportError:
        pass

    if last_err is not None:
        raise last_err
    raise json.JSONDecodeError("Could not parse LLM JSON", s, 0)
