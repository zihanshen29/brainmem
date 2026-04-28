import json
from typing import Any

JSON_ONLY_INSTRUCTIONS = (
    "Return only valid JSON. Do not include markdown fences, commentary, or trailing text."
)


def build_signal_extraction_prompt(text: str) -> str:
    """Build the prompt for extracting structured signals from raw text."""
    return "\n".join(
        [
            "Extract memory signals from the input text.",
            JSON_ONLY_INSTRUCTIONS,
            "The JSON must match brain.pipeline.signal_detect.SignalExtraction.",
            "",
            "Input text:",
            text,
        ]
    )


def build_conflict_prompt(old_fact: dict[str, Any], new_fact: dict[str, Any]) -> str:
    """Build the prompt for judging whether a candidate fact conflicts with an old fact."""
    return "\n".join(
        [
            "Judge whether the new candidate fact conflicts with the existing fact.",
            JSON_ONLY_INSTRUCTIONS,
            "Return an object with keys: is_conflict, new_supersedes_old, reason, confidence.",
            "",
            "Existing fact JSON:",
            json.dumps(old_fact, ensure_ascii=False, sort_keys=True),
            "",
            "New candidate fact JSON:",
            json.dumps(new_fact, ensure_ascii=False, sort_keys=True),
        ]
    )


def build_compiled_truth_prompt(
    timeline: list[dict[str, Any]],
    current_truth: str | None,
) -> str:
    """Build the prompt for rewriting a page's compiled truth from timeline entries."""
    payload = {
        "current_truth": current_truth,
        "timeline": timeline,
    }
    return "\n".join(
        [
            "Rewrite the compiled truth for this memory page from the timeline.",
            "Keep it concise, factual, and consistent with the newest timeline evidence.",
            JSON_ONLY_INSTRUCTIONS,
            "Return an object with exactly one key: compiled_truth.",
            "",
            "Page evidence JSON:",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ]
    )
