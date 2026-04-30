from __future__ import annotations

import re
from typing import Literal

QueryClass = Literal["structured", "open_ended"]

STRUCTURED_PATTERNS = [
    re.compile(r"(?:\d{4}\s*年|\d{4}[-/]\d{1,2}|Q[1-4]|第[一二三四1234]季度|今天|昨天|上周|本周|上个月|这个月)"),
    re.compile(r"(?:谁|什么人|哪位|哪个人).{0,12}(?:是|在|负责|参与|提到|帮|做)"),
    re.compile(r"(?:什么时候|何时|哪天|日期|时间)"),
    re.compile(r"^\s*(?:列出|列举|有哪些|有什么|哪些|给我列|帮我列)"),
    re.compile(r"(?:多少|几个|次数|数量|统计|汇总)"),
    re.compile(r"\b(?:who|when|list|which|what people|how many|show me)\b", re.IGNORECASE),
]


def classify_query(query: str) -> QueryClass:
    """Classify query shape with deterministic rules only."""
    normalized = query.strip()
    if not normalized:
        return "open_ended"
    if any(pattern.search(normalized) for pattern in STRUCTURED_PATTERNS):
        return "structured"
    return "open_ended"
