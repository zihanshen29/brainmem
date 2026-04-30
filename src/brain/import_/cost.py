from collections import Counter
from collections.abc import Iterable

from brain.import_.discovery import DiscoveredFile
from brain.models.import_job import CostEstimate, ImportFileKind

DEFAULT_EXTRACTION_UNIT_COST_PER_1M = 3.0
DEFAULT_EMBEDDING_UNIT_COST_PER_1M = 0.02
AVG_OUTPUT_TOKENS_PER_FILE = 500
AVG_EMBEDDING_TOKENS_PER_FILE = 1000


def cost_estimate(files: Iterable[DiscoveredFile]) -> CostEstimate:
    """Estimate import cost locally without LLM/API calls."""
    file_list = list(files)
    by_kind: Counter[ImportFileKind] = Counter(file.kind for file in file_list)
    extraction_tokens = sum(_estimate_file_tokens(file) for file in file_list) + (
        len(file_list) * AVG_OUTPUT_TOKENS_PER_FILE
    )
    embedding_tokens = len(file_list) * AVG_EMBEDDING_TOKENS_PER_FILE
    extraction_usd = extraction_tokens / 1_000_000 * DEFAULT_EXTRACTION_UNIT_COST_PER_1M
    embedding_usd = embedding_tokens / 1_000_000 * DEFAULT_EMBEDDING_UNIT_COST_PER_1M
    return CostEstimate(
        total_files=len(file_list),
        by_kind=dict(by_kind),
        estimated_extraction_tokens=extraction_tokens,
        estimated_embedding_tokens=embedding_tokens,
        estimated_extraction_usd=extraction_usd,
        estimated_embedding_usd=embedding_usd,
        estimated_total_usd=extraction_usd + embedding_usd,
    )


def _estimate_file_tokens(file: DiscoveredFile) -> int:
    try:
        size = file.path.stat().st_size
    except OSError:
        size = 0
    return max(1, (size + 3) // 4)
