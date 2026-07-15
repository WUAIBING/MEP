from __future__ import annotations

import re

PARTIAL_DIFF_CAVEAT_PATTERNS = (
    r"\bnot fully shown(?:\s+in\s+the\s+diff)?\b(?:[.:;,])?",
    r"\bpartial diff\b(?:[.:;,])?",
    r"\bpartially shown\b(?:[.:;,])?",
    r"\bwithout the full (?:diff|patch)\b(?:[.:;,])?",
)


def has_partial_diff_caveat(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    return any(re.search(pattern, lowered) for pattern in PARTIAL_DIFF_CAVEAT_PATTERNS)
