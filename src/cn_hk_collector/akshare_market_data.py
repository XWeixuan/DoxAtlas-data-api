from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, Optional

import requests

from cn_hk_collector.market import DEFAULT_MARKET, get_market_context, normalize_market, normalize_ticker

logger = logging.getLogger(__name__)

AKSHARE_CACHE_TTL_SECONDS = 300
AKSHARE_TIMEOUT_SECONDS = 12
_CACHE: dict[str, dict[str, Any]] = {}
QUOTE_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}


def _empty_snapshot(ticker: str, market: str, error: str | None = None) -> Dict[str, Any]:
    ctx = get_market_context(market)
    payload: Dict[str, Any] = {
        "ticker": ticker,
        "market": market,
        "currency": ctx.currency,
        "currencySymbol": ctx.currency_symbol,
        "companyName": ticker,
        "org_short_name_cn": ticker,
        "price": 0,
        "priceChange": 0,
        "isAvailable": False,
        "provider": "akshare",
    }
    if error:
        payload["error"] = error
    return payload


def _import_akshare():
    try:
        import akshare as ak  # type: ignore

        return ak
    except Exception as exc:
        raise RuntimeError(f"AKShare unavailable: {exc}") from exc


def _value_map(df: Any) -> Dict[str, Any]:
    try:
        rows = df.to_dict("records")
    except Exception:
        return {}
    result = {}
    for row in rows:
        key = str(row.get("item") or "").strip()
        if key:
            result[key] = row.get("value")
    return result


def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text and text.lower() != "nan":
            return text
    return None


def _safe_float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        text = str(value).strip()
        if not text or text == "-":
            return 0.0
        return float(text.replace(",", ""))
    except Exception:
        return 0.0


def _cn_xq_symbol(ticker: str) -> str:
    if ticker.startswith(("6", "9")):
        return f"SH{ticker}"
    if ticker.startswith(("4", "8")):
        return f"BJ{ticker}"
    return f"SZ{ticker}"


def _cn_quote_symbol(ticker: str) -> str:
    if ticker.startswith(("6", "9")):
        return f"sh{ticker}"
    if ticker.startswith(("4", "8")):
        return f"bj{ticker}"
    return f"sz{ticker}"


def _fetch_text(url: str, encoding: str = "gbk") -> str:
    session = requests.Session()
    session.trust_env = False
    response = session.get(url, headers=QUOTE_HEADERS, timeout=8)
    response.raise_for_status()
    return response.content.decode(encoding, "ignore")


def _fetch_tencent_quote(symbol: str) -> Dict[str, Any]:
    text = _fetch_text(f"https://qt.gtimg.cn/q={symbol}", encoding="gbk")
    match = re.search(r'="([^"]*)"', text)
    if not match:
        return {}
    parts = match.group(1).split("~")
    if len(parts) < 33:
        return {}
    return {
        "name": parts[1],
        "code": parts[2],
        "price": _safe_float(parts[3]),
        "previous_close": _safe_float(parts[4]),
        "change": _safe_float(parts[31]),
        "change_pct": _safe_float(parts[32]),
        "timestamp": parts[30] if len(parts) > 30 else None,
    }


def _fetch_sina_cn_quote(symbol: str) -> Dict[str, Any]:
    text = _fetch_text(f"https://hq.sinajs.cn/list={symbol}", encoding="gbk")
    match = re.search(r'="([^"]*)"', text)
    if not match:
        return {}
    parts = match.group(1).split(",")
    if len(parts) < 4:
        return {}
    price = _safe_float(parts[3])
    previous_close = _safe_float(parts[2])
    change_pct = ((price - previous_close) / previous_close * 100) if previous_close else 0.0
    return {"name": parts[0], "price": price, "previous_close": previous_close, "change_pct": change_pct}


def _fetch_sina_hk_quote(ticker: str) -> Dict[str, Any]:
    text = _fetch_text(f"https://hq.sinajs.cn/list=hk{ticker.zfill(5)}", encoding="gbk")
    match = re.search(r'="([^"]*)"', text)
    if not match:
        return {}
    parts = match.group(1).split(",")
    if len(parts) < 9:
        return {}
    return {"name": parts[1], "price": _safe_float(parts[6]), "change_pct": _safe_float(parts[8])}


def _lookup_stock_row(df: Any, ticker: str, code_width: int | None = None) -> Optional[Dict[str, Any]]:
    try:
        records = df.to_dict("records")
    except Exception:
        return None
    targets = {ticker}
    if code_width:
        targets.add(ticker.zfill(code_width))
    for row in records:
        raw_code = str(row.get("代码") or row.get("code") or "").strip()
        if not raw_code:
            continue
        codes = {raw_code, raw_code.zfill(code_width or len(raw_code))}
        if targets & codes:
            return row
    return None


