from __future__ import annotations

import hashlib
import math
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo


DEFAULT_SOCIAL_ANALYSIS_QUOTA_PER_7D = 3000
MIN_SHORT_TEXT_CJK_CHARS = 8
HIGH_QUALITY_SHARE = 0.6

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]+")
_ELLIPSIS_RE = re.compile(r"(\.\.\.|…|全文|展开|点击查看)")
_STOCK_MARK_RE = re.compile(r"\$?[\u4e00-\u9fffA-Za-z]{0,12}\(?[A-Za-z]{0,3}\d{4,6}\)?\$?", re.I)

_SPAM_PATTERNS = (
    "加群",
    "进群",
    "带单",
    "荐股",
    "老师",
    "直播间",
    "VX",
    "微信",
    "公众号",
    "领取",
    "免费诊股",
    "稳赚",
    "私信",
    "合作",
)

_EVENT_KEYWORDS = (
    "公告",
    "业绩",
    "利润",
    "营收",
    "分红",
    "回购",
    "增持",
    "减持",
    "并购",
    "重组",
    "中标",
    "合同",
    "订单",
    "异动",
    "监管",
    "停牌",
    "复牌",
    "出关",
    "利好",
    "利空",
    "装机",
    "电力",
    "电价",
    "煤价",
    "发电量",
    "新能源",
    "火电",
    "资产",
    "负债",
    "融资",
    "评级",
    "研报",
    "政策",
)

_REASONING_KEYWORDS = (
    "因为",
    "由于",
    "导致",
    "所以",
    "预计",
    "预期",
    "同比",
    "环比",
    "估值",
    "逻辑",
    "兑现",
    "影响",
    "催化",
    "风险",
    "基本面",
    "资金",
    "机构",
    "市场",
)

_MARKET_SIGNAL_KEYWORDS = (
    "涨停",
    "跌停",
    "拉升",
    "砸盘",
    "封板",
    "开板",
    "放量",
    "缩量",
    "低吸",
    "高抛",
    "减仓",
    "加仓",
    "主力",
    "洗盘",
)


def social_analysis_quota_per_7d() -> int:
    try:
        return max(1, int(os.environ.get("GUBA_SOCIAL_ANALYSIS_QUOTA_PER_7D", str(DEFAULT_SOCIAL_ANALYSIS_QUOTA_PER_7D))))
    except ValueError:
        return DEFAULT_SOCIAL_ANALYSIS_QUOTA_PER_7D


def _normalize_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _combined_text(record: dict[str, Any]) -> str:
    parts = [
        _normalize_text(record.get("title")),
        _normalize_text(record.get("summary")),
        _normalize_text(record.get("content")),
    ]
    return " ".join(dict.fromkeys(part for part in parts if part))


def _cjk_count(text: str) -> int:
    return len(_CJK_RE.findall(text))


def _content_chars(text: str) -> int:
    return len("".join(_WORD_RE.findall(text)))


def _contains_any(text: str, terms: Iterable[str]) -> bool:
    lower = text.lower()
    return any(term.lower() in lower for term in terms)


def _engagement(record: dict[str, Any]) -> int:
    total = 0
    for key in ("social_comment_count", "social_read_count", "social_like_count", "social_forward_count"):
        try:
            total += max(0, int(record.get(key) or 0))
        except (TypeError, ValueError):
            continue
    return total


def _is_stock_marker_only(text: str) -> bool:
    compact = re.sub(r"[\s,，。.!！?？:：;；、()（）\[\]【】$]+", "", text)
    if not compact:
        return True
    stripped = _STOCK_MARK_RE.sub("", compact)
    return not stripped


def _is_low_information_short(text: str, cjk_chars: int) -> bool:
    if cjk_chars >= MIN_SHORT_TEXT_CJK_CHARS:
        return False
    if _contains_any(text, _EVENT_KEYWORDS) or _contains_any(text, _MARKET_SIGNAL_KEYWORDS):
        return False
    return True


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _window_quota(window_start: Any, window_end: Any, quota_per_7d: int) -> int:
    start = _parse_dt(window_start)
    end = _parse_dt(window_end)
    if not start or not end or end <= start:
        return quota_per_7d
    seconds = (end - start).total_seconds()
    return max(1, math.ceil(quota_per_7d * seconds / (7 * 24 * 3600)))


def _day_bucket(record: dict[str, Any]) -> str:
    parsed = _parse_dt(record.get("published_at"))
    if not parsed:
        return "unknown"
    return parsed.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _stable_hash(value: str) -> int:
    digest = hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()
    return int(digest[:12], 16)


