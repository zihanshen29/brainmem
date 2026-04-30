from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence

from brain.models import EmbeddingChunk, RetrievalHit

TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def bm25_search(
    chunks: Sequence[EmbeddingChunk],
    query: str,
    top: int = 50,
) -> list[RetrievalHit]:
    """Rank full chunk text with BM25 and return retrieval hits."""
    if top <= 0 or not chunks:
        return []

    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    corpus = [tokenize(chunk.text) for chunk in chunks]
    scores = _bm25_scores(corpus, query_tokens)
    query_unique = set(query_tokens)
    ranked = [
        (chunk, float(score), len(query_unique & set(corpus[index])), index)
        for index, (chunk, score) in enumerate(zip(chunks, scores, strict=True))
        if score > 0
    ]
    ranked.sort(key=lambda item: (-item[2], -item[1], item[0].page_slug, item[3]))

    return [
        RetrievalHit(
            page_slug=chunk.page_slug,
            chunk_kind=chunk.chunk_kind,
            chunk_id=chunk.chunk_id,
            score=score,
            rank=rank,
            path="keyword",
        )
        for rank, (chunk, score, _, _) in enumerate(ranked[:top], start=1)
    ]


def tokenize(text: str) -> list[str]:
    """Tokenize mixed Chinese and English text for retrieval."""
    normalized = text.strip().lower()
    if not normalized:
        return []

    tokens = TOKEN_RE.findall(normalized)
    if CJK_RE.search(normalized):
        try:
            import jieba

            tokens.extend(token.strip() for token in jieba.lcut(normalized) if token.strip())
        except ImportError:
            tokens.extend(char for char in normalized if CJK_RE.match(char))
    return tokens


def _bm25_scores(corpus: list[list[str]], query_tokens: list[str]) -> list[float]:
    try:
        from rank_bm25 import BM25Okapi

        return list(BM25Okapi(corpus).get_scores(query_tokens))
    except ImportError:
        return _fallback_bm25_scores(corpus, query_tokens)


def _fallback_bm25_scores(corpus: list[list[str]], query_tokens: list[str]) -> list[float]:
    doc_count = len(corpus)
    avgdl = sum(len(doc) for doc in corpus) / doc_count if doc_count else 0.0
    doc_freq = Counter(token for token in set(query_tokens) for doc in corpus if token in set(doc))
    counters = [Counter(doc) for doc in corpus]
    k1 = 1.5
    b = 0.75
    scores: list[float] = []
    for doc, counter in zip(corpus, counters, strict=True):
        score = 0.0
        doc_len = len(doc)
        for token in query_tokens:
            freq = counter[token]
            if freq == 0:
                continue
            idf = math.log(1 + (doc_count - doc_freq[token] + 0.5) / (doc_freq[token] + 0.5))
            denom = freq + k1 * (1 - b + b * doc_len / avgdl) if avgdl else freq + k1
            score += idf * (freq * (k1 + 1) / denom)
        scores.append(score)
    return scores