def _fetch_cn_snapshot_sync(ak: Any, ticker: str, market: str) -> Dict[str, Any]:
    ctx = get_market_context(market)
    company_name = ticker
    short_name = ticker
    price = 0.0
    change = 0.0
    errors: list[str] = []

    try:
        quote = _fetch_tencent_quote(_cn_quote_symbol(ticker))
        short_name = _first_non_empty(quote.get("name"), short_name) or ticker
        company_name = short_name
        price = _safe_float(quote.get("price")) or price
        change = _safe_float(quote.get("change_pct"))
    except Exception as exc:
        errors.append(f"tencent_quote: {exc}")

    try:
        if price <= 0:
            quote = _fetch_sina_cn_quote(_cn_quote_symbol(ticker))
            short_name = _first_non_empty(quote.get("name"), short_name) or ticker
            company_name = short_name
            price = _safe_float(quote.get("price")) or price
            change = _safe_float(quote.get("change_pct")) or change
    except Exception as exc:
        errors.append(f"sina_quote: {exc}")

    try:
        if short_name == ticker and hasattr(ak, "stock_individual_info_em"):
            info = _value_map(ak.stock_individual_info_em(symbol=ticker, timeout=AKSHARE_TIMEOUT_SECONDS))
            short_name = _first_non_empty(info.get("股票简称"), short_name) or ticker
            company_name = _first_non_empty(short_name, company_name, ticker) or ticker
    except Exception as exc:
        errors.append(f"stock_individual_info_em: {exc}")

    if price <= 0 and short_name == ticker:
        return _empty_snapshot(ticker, market, "; ".join(errors) if errors else "AKShare returned no CN data")

    return {
        "ticker": ticker,
        "market": market,
        "currency": ctx.currency,
        "currencySymbol": ctx.currency_symbol,
        "companyName": company_name,
        "org_short_name_cn": short_name,
        "price": price,
        "priceChange": change,
        "isAvailable": price > 0 or short_name != ticker,
        "provider": "akshare",
        "errors": errors[:3],
    }


def _fetch_hk_snapshot_sync(ak: Any, ticker: str, market: str) -> Dict[str, Any]:
    ctx = get_market_context(market)
    company_name = ticker
    short_name = ticker
    price = 0.0
    change = 0.0
    errors: list[str] = []
    candidates = [ticker, ticker.zfill(5)]

    try:
        quote = _fetch_tencent_quote(f"hk{ticker.zfill(5)}")
        short_name = _first_non_empty(quote.get("name"), short_name) or ticker
        company_name = short_name
        price = _safe_float(quote.get("price")) or price
        change = _safe_float(quote.get("change_pct"))
    except Exception as exc:
        errors.append(f"tencent_quote: {exc}")

    try:
        if price <= 0:
            quote = _fetch_sina_hk_quote(ticker)
            short_name = _first_non_empty(quote.get("name"), short_name) or ticker
            company_name = short_name
            price = _safe_float(quote.get("price")) or price
            change = _safe_float(quote.get("change_pct")) or change
    except Exception as exc:
        errors.append(f"sina_quote: {exc}")

    try:
        profile = ak.stock_hk_security_profile_em(symbol=ticker.zfill(5))
        row = profile.to_dict("records")[0] if len(profile) else {}
        short_name = _first_non_empty(row.get("证券简称"), short_name) or ticker
    except Exception as exc:
        errors.append(f"stock_hk_security_profile_em: {exc}")

    try:
        profile = ak.stock_hk_company_profile_em(symbol=ticker.zfill(5))
        row = profile.to_dict("records")[0] if len(profile) else {}
        company_name = _first_non_empty(row.get("公司名称"), company_name, short_name) or ticker
    except Exception as exc:
        errors.append(f"stock_hk_company_profile_em: {exc}")

    if price <= 0 and short_name == ticker:
        return _empty_snapshot(ticker, market, "; ".join(errors) if errors else "AKShare returned no HK data")

    return {
        "ticker": ticker,
        "market": market,
        "currency": ctx.currency,
        "currencySymbol": ctx.currency_symbol,
        "companyName": company_name,
        "org_short_name_cn": short_name,
        "price": price,
        "priceChange": change,
        "isAvailable": price > 0 or short_name != ticker,
        "provider": "akshare",
        "errors": errors[:3],
    }


def fetch_akshare_snapshot_sync(ticker: str, market: str = DEFAULT_MARKET) -> Dict[str, Any]:
    market = normalize_market(market)
    ticker = normalize_ticker(ticker, market)
    if market not in {"cn", "hk"}:
        return _empty_snapshot(ticker, market, f"AKShare is not configured for market={market}")

    cache_key = f"{market}:{ticker}"
    cached = _CACHE.get(cache_key)
    if cached and time.time() - cached["timestamp"] < AKSHARE_CACHE_TTL_SECONDS:
        return dict(cached["data"])

    try:
        ak = _import_akshare()
        if market == "cn":
            snapshot = _fetch_cn_snapshot_sync(ak, ticker, market)
        else:
            snapshot = _fetch_hk_snapshot_sync(ak, ticker, market)
    except Exception as exc:
        logger.warning("AKShare snapshot failed for %s/%s: %s", market, ticker, exc)
        snapshot = _empty_snapshot(ticker, market, str(exc))

    _CACHE[cache_key] = {"timestamp": time.time(), "data": dict(snapshot)}
    return snapshot


async def fetch_akshare_snapshot(ticker: str, market: str = DEFAULT_MARKET) -> Dict[str, Any]:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(fetch_akshare_snapshot_sync, ticker, market),
            timeout=AKSHARE_TIMEOUT_SECONDS + 3,
        )
    except Exception as exc:
        market = normalize_market(market)
        ticker = normalize_ticker(ticker, market)
        logger.warning("AKShare async snapshot failed for %s/%s: %s", market, ticker, exc)
        return _empty_snapshot(ticker, market, str(exc))
