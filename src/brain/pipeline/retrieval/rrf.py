from __future__ import annotations

from brain.models import FusedResult, RetrievalHit


def rrf_fuse(*paths: list[RetrievalHit], k: int = 60) -> list[FusedResult]:
    """Fuse ranked retrieval paths with Reciprocal Rank Fusion."""
    page_scores: dict[str, float] = {}
    page_chunks: dict[str, list[RetrievalHit]] = {}

    for hits in paths:
        for hit in hits:
            page_scores[hit.page_slug] = page_scores.get(hit.page_slug, 0.0) + (
                1.0 / (k + hit.rank)
            )
            page_chunks.setdefault(hit.page_slug, []).append(hit)

    fused = [
        FusedResult(
            page_slug=slug,
            chunks=page_chunks[slug],
            rrf_score=score,
            final_rank=0,
        )
        for slug, score in page_scores.items()
    ]
    fused.sort(key=lambda result: (-result.rrf_score, result.page_slug))
    for rank, result in enumerate(fused, start=1):
        result.final_rank = rank
    return fused