def evaluate_social_quality(record: dict[str, Any]) -> dict[str, Any]:
    text = _combined_text(record)
    cjk_chars = _cjk_count(text)
    chars = _content_chars(text)
    reasons: list[str] = []
    score = 0

    if not text:
        return {
            "is_drop": True,
            "quality_score": 0,
            "quality_tier": "drop",
            "quality_reasons": ["empty_text"],
            "body_quality": "empty",
            "content_chars": 0,
            "detail_required": False,
        }

    if _is_stock_marker_only(text):
        return {
            "is_drop": True,
            "quality_score": 0,
            "quality_tier": "drop",
            "quality_reasons": ["stock_marker_only"],
            "body_quality": "list_complete_short",
            "content_chars": chars,
            "detail_required": False,
        }

    if _contains_any(text, _SPAM_PATTERNS):
        return {
            "is_drop": True,
            "quality_score": 0,
            "quality_tier": "drop",
            "quality_reasons": ["spam_or_leadgen"],
            "body_quality": "spam",
            "content_chars": chars,
            "detail_required": False,
        }

    has_event = _contains_any(text, _EVENT_KEYWORDS)
    has_reasoning = _contains_any(text, _REASONING_KEYWORDS)
    has_market_signal = _contains_any(text, _MARKET_SIGNAL_KEYWORDS)
    has_number = bool(re.search(r"\d+(?:\.\d+)?\s*(?:%|亿|万|元|MW|GW|度|股)?", text, flags=re.I))

    if _is_low_information_short(text, cjk_chars):
        return {
            "is_drop": True,
            "quality_score": 5,
            "quality_tier": "drop",
            "quality_reasons": ["too_short_without_signal"],
            "body_quality": "list_complete_short",
            "content_chars": chars,
            "detail_required": False,
        }

    if cjk_chars >= 120:
        score += 45
        reasons.append("long_discussion")
    elif cjk_chars >= 60:
        score += 35
        reasons.append("substantial_text")
    elif cjk_chars >= 24:
        score += 25
        reasons.append("contextual_text")
    elif cjk_chars >= MIN_SHORT_TEXT_CJK_CHARS:
        score += 12
        reasons.append("short_but_readable")

    if has_event:
        score += 30
        reasons.append("event_or_fundamental_signal")
    if has_reasoning:
        score += 20
        reasons.append("reasoning_signal")
    if has_market_signal:
        score += 10
        reasons.append("market_signal")
    if has_number:
        score += 10
        reasons.append("numeric_signal")

    engagement = _engagement(record)
    if engagement >= 1000:
        score += 15
        reasons.append("high_engagement")
    elif engagement >= 100:
        score += 8
        reasons.append("some_engagement")

    if re.search(r"([哈啊呀哦嗯]{2,}|[!?！？。]{3,})", text):
        score -= 8
        reasons.append("chatty_noise")

    if cjk_chars < 16 and not (has_event or has_reasoning):
        score -= 8
        reasons.append("thin_context")

    score = max(0, min(100, score))
    if score >= 70:
        tier = "high"
    elif score >= 40:
        tier = "medium"
    else:
        tier = "low"

    text_may_be_truncated = bool(_ELLIPSIS_RE.search(text)) or len(_normalize_text(record.get("summary"))) >= 110
    if chars <= 80 and not text_may_be_truncated:
        body_quality = "list_complete_short"
    elif text_may_be_truncated:
        body_quality = "list_maybe_truncated"
    else:
        body_quality = "list_substantial"

    detail_required = body_quality == "list_maybe_truncated" and chars < 80

    return {
        "is_drop": False,
        "quality_score": score,
        "quality_tier": tier,
        "quality_reasons": reasons or ["kept_low_signal"],
        "body_quality": body_quality,
        "content_chars": chars,
        "detail_required": detail_required,
    }


def _rank_key(record: dict[str, Any]) -> tuple[int, int, int, int]:
    score = int(record.get("social_quality_score") or 0)
    engagement = _engagement(record)
    published = _parse_dt(record.get("published_at"))
    timestamp = int(published.timestamp()) if published else 0
    stable = _stable_hash(str(record.get("url") or record.get("title") or ""))
    return (-score, -engagement, -timestamp, stable)


def _allocate_quota(counts: dict[str, int], total_quota: int) -> dict[str, int]:
    if total_quota <= 0 or not counts:
        return {bucket: 0 for bucket in counts}
    total = sum(counts.values())
    allocations: dict[str, int] = {}
    remainders: list[tuple[float, str]] = []
    assigned = 0
    for bucket, count in counts.items():
        exact = total_quota * count / total
        base = math.floor(exact)
        allocations[bucket] = base
        assigned += base
        remainders.append((exact - base, bucket))
    for _, bucket in sorted(remainders, reverse=True)[: max(0, total_quota - assigned)]:
        allocations[bucket] += 1
    return allocations


