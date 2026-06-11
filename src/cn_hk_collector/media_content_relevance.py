from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional


NEGATIVE_PATTERNS = (
    "多股上涨",
    "多股下跌",
    "板块走强",
    "龙虎榜",
    "早盘异动",
    "午后异动",
    "资金流入",
    "资金流出",
    "资金净流入",
    "资金净流出",
    "特大单",
)


@dataclass(frozen=True)
class ContentRelevanceDecision:
    is_content_relevant: bool
    content_relevance_reason: str
    target_mentions: int
    other_unique_tickers: int
    other_ticker_mentions: int
    total_ticker_mentions: int
    target_share: float
    target_in_title: bool
    negative_hit: bool


@dataclass(frozen=True)
class ContentRelevanceStats:
    attempted: int = 0
    valid: int = 0
    invalid: int = 0
    written: int = 0


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_code(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z]", "", str(value or "")).upper()
    if cleaned.endswith("HK"):
        cleaned = cleaned[:-2]
    if cleaned.endswith(("SH", "SZ", "SS")):
        cleaned = cleaned[:-2]
    if cleaned.isdigit():
        return cleaned.lstrip("0") or "0"
    return cleaned


def _target_aliases(ticker: str, aliases: Optional[Iterable[str]] = None) -> list[str]:
    raw_aliases = [ticker, *(aliases or [])]
    normalized: list[str] = []
    for alias in raw_aliases:
        value = _normalize_text(alias)
        if not value:
            continue
        normalized.append(value)
        code = _normalize_code(value)
        if code and code != value:
            normalized.append(code)
        if code.isdigit() and len(code) <= 5:
            normalized.append(code.zfill(4))
            normalized.append(code.zfill(5))
    return list(dict.fromkeys(normalized))


def _count_alias_mentions(text: str, aliases: Iterable[str]) -> int:
    count = 0
    for alias in aliases:
        if not alias:
            continue
        if re.fullmatch(r"[A-Za-z0-9.:-]+", alias):
            count += len(re.findall(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", text, flags=re.I))
        else:
            count += text.count(alias)
    return count


def _extract_stock_codes(text: str) -> list[str]:
    codes: list[str] = []
    for match in re.finditer(r"(?<!\d)(?:[03468]\d{5})(?!\d)", text):
        codes.append(_normalize_code(match.group(0)))
    hk_patterns = (
        r"(?:HK|港股|代码|股份代号|股票代码)\s*[:：]?\s*(\d{4,5})(?!\d)",
        r"(?<!\d)(\d{4,5})\s*\.HK\b",
        r"[（(](\d{4,5})[）)]",
    )
    for pattern in hk_patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            raw_code = match.group(1)
            if re.fullmatch(r"(?:19|20)\d{2}", raw_code):
                continue
            codes.append(_normalize_code(raw_code))
    return [code for code in codes if code]


def evaluate_content_relevance(
    record: dict[str, Any],
    target_ticker: str,
    *,
    target_aliases: Optional[Iterable[str]] = None,
    negative_patterns: Iterable[str] = NEGATIVE_PATTERNS,
) -> ContentRelevanceDecision:
    title = _normalize_text(record.get("title"))
    content = _normalize_text(record.get("content") or record.get("summary"))
    full_text = f"{title} {content}".strip()
    aliases = _target_aliases(target_ticker, target_aliases)
    target_codes = {_normalize_code(alias) for alias in aliases if _normalize_code(alias)}

    target_mentions = _count_alias_mentions(full_text, aliases)
    target_in_title = _count_alias_mentions(title, aliases) > 0
    negative_hit = any(pattern and pattern in full_text for pattern in negative_patterns)

    stock_codes = _extract_stock_codes(full_text)
    other_codes = [code for code in stock_codes if code not in target_codes]
    other_unique = len(set(other_codes))
    other_mentions = len(other_codes)
    total = target_mentions + other_mentions
    target_share = (target_mentions / total) if total else 0.0

    if target_mentions == 0:
        return ContentRelevanceDecision(True, "target_not_found_kept", target_mentions, other_unique, other_mentions, total, target_share, target_in_title, negative_hit)
    if negative_hit:
        return ContentRelevanceDecision(False, "negative_pattern_hit", target_mentions, other_unique, other_mentions, total, target_share, target_in_title, negative_hit)
    if target_in_title and target_mentions >= 2:
        return ContentRelevanceDecision(True, "target_in_title_and_repeated", target_mentions, other_unique, other_mentions, total, target_share, target_in_title, negative_hit)
    if other_unique <= 1 and target_mentions >= 2:
        return ContentRelevanceDecision(True, "target_dominant_with_few_other_tickers", target_mentions, other_unique, other_mentions, total, target_share, target_in_title, negative_hit)
    if other_unique >= 5:
        return ContentRelevanceDecision(False, "too_many_other_tickers", target_mentions, other_unique, other_mentions, total, target_share, target_in_title, negative_hit)
    if target_share <= 0.35 and total > 7:
        return ContentRelevanceDecision(False, "low_target_share_with_many_mentions", target_mentions, other_unique, other_mentions, total, target_share, target_in_title, negative_hit)
    return ContentRelevanceDecision(True, "edge_case_kept_to_avoid_false_negative", target_mentions, other_unique, other_mentions, total, target_share, target_in_title, negative_hit)


def _sql_text(value: Optional[str]) -> str:
    if value is None:
        return "NULL"
    cleaned = str(value).replace("\x00", "")
    return "'" + cleaned.replace("'", "''") + "'"


def _sql_bool(value: bool) -> str:
    return "TRUE" if value else "FALSE"


def annotate_media_content_relevance(
    records: Iterable[dict[str, Any]],
    *,
    target_ticker: str,
    market: str,
    target_aliases: Optional[Iterable[str]] = None,
    db_conn: Any = None,
    batch_size: int = 200,
) -> ContentRelevanceStats:
    rows: list[dict[str, Any]] = []
    valid = 0
    invalid = 0
    for record in records:
        url = _normalize_text(record.get("url"))
        if not url:
            continue
        if record.get("is_content_relevant") is False:
            is_relevant = False
            reason = _normalize_text(record.get("content_relevance_reason")) or "preclassified_irrelevant"
        else:
            decision = evaluate_content_relevance(record, target_ticker, target_aliases=target_aliases)
            is_relevant = decision.is_content_relevant
            reason = decision.content_relevance_reason
        if is_relevant:
            valid += 1
        else:
            invalid += 1
        rows.append(
            {
                "market": market,
                "ticker": target_ticker,
                "url": url,
                "is_content_relevant": is_relevant,
                "content_relevance_reason": reason,
            }
        )

    written = _update_relevance_rows(db_conn, rows, batch_size)
    return ContentRelevanceStats(attempted=len(rows), valid=valid, invalid=invalid, written=written)


def _update_relevance_rows(db_conn: Any, rows: list[dict[str, Any]], batch_size: int) -> int:
    if not db_conn or not rows:
        return 0
    written = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        with db_conn.cursor() as cur:
            cur.executemany(
                """
                UPDATE raw_media
                SET is_content_relevant = %s,
                    content_relevance_reason = %s,
                    updated_at = now()
                WHERE market = %s
                  AND ticker = %s
                  AND url = %s
                """,
                [
                    (
                        bool(row["is_content_relevant"]),
                        row["content_relevance_reason"],
                        row["market"],
                        row["ticker"],
                        row["url"],
                    )
                    for row in chunk
                ],
            )
        db_conn.commit()
        written += len(chunk)
    return written
