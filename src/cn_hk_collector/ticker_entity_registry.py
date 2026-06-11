from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, List, Optional

from cn_hk_collector.akshare_market_data import fetch_akshare_snapshot_sync
from cn_hk_collector.market import normalize_market, normalize_ticker

logger = logging.getLogger(__name__)

_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_CACHE_TS: dict[tuple[str, str], float] = {}
_TTL_SECONDS = 24 * 3600


def _first(row: dict[str, Any], names: Iterable[str]) -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _normalize_entity(market: str, row: dict[str, Any]) -> Optional[dict[str, Any]]:
    ticker = _first(row, ("证券代码", "A股代码", "代码", "symbol", "品种代码", "Code"))
    if not ticker:
        return None
    try:
        ticker = normalize_ticker(ticker, market)
    except Exception:
        ticker = ticker.zfill(5 if market == "hk" else 6)
    return {
        "market": market,
        "ticker": ticker,
        "company_name": _first(row, ("公司全称", "公司名称", "中文名称", "名称", "证券简称")),
        "short_name": _first(row, ("证券简称", "中文名称", "名称", "简称")),
        "english_name": _first(row, ("英文名称", "英文简称", "英文名")),
        "exchange": _first(row, ("交易所", "交易所/板块", "市场", "交易类型")),
    }


def _dataframe_rows(df: Any) -> list[dict[str, Any]]:
    try:
        return df.to_dict("records")
    except Exception:
        return []


def fetch_akshare_ticker_entities(market: str) -> list[dict[str, Any]]:
    market = normalize_market(market)
    if market not in {"cn", "hk"}:
        return []

    import akshare as ak  # type: ignore

    frames = []
    if market == "cn":
        for call in (
            lambda: ak.stock_info_sh_name_code(symbol="主板A股"),
            lambda: ak.stock_info_sh_name_code(symbol="科创板"),
            lambda: ak.stock_info_sz_name_code(symbol="A股列表"),
            lambda: ak.stock_info_bj_name_code(),
        ):
            try:
                frames.extend(_dataframe_rows(call()))
            except Exception as exc:
                logger.debug("AKShare ticker entity CN list call failed: %s", exc)
    else:
        for call in (lambda: ak.stock_hk_spot_em(), lambda: ak.stock_hk_spot()):
            try:
                rows = _dataframe_rows(call())
                if rows:
                    frames.extend(rows)
                    break
            except Exception as exc:
                logger.debug("AKShare ticker entity HK list call failed: %s", exc)

    entities: dict[str, dict[str, Any]] = {}
    for row in frames:
        entity = _normalize_entity(market, row)
        if not entity or not entity["ticker"]:
            continue
        existing = entities.get(entity["ticker"], {})
        merged = {**existing, **{key: value for key, value in entity.items() if value}}
        entities[entity["ticker"]] = merged
    return list(entities.values())


def refresh_ticker_entities(market: str, *, db_conn: Any = None, chunk_size: int = 500) -> int:
    entities = fetch_akshare_ticker_entities(market)
    if not db_conn or not entities:
        return len(entities)
    written = 0
    for i in range(0, len(entities), chunk_size):
        chunk = entities[i : i + chunk_size]
        try:
            with db_conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO ticker_entities (
                        market, ticker, company_name, short_name, english_name, exchange, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (market, ticker)
                    DO UPDATE SET
                        company_name = EXCLUDED.company_name,
                        short_name = EXCLUDED.short_name,
                        english_name = EXCLUDED.english_name,
                        exchange = EXCLUDED.exchange,
                        updated_at = now()
                    """,
                    [
                        (
                            item.get("market"),
                            item.get("ticker"),
                            item.get("company_name"),
                            item.get("short_name"),
                            item.get("english_name"),
                            item.get("exchange"),
                        )
                        for item in chunk
                    ],
                )
            db_conn.commit()
            written += len(chunk)
        except Exception as exc:
            logger.debug("ticker_entities upsert failed chunk=%s: %s", i // chunk_size, exc)
            break
    return written


def _fetch_entity_from_db(market: str, ticker: str, db_conn: Any = None) -> Optional[dict[str, Any]]:
    if not db_conn:
        return None
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT ticker, company_name, short_name, english_name
                FROM ticker_entities
                WHERE market = %s AND ticker = %s
                LIMIT 1
                """,
                (market, ticker),
            )
            row = cur.fetchone()
        if row:
            return dict(row)
    except Exception as exc:
        logger.debug("ticker_entities lookup failed market=%s ticker=%s: %s", market, ticker, exc)
    return None


def get_ticker_entity_aliases(
    ticker: str,
    market: str,
    *,
    db_conn: Any = None,
    fallback_snapshot: Optional[dict[str, Any]] = None,
) -> list[str]:
    market = normalize_market(market)
    ticker = normalize_ticker(ticker, market)
    cache_key = (market, ticker)
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached and now - _CACHE_TS.get(cache_key, 0) < _TTL_SECONDS:
        entity = cached
    else:
        entity = _fetch_entity_from_db(market, ticker, db_conn=db_conn)
        if not entity:
            entity = fallback_snapshot or fetch_akshare_snapshot_sync(ticker, market)
        _CACHE[cache_key] = dict(entity or {})
        _CACHE_TS[cache_key] = now

    aliases = [
        ticker,
        entity.get("ticker"),
        entity.get("short_name"),
        entity.get("company_name"),
        entity.get("english_name"),
        entity.get("org_short_name_cn"),
        entity.get("companyName"),
    ]
    return list(dict.fromkeys(str(alias).strip() for alias in aliases if str(alias or "").strip()))
