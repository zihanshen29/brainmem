"""Token estimates that work without downloading a tokenizer table."""

from __future__ import annotations

import math
import re

_CJK = re.compile(r"[一-鿿]")


def approximate_tokens(text: str) -> int:
    """One token per Han character and about 3.5 characters per token otherwise."""
    if not text:
        return 0
    cjk_chars = len(_CJK.findall(text))
    return max(1, cjk_chars + math.ceil((len(text) - cjk_chars) / 3.5))
