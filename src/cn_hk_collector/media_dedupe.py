from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

SIMHASH_BITS = 64
SIMHASH_DUPLICATE_THRESHOLD = 0.90


def normalize_content(text: str | None) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    value = re.sub(r"[^\w\u4e00-\u9fff]+", "", value)
    return value


def content_hash(text: str | None) -> str:
    normalized = normalize_content(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def _tokens(text: str) -> List[str]:
    normalized = normalize_content(text)
    if not normalized:
        return []
    latin_tokens = re.findall(r"[a-z0-9_]{2,}", normalized)
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", normalized)
    cjk_bigrams = ["".join(cjk_chars[i : i + 2]) for i in range(max(0, len(cjk_chars) - 1))]
    return latin_tokens + cjk_bigrams


def simhash_value(text: str | None, bits: int = SIMHASH_BITS) -> int:
    weights = [0] * bits
    for token in _tokens(str(text or "")):
        digest = int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16)
        for bit in range(bits):
            weights[bit] += 1 if digest & (1 << bit) else -1
    result = 0
    for bit, weight in enumerate(weights):
        if weight > 0:
            result |= 1 << bit
    return result


def simhash_similarity(left: int, right: int, bits: int = SIMHASH_BITS) -> float:
    distance = (left ^ right).bit_count()
    return 1.0 - (distance / bits)


def _parse_time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return datetime.max.replace(tzinfo=timezone.utc)


def _record_text(record: Dict[str, Any]) -> str:
    content = str(record.get("content") or "").strip()
    title = str(record.get("title") or "").strip()
    return f"{title} {content}".strip()


def _content_identity_text(record: Dict[str, Any]) -> str:
    return str(record.get("content") or record.get("title") or "").strip()


def _is_duplicate(left: Dict[str, Any], right: Dict[str, Any], threshold: float) -> bool:
    if left.get("url") and right.get("url") and left.get("url") == right.get("url"):
        return True
    if left.get("content_hash") and left.get("content_hash") == right.get("content_hash"):
        return True
    left_simhash = int(left.get("simhash_int") or 0)
    right_simhash = int(right.get("simhash_int") or 0)
    if left_simhash and right_simhash:
        return simhash_similarity(left_simhash, right_simhash) >= threshold
    return False


def dedupe_media_records(records: Iterable[Dict[str, Any]], threshold: float = SIMHASH_DUPLICATE_THRESHOLD) -> List[Dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for record in records:
        item = dict(record)
        text = _record_text(item)
        item["content_hash"] = content_hash(_content_identity_text(item))
        item["simhash_int"] = simhash_value(text)
        item["simhash"] = f"{item['simhash_int']:016x}" if item["simhash_int"] else None
        prepared.append(item)

    groups: list[list[dict[str, Any]]] = []
    for item in prepared:
        matched_group = None
        for group in groups:
            if any(_is_duplicate(item, existing, threshold) for existing in group):
                matched_group = group
                break
        if matched_group is None:
            groups.append([item])
        else:
            matched_group.append(item)

    representatives: list[dict[str, Any]] = []
    for group in groups:
        group.sort(key=lambda rec: (_parse_time(rec.get("published_at")), str(rec.get("url") or "")))
        representative = dict(group[0])
        urls = [str(rec.get("url") or "").strip() for rec in group if rec.get("url")]
        representative["duplicate_count"] = len(group)
        representative["duplicate_urls"] = list(dict.fromkeys(urls))
        representative.pop("simhash_int", None)
        representatives.append(representative)

    representatives.sort(key=lambda rec: _parse_time(rec.get("published_at")))
    return representatives
