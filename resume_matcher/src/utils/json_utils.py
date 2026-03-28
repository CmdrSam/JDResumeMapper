import json
import re
from typing import Any


def parse_llm_json(text: str) -> Any:
    """Parse JSON from an LLM reply, stripping optional markdown fences."""
    s = text.strip()
    fence = re.match(r"^```(?:json)?\s*\n?", s, re.IGNORECASE)
    if fence:
        s = s[fence.end() :]
        s = re.sub(r"\n?```\s*$", "", s, flags=re.IGNORECASE)
    s = s.strip()
    return json.loads(s)
