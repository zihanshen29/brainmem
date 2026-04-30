from brain.models import EmbeddingChunk
from brain.pipeline.retrieval import bm25_search


def chunk(slug: str, text: str) -> EmbeddingChunk:
    return EmbeddingChunk(
        page_slug=slug,
        chunk_kind="compiled_truth",
        chunk_id="main",
        text=text,
        text_preview=text[:20],
    )


def test_bm25_search_ranks_expected_pages_in_top_three() -> None:
    chunks = [
        chunk("cv-coursework", "Computer vision coursework uses segmentation and object detection."),
        chunk("transformer-paper", "Transformer notes about attention and sequence modeling."),
        chunk("zhang-review", "Zhang reviewed the computer vision baseline and detection report."),
        chunk("cooking", "Dinner notes with vegetables and rice."),
    ]

    hits = bm25_search(chunks, "segmentation detection reviewed attention", top=3)

    assert [hit.page_slug for hit in hits] == [
        "cv-coursework",
        "zhang-review",
        "transformer-paper",
    ]
    assert [hit.rank for hit in hits] == [1, 2, 3]
    assert all(hit.path == "keyword" for hit in hits)