def _select_within_day(records: list[dict[str, Any]], quota: int) -> set[str]:
    if quota <= 0:
        return set()
    if len(records) <= quota:
        return {str(record.get("url") or id(record)) for record in records}

    high_pool = [record for record in records if record.get("social_quality_tier") in {"high", "medium"}]
    low_pool = [record for record in records if record.get("social_quality_tier") == "low"]
    high_quota = min(len(high_pool), round(quota * HIGH_QUALITY_SHARE))
    low_quota = min(len(low_pool), quota - high_quota)
    spare = quota - high_quota - low_quota
    if spare > 0 and high_quota < len(high_pool):
        take = min(spare, len(high_pool) - high_quota)
        high_quota += take
        spare -= take
    if spare > 0 and low_quota < len(low_pool):
        low_quota += min(spare, len(low_pool) - low_quota)

    selected: set[str] = set()
    for record in sorted(high_pool, key=_rank_key)[:high_quota]:
        selected.add(str(record.get("url") or id(record)))
    for record in sorted(low_pool, key=_rank_key)[:low_quota]:
        selected.add(str(record.get("url") or id(record)))
    return selected


def annotate_social_records(
    records: Iterable[dict[str, Any]],
    *,
    window_start: Any = None,
    window_end: Any = None,
    quota_per_7d: int | None = None,
    preserve_existing_selection: bool = False,
) -> list[dict[str, Any]]:
    quota_per_7d = quota_per_7d or social_analysis_quota_per_7d()
    quota = _window_quota(window_start, window_end, quota_per_7d)
    annotated: list[dict[str, Any]] = []

    for record in records:
        item = dict(record)
        decision = evaluate_social_quality(item)
        existing_body_quality = str(record.get("social_body_quality") or "")
        existing_detail_required = bool(record.get("social_detail_required"))
        is_drop = bool(decision["is_drop"])
        item["social_quality_score"] = int(decision["quality_score"])
        item["social_quality_tier"] = str(decision["quality_tier"])
        item["social_quality_reasons"] = list(decision["quality_reasons"])
        item["social_body_quality"] = "detail_full_text" if preserve_existing_selection and existing_body_quality == "detail_full_text" else str(decision["body_quality"])
        item["social_content_chars"] = int(decision["content_chars"])
        item["social_detail_required"] = existing_detail_required if preserve_existing_selection else bool(decision["detail_required"])
        item["social_sampling_bucket"] = _day_bucket(item)

        if is_drop:
            item["social_selected_for_analysis"] = False
            item["is_content_relevant"] = False
            item["content_relevance_reason"] = "quality_drop:" + ",".join(item["social_quality_reasons"])
            item["social_sampling_reason"] = "quality_drop"
        elif preserve_existing_selection:
            selected = bool(record.get("social_selected_for_analysis"))
            item["social_selected_for_analysis"] = selected
            item["is_content_relevant"] = selected
            item["content_relevance_reason"] = (
                "selected_for_analysis" if selected else str(record.get("content_relevance_reason") or "sampling_excluded:preserved")
            )
            item["social_sampling_reason"] = str(record.get("social_sampling_reason") or ("selected:preserved" if selected else "sampling_excluded:preserved"))
        else:
            item["social_selected_for_analysis"] = False
            item["is_content_relevant"] = False
            item["content_relevance_reason"] = "sampling_pending"
            item["social_sampling_reason"] = "sampling_pending"
        annotated.append(item)

    if preserve_existing_selection:
        for item in annotated:
            if not item.get("social_selected_for_analysis"):
                item["social_detail_required"] = False
        return annotated

    eligible = [item for item in annotated if item.get("social_quality_tier") != "drop"]
    if len(eligible) <= quota:
        selected_urls = {str(item.get("url") or id(item)) for item in eligible}
    else:
        counts: dict[str, int] = defaultdict(int)
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in eligible:
            bucket = str(item.get("social_sampling_bucket") or "unknown")
            counts[bucket] += 1
            buckets[bucket].append(item)
        day_quotas = _allocate_quota(dict(counts), quota)
        selected_urls = set()
        for bucket, day_records in buckets.items():
            selected_urls.update(_select_within_day(day_records, day_quotas.get(bucket, 0)))

    for item in annotated:
        key = str(item.get("url") or id(item))
        if item.get("social_quality_tier") == "drop":
            item["social_detail_required"] = False
            continue
        selected = key in selected_urls
        item["social_selected_for_analysis"] = selected
        item["is_content_relevant"] = selected
        if selected:
            item["content_relevance_reason"] = "selected_for_analysis"
            item["social_sampling_reason"] = (
                "selected:quota_within_limit" if len(eligible) <= quota else f"selected:stratified_quota_{quota}"
            )
        else:
            item["content_relevance_reason"] = f"sampling_excluded:quota_{quota}"
            item["social_sampling_reason"] = "sampling_excluded:stratified_quota"
            item["social_detail_required"] = False
    return annotated
