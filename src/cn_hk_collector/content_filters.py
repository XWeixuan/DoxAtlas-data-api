from __future__ import annotations

from typing import Any, Iterable, List


SOCIAL_CONTENT_MAX_CHARS = 250
MEDIA_CONTENT_MAX_CHARS = 5000


def _content_length(record: dict[str, Any]) -> int:
    return len(str(record.get("content") or "").strip())


def apply_length_relevance_filter(records: Iterable[dict[str, Any]], *, source_type: str) -> List[dict[str, Any]]:
    max_chars = SOCIAL_CONTENT_MAX_CHARS if source_type == "social" else MEDIA_CONTENT_MAX_CHARS
    reason = f"length_filter:content_chars>{max_chars}"
    filtered: list[dict[str, Any]] = []
    for record in records:
        item = dict(record)
        if _content_length(item) > max_chars:
            item["is_content_relevant"] = False
            item["content_relevance_reason"] = reason
        filtered.append(item)
    return filtered
