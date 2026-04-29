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
            "Return exactly one JSON object with these top-level keys:",
            "- entities: array of objects with keys name, type, confidence, metadata.",
            "- facts: array of objects with keys subject, predicate, object, object_type, valid_from, valid_to, source_event, source_ref, confidence.",
            "- timeline_summary: one concise sentence summarizing the durable event.",
            "- suggested_page_type: one of entity, project, concept, event, experience, conversation, or null.",
            "Do not return a top-level signals key.",
            "Entity type must be one of person, org, concept, project, event, place, or null.",
            "Entity metadata must always be a JSON object; use {} when there is no metadata, never null.",
            "Fact object_type must be one of entity, literal, date, number.",
            "Do not use semantic labels such as person, org, place, location, concept, or project as fact object_type; use entity when the object is an entity-like thing.",
            "For each fact, subject should be a lowercase ASCII slug when possible.",
            "If the input contains a Hint JSON with source_event or source_ref, copy those exact values into every fact.",
            "If no durable facts are present, return an empty facts array but still return entities and timeline_summary.",
            "Use null for unknown optional values; do not omit required keys.",
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


def build_question_answer_prompt(query: str, pages: list[dict[str, Any]]) -> str:
    """Build the prompt for answering a question from retrieved brain pages."""
    payload = {
        "query": query,
        "pages": pages,
    }
    return "\n".join(
        [
            "Answer the user's question using only the provided brain pages.",
            "If the pages do not contain enough information, say you do not know.",
            "Do not invent facts or use outside knowledge.",
            JSON_ONLY_INSTRUCTIONS,
            "Return an object with keys: answer, sources.",
            "sources must be a list of page slugs used in the answer.",
            "",
            "Question evidence JSON:",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ]
    )


def build_promote_chat_prompt(
    raw_text: str,
    title_hint: str | None = None,
    slug_hint: str | None = None,
) -> str:
    """Build the prompt for turning raw AI chat text into a conversation page draft."""
    payload = {
        "raw_text": raw_text,
        "title_hint": title_hint,
        "slug_hint": slug_hint,
    }
    return "\n".join(
        [
            "Promote the raw AI chat into a durable conversation memory page draft.",
            "Use the hints when they are provided, but do not invent unsupported facts.",
            JSON_ONLY_INSTRUCTIONS,
            "Return an object with keys: title, compiled_truth, timeline_description.",
            "title must be concise and human-readable.",
            "compiled_truth must summarize the stable useful content from the chat.",
            "timeline_description must be one single-line sentence describing the promoted chat.",
            "",
            "Conversation evidence JSON:",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ]
    )
