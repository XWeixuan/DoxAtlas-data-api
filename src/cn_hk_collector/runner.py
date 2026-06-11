from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Any, Iterable
from uuid import uuid4

from cn_hk_collector.akshare_market_data import fetch_akshare_snapshot_sync
from cn_hk_collector.collectors.cn_hk_media_client import fetch_cn_hk_media_sync
from cn_hk_collector.collectors.guba_client import fetch_guba_posts_sync
from cn_hk_collector.market import normalize_market, normalize_ticker
from cn_hk_collector.media_content_relevance import evaluate_content_relevance
from cn_hk_collector.media_dedupe import dedupe_media_records
from cn_hk_collector.ticker_entity_registry import get_ticker_entity_aliases

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CollectResult:
    task_id: str
    market: str
    ticker: str
    media_fetched: int
    media_written: int
    social_fetched: int
    social_written: int
    media_relevant: int
    media_irrelevant: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _alias_candidates(ticker: str, ticker_info: dict[str, Any], db_conn: Any, market: str) -> list[str]:
    aliases = [
        ticker,
        str(ticker_info.get("org_short_name_cn") or "").strip(),
        str(ticker_info.get("companyName") or "").strip(),
    ]
    aliases.extend(
        get_ticker_entity_aliases(
            ticker,
            market,
            db_conn=db_conn,
            fallback_snapshot=ticker_info,
        )
    )
    return list(dict.fromkeys(alias for alias in aliases if alias))


def label_media_records(records: Iterable[dict[str, Any]], *, ticker: str, aliases: Iterable[str]) -> list[dict[str, Any]]:
    labeled: list[dict[str, Any]] = []
    for record in records:
        item = dict(record)
        if item.get("is_content_relevant") is False:
            item["content_relevance_reason"] = item.get("content_relevance_reason") or "preclassified_irrelevant"
        else:
            decision = evaluate_content_relevance(item, ticker, target_aliases=aliases)
            item["is_content_relevant"] = decision.is_content_relevant
            item["content_relevance_reason"] = decision.content_relevance_reason
        labeled.append(item)
    return labeled


def collect_ticker(
    *,
    market: str,
    ticker: str,
    lookback_days: int = 7,
    window_start: str | None = None,
    window_end: str | None = None,
    database_url: str | None = None,
    task_id: str | None = None,
    collect_media: bool = True,
    collect_social: bool = True,
) -> CollectResult:
    from cn_hk_collector.db import connect, upsert_raw_records

    market = normalize_market(market)
    ticker = normalize_ticker(ticker, market)
    task_id = task_id or str(uuid4())

    with connect(database_url) as conn:
        ticker_info = fetch_akshare_snapshot_sync(ticker, market=market)
        aliases = _alias_candidates(ticker, ticker_info, conn, market)

        media_records: list[dict[str, Any]] = []
        if collect_media:
            media_records = fetch_cn_hk_media_sync(
                ticker,
                market=market,
                lookback_days=lookback_days,
                window_start=window_start,
                window_end=window_end,
                ticker_info=ticker_info,
            )
            media_records = dedupe_media_records(media_records)
            media_records = label_media_records(media_records, ticker=ticker, aliases=aliases)

        social_records: list[dict[str, Any]] = []
        if collect_social:
            social_records = fetch_guba_posts_sync(
                ticker,
                lookback_days=lookback_days,
                window_start=window_start,
                window_end=window_end,
                market=market,
            )

        media_written = upsert_raw_records(
            conn,
            "raw_media",
            media_records,
            market=market,
            ticker=ticker,
            task_id=task_id,
        )
        social_written = upsert_raw_records(
            conn,
            "raw_social",
            social_records,
            market=market,
            ticker=ticker,
            task_id=task_id,
        )

    relevant = sum(1 for record in media_records if record.get("is_content_relevant") is not False)
    irrelevant = sum(1 for record in media_records if record.get("is_content_relevant") is False)
    result = CollectResult(
        task_id=task_id,
        market=market,
        ticker=ticker,
        media_fetched=len(media_records),
        media_written=media_written,
        social_fetched=len(social_records),
        social_written=social_written,
        media_relevant=relevant,
        media_irrelevant=irrelevant,
    )
    logger.info("collect result: %s", result.to_dict())
    return result
