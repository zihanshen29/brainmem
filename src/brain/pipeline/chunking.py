from brain.exceptions import BrainError
from brain.models.embedding import EmbeddingChunk
from brain.models.page import Page
from brain.pages.timeline import parse_entry

STUB_COMPILED_TRUTH = "(stub - waiting for more evidence)"
TEXT_PREVIEW_MAX_CHARS = 200


def split_page_into_chunks(page: Page, max_chars: int) -> list[EmbeddingChunk]:
    """Split a parsed page into stable embedding chunks."""
    if max_chars <= 0:
        raise BrainError("max_chars must be positive")

    chunks: list[EmbeddingChunk] = []
    title = page.frontmatter.title
    page_slug = page.frontmatter.slug

    compiled_truth = page.compiled_truth.strip()
    if compiled_truth and compiled_truth != STUB_COMPILED_TRUTH:
        text = _truncate(compiled_truth, max_chars)
        chunks.append(
            EmbeddingChunk(
                page_slug=page_slug,
                chunk_kind="compiled_truth",
                chunk_id="main",
                text=f"{title}\n\n{text}",
                text_preview=text[:TEXT_PREVIEW_MAX_CHARS],
            )
        )

    for raw_entry in page.timeline:
        entry = parse_entry(raw_entry)
        description = _truncate(entry.description, max_chars)
        chunks.append(
            EmbeddingChunk(
                page_slug=page_slug,
                chunk_kind="timeline_entry",
                chunk_id=entry.event_id,
                text=f"{entry.date} - {title}: {description}",
                text_preview=description[:TEXT_PREVIEW_MAX_CHARS],
            )
        )

    return chunks


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars]
