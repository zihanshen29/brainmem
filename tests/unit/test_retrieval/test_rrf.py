import pytest

from brain.models import RetrievalHit
from brain.pipeline.retrieval import rrf_fuse


def hit(slug: str, rank: int, path: str) -> RetrievalHit:
    return RetrievalHit(
        page_slug=slug,
        chunk_kind="compiled_truth",
        chunk_id="main",
        score=1.0,
        rank=rank,
        path=path,
    )


def test_rrf_fuse_aggregates_by_page_and_assigns_final_rank() -> None:
    fused = rrf_fuse(
        [hit("alpha", 1, "vector"), hit("beta", 2, "vector")],
        [hit("beta", 1, "keyword"), hit("alpha", 3, "keyword")],
        [hit("gamma", 1, "sql")],
        k=60,
    )

    assert [result.page_slug for result in fused] == ["beta", "alpha", "gamma"]
    assert [result.final_rank for result in fused] == [1, 2, 3]
    assert fused[0].rrf_score == pytest.approx((1 / 62) + (1 / 61))
    assert len(fused[0].chunks) == 2


def test_rrf_fuse_sorts_ties_by_slug() -> None:
    fused = rrf_fuse([hit("zulu", 1, "vector")], [hit("alpha", 1, "keyword")], k=60)

    assert [result.page_slug for result in fused] == ["alpha", "zulu"]
