"""Derive headline match scores from displayed recruiter dimension rows."""

from __future__ import annotations

from typing import Any


def overall_match_from_dimension_rows(rows: list[Any]) -> tuple[float, int] | None:
    """
    Mean of ``score_out_of_5`` over the dimension rows actually shown (e.g. after dropping ≤2).

    Returns ``(overall 0–5 with one decimal, percent 0–100)`` or ``None`` if there are no rows.
    """
    use = [r for r in rows if isinstance(r, dict)]
    if not use:
        return None
    scores: list[int] = []
    for d in use:
        try:
            scores.append(max(0, min(5, int(d.get("score_out_of_5", 0)))))
        except (TypeError, ValueError):
            scores.append(0)
    if not scores:
        return None
    om = sum(scores) / len(scores)
    om = max(0.0, min(5.0, round(om, 1)))
    pct = max(0, min(100, int(round(om / 5.0 * 100))))
    return om, pct
