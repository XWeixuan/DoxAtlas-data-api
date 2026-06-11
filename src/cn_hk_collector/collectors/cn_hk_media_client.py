from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import quote, urlencode, urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from cn_hk_collector.akshare_market_data import fetch_akshare_snapshot_sync
from cn_hk_collector.collectors.chinese_text import clean_chinese_text, decode_chinese_response, looks_garbled
from cn_hk_collector.collectors.crawl_log import format_media_crawl_summary
from cn_hk_collector.collectors.guba_utils.ProxyManager import ProxyManager
from cn_hk_collector.content_filters import apply_length_relevance_filter
from cn_hk_collector.market import DEFAULT_MARKET, normalize_market, normalize_ticker

logger = logging.getLogger(__name__)

LIST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

DETAIL_DEADLINE_SECONDS = 150
DETAIL_MAX_ATTEMPTS_PER_URL = 1
DETAIL_FETCH_WORKERS = 32
DETAIL_CHANNEL_LIMITS = {
    "eastmoney_news": 80,
    "sina_stock_news": 120,
    "yicai": 50,
    "cls_news": 35,
    "cls_telegraph": 35,
    "stcn": 40,
}
MAX_LIST_PAGES = 5
EASTMONEY_LIST_PAGES = 30
CLS_SEARCH_LIST_PAGES = 3
CLS_TELEGRAPH_LIST_PAGES = 3
STCN_LIST_PAGES = 5
YICAI_LIST_PAGES = 20
LIST_FETCH_WORKERS = 12
CLS_APP = "CailianpressWeb"
CLS_SV = "8.7.9"
MARKET_TZ = {
    "cn": ZoneInfo("Asia/Shanghai"),
    "hk": ZoneInfo("Asia/Hong_Kong"),
}


@dataclass(frozen=True)
class MediaSource:
    channel: str
    source_name: str
    build_url: Callable[[str, str, int], Optional[str]]
    enabled_markets: tuple[str, ...] = ("cn", "hk")
    max_pages: int = MAX_LIST_PAGES


def _new_crawl_stats() -> dict[str, Any]:
    return {
        "lock": Lock(),
        "list_by_channel": Counter(),
        "list_seconds_by_channel": Counter(),
        "list_proxy_ips_by_channel": Counter(),
        "list_failures_by_channel": Counter(),
    }


def _add_list_stats(stats: Optional[dict[str, Any]], *, channel: str, count: int, seconds: float, proxy_ips: int = 0, failures: int = 0) -> None:
    if not stats:
        return
    with stats["lock"]:
        stats["list_by_channel"][channel] += count
        stats["list_seconds_by_channel"][channel] += seconds
        stats["list_proxy_ips_by_channel"][channel] += proxy_ips
        stats["list_failures_by_channel"][channel] += failures


def _parse_optional_utc_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _to_utc(dt: datetime, market: str) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MARKET_TZ.get(market, ZoneInfo("Asia/Shanghai")))
    return dt.astimezone(timezone.utc)


def _parse_publish_time(text: str, market: str) -> Optional[datetime]:
    cleaned = re.sub(r"\s+", " ", text or "")
    minutes_ago = re.search(r"(\d+)\s*分钟前", cleaned)
    if minutes_ago:
        now = datetime.now(MARKET_TZ.get(market, ZoneInfo("Asia/Shanghai")))
        return (now - timedelta(minutes=int(minutes_ago.group(1)))).astimezone(timezone.utc)
    hours_ago = re.search(r"(\d+)\s*小时前", cleaned)
    if hours_ago:
        now = datetime.now(MARKET_TZ.get(market, ZoneInfo("Asia/Shanghai")))
        return (now - timedelta(hours=int(hours_ago.group(1)))).astimezone(timezone.utc)
    relative_match = re.search(r"(昨天|今日|今天)\s*(\d{1,2}:\d{2})", cleaned)
    if relative_match:
        now = datetime.now(MARKET_TZ.get(market, ZoneInfo("Asia/Shanghai")))
        day = now.date()
        if relative_match.group(1) == "昨天":
            day = (now - timedelta(days=1)).date()
        try:
            parsed_time = datetime.strptime(relative_match.group(2), "%H:%M").time()
            return _to_utc(datetime.combine(day, parsed_time), market)
        except ValueError:
            pass
    patterns = [
        r"(\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)",
        r"(\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)",
        r"(\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2})",
        r"(\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, cleaned)
        if not match:
            continue
        raw = match.group(1)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
            try:
                return _to_utc(datetime.strptime(raw, fmt), market)
            except ValueError:
                pass
        try:
            normalized = raw.replace("年", "-").replace("月", "-").replace("日", "").replace("  ", " ")
            return _to_utc(datetime.strptime(normalized, "%Y-%m-%d %H:%M"), market)
        except ValueError:
            pass
        try:
            current_year = datetime.now(MARKET_TZ.get(market, ZoneInfo("Asia/Shanghai"))).year
            dt = datetime.strptime(f"{current_year}-{raw}", "%Y-%m-%d %H:%M")
            if dt.replace(tzinfo=MARKET_TZ.get(market, ZoneInfo("Asia/Shanghai"))) > datetime.now(
                MARKET_TZ.get(market, ZoneInfo("Asia/Shanghai"))
            ) + timedelta(days=1):
                dt = dt.replace(year=current_year - 1)
            return _to_utc(dt, market)
        except ValueError:
            pass
    return None


def _within_window(published_dt: Optional[datetime], start_utc: datetime, end_utc: Optional[datetime]) -> bool:
    if not published_dt:
        return True
    if published_dt < start_utc:
        return False
    if end_utc and published_dt >= end_utc:
        return False
    return True


def _build_search_terms(ticker: str, ticker_info: Optional[Dict[str, Any]] = None) -> List[str]:
    terms = [ticker]
    for key in ("org_short_name_cn", "companyName"):
        value = str((ticker_info or {}).get(key) or "").strip()
        if value:
            terms.append(value)
            normalized = unicodedata.normalize("NFKC", value)
            if normalized and normalized != value:
                terms.append(normalized)
    return list(dict.fromkeys(terms))


def _cn_sina_symbol(ticker: str) -> str:
    if ticker.startswith(("6", "9")):
        return f"sh{ticker}"
    if ticker.startswith(("4", "8")):
        return f"bj{ticker}"
    return f"sz{ticker}"


def _sina_stock_news_url(market: str, ticker: str, page: int) -> Optional[str]:
    if market == "hk" and re.fullmatch(r"\d{4,5}", ticker):
        return f"https://stock.finance.sina.com.cn/hkstock/go.php/CompanyNews/page/{page}/code/{ticker.zfill(5)}/.phtml"
    if market == "cn" and re.fullmatch(r"\d{6}", ticker):
        return f"https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllNewsStock.php?symbol={_cn_sina_symbol(ticker)}&Page={page}"
    return None


def _build_sources() -> List[MediaSource]:
    return [
        MediaSource(
            "eastmoney_news",
            "EastMoney News",
            lambda _market, query, page: f"https://so.eastmoney.com/news/s?keyword={quote(query)}&sort=time&pageindex={page}",
            max_pages=EASTMONEY_LIST_PAGES,
        ),
        MediaSource(
            "eastmoney_report",
            "EastMoney Research",
            lambda _market, query, page: f"https://so.eastmoney.com/news/s?keyword={quote(query)}&type=yanbao&pageindex={page}",
            max_pages=2,
        ),
        MediaSource(
            "eastmoney_announcement",
            "EastMoney Announcement",
            lambda _market, query, page: f"https://so.eastmoney.com/news/s?keyword={quote(query)}&type=notice&pageindex={page}",
            max_pages=2,
        ),
        MediaSource(
            "cls_news",
            "CLS News",
            lambda _market, query, page: f"https://www.cls.cn/searchPage?keyword={quote(query)}&page={page}",
            max_pages=CLS_SEARCH_LIST_PAGES,
        ),
        MediaSource(
            "stcn",
            "Securities Times",
            lambda _market, query, page: f"https://www.stcn.com/article/search.html?search_type=all&keyword={quote(query)}&uncertainty=1&sorter=time&page={page}",
            max_pages=STCN_LIST_PAGES,
        ),
        MediaSource(
            "yicai",
            "Yicai",
            lambda _market, query, page: f"https://www.yicai.com/search?keys={quote(query)}&page={page}",
            max_pages=YICAI_LIST_PAGES,
        ),
        MediaSource(
            "cls_telegraph",
            "CLS Telegraph",
            lambda _market, _query, page: f"https://www.cls.cn/telegraph?page={page}",
            max_pages=CLS_TELEGRAPH_LIST_PAGES,
        ),
        MediaSource(
            "sina_stock_news",
            "Sina Finance",
            lambda market, query, page: _sina_stock_news_url(market, query, page),
            enabled_markets=("cn", "hk"),
            max_pages=8,
        ),
    ]


def _fetch_list_html(url: str, proxy_manager: ProxyManager) -> str:
    session = requests.Session()
    session.trust_env = False
    proxies = proxy_manager.get_proxy()
    resp = session.get(url, headers=LIST_HEADERS, timeout=12, proxies=proxies)
    if resp.status_code in {403, 429}:
        proxy_manager.mark_invalid(force=True)
    resp.raise_for_status()
    return decode_chinese_response(resp)


def _fetch_json(url: str, params: Dict[str, Any], referer: str, proxy_manager: Optional[ProxyManager] = None) -> Dict[str, Any]:
    if proxy_manager is None:
        raise RuntimeError("CN/HK media JSON fetch requires proxy_manager; direct target requests are disabled")
    session = requests.Session()
    session.trust_env = False
    headers = {
        **LIST_HEADERS,
        "Accept": "application/json,text/javascript,*/*;q=0.1",
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
    }
    proxies = proxy_manager.get_proxy()
    resp = session.get(url, params=params, headers=headers, timeout=12, proxies=proxies)
    if resp.status_code in {403, 405, 429}:
        proxy_manager.mark_invalid(force=True)
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        return json.loads(decode_chinese_response(resp))


def _strip_html(text: Any) -> str:
    value = re.sub(r"<[^>]+>", " ", str(text or ""))
    return clean_chinese_text(value)


def _cls_flatten_sign_value(key: str, value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        value = str(value).lower()
    if isinstance(value, (str, int, float)):
        return f"{key}={value}"
    if isinstance(value, list):
        if not value:
            return f"{key}[]"
        return "&".join(
            part
            for part in (_cls_flatten_sign_value(f"{key}[{idx}]", item) for idx, item in enumerate(value))
            if part
        )
    if isinstance(value, dict):
        return "&".join(
            part
            for part in (_cls_flatten_sign_value(f"{key}[{name}]", value[name]) for name in sorted(value, key=str.upper))
            if part
        )
    return f"{key}={value}"


def _cls_signed_params(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    signed = {"app": CLS_APP, "os": "web", "sv": CLS_SV}
    if params:
        signed.update(params)
    payload = "&".join(
        part
        for part in (_cls_flatten_sign_value(key, signed[key]) for key in sorted(signed, key=str.upper))
        if part
    )
    signed["sign"] = hashlib.md5(hashlib.sha1(payload.encode("utf-8")).hexdigest().encode("utf-8")).hexdigest()
    return signed


def _parse_epoch_seconds(value: Any) -> Optional[datetime]:
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _first_sentence_title(text: str, fallback: str) -> str:
    cleaned = _strip_html(text)
    if not cleaned:
        return fallback
    bracket = re.match(r"【([^】]{4,120})】", cleaned)
    if bracket:
        return bracket.group(1).strip()
    return re.split(r"[。；;\n]", cleaned, maxsplit=1)[0][:120].strip() or fallback


def _parse_yicai_items(data: Dict[str, Any], query: str, market: str) -> List[Dict[str, Any]]:
    docs = (((data or {}).get("results") or {}).get("docs") or [])
    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for doc in docs:
        title = _strip_html(doc.get("title"))
        desc = _strip_html(doc.get("desc"))
        tags = _strip_html(doc.get("tags"))
        raw_url = str(doc.get("url") or "").strip()
        if not title or not raw_url:
            continue
        url = urljoin("https://www.yicai.com", raw_url)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        published_dt = _parse_publish_time(str(doc.get("creationDate") or ""), market)
        items.append(
            {
                "title": title,
                "url": url,
                "source_name": str(doc.get("source") or "Yicai"),
                "channel": "yicai",
                "published_at": published_dt.isoformat() if published_dt else datetime.now(timezone.utc).isoformat(),
                "published_dt": published_dt,
                "summary": desc or None,
            }
        )
    return items


def _fetch_yicai_query(query: str, market: str, page: int, proxy_manager: Optional[ProxyManager] = None) -> List[Dict[str, Any]]:
    try:
        data = _fetch_json(
            "https://m.yicai.com/api/ajax/getSearchResult",
            {"keys": query, "page": max(page - 1, 0), "pagesize": 20},
            f"https://m.yicai.com/search?keys={quote(query)}",
        )
    except Exception as exc:
        logger.debug("CN/HK media Yicai mobile fetch failed query=%s page=%s: %s", query, page, exc)
        data = {}
    if data.get("status") != 1:
        try:
            data = _fetch_json(
                "https://www.yicai.com/api/ajax/getSearchResult",
                {"keys": query, "page": max(page - 1, 0), "pagesize": 20},
                f"https://www.yicai.com/search?keys={quote(query)}",
                proxy_manager=proxy_manager,
            )
        except Exception as exc:
            logger.debug("CN/HK media Yicai desktop fallback failed query=%s page=%s: %s", query, page, exc)
            data = {}
    if data.get("status") != 1:
        return []
    return _parse_yicai_items(data, query, market)


def _parse_jsonp(text: str) -> Dict[str, Any]:
    match = re.search(r"^[^(]+\((.*)\)\s*;?\s*$", text or "", flags=re.S)
    payload = match.group(1) if match else text
    return json.loads(payload)


def _fetch_eastmoney_news_query(query: str, market: str, page: int, proxy_manager: ProxyManager) -> List[Dict[str, Any]]:
    param = {
        "uid": "",
        "keyword": query,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "8192",
                "sort": "time",
                "pageIndex": page,
                "pageSize": 20,
                "preTag": "",
                "postTag": "",
            }
        },
    }
    session = requests.Session()
    session.trust_env = False
    proxies = proxy_manager.get_proxy()
    resp = session.get(
        "https://search-api-web.eastmoney.com/search/jsonp",
        params={"cb": "doxatlas", "param": json.dumps(param, ensure_ascii=False, separators=(",", ":"))},
        headers={**LIST_HEADERS, "Referer": f"https://so.eastmoney.com/news/s?keyword={quote(query)}"},
        timeout=12,
        proxies=proxies,
    )
    if resp.status_code in {403, 429}:
        proxy_manager.mark_invalid(force=True)
    resp.raise_for_status()
    data = _parse_jsonp(resp.text)
    articles = ((data or {}).get("result") or {}).get("cmsArticleWebOld") or []
    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for article in articles:
        title = _strip_html(article.get("title"))
        summary = _strip_html(article.get("content"))
        context = " ".join(part for part in (title, summary) if part)
        if query and query not in context:
            continue
        raw_url = str(article.get("url") or "").strip()
        if not title or not raw_url or raw_url in seen_urls:
            continue
        seen_urls.add(raw_url)
        published_dt = _parse_publish_time(str(article.get("date") or ""), market)
        items.append(
            {
                "title": title,
                "url": raw_url,
                "source_name": "EastMoney News",
                "channel": "eastmoney_news",
                "published_at": published_dt.isoformat() if published_dt else datetime.now(timezone.utc).isoformat(),
                "published_dt": published_dt,
                "summary": summary or None,
            }
        )
    return items


def _fetch_eastmoney_announcement_query(ticker: str, query: str, market: str, page: int, proxy_manager: ProxyManager) -> List[Dict[str, Any]]:
    param = {
        "uid": "",
        "keyword": query,
        "type": ["notice"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "notice": {
                "searchScope": "8192",
                "sort": "time",
                "pageIndex": page,
                "pageSize": 20,
                "preTag": "",
                "postTag": "",
            }
        },
    }
    session = requests.Session()
    session.trust_env = False
    proxies = proxy_manager.get_proxy()
    resp = session.get(
        "https://search-api-web.eastmoney.com/search/jsonp",
        params={"cb": "doxatlas", "param": json.dumps(param, ensure_ascii=False, separators=(",", ":"))},
        headers={**LIST_HEADERS, "Referer": f"https://so.eastmoney.com/news/s?keyword={quote(query)}&type=notice"},
        timeout=12,
        proxies=proxies,
    )
    if resp.status_code in {403, 429}:
        proxy_manager.mark_invalid(force=True)
    resp.raise_for_status()
    data = _parse_jsonp(decode_chinese_response(resp))
    notices = ((data or {}).get("result") or {}).get("notice") or []
    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for notice in notices:
        title = _strip_html(notice.get("title"))
        summary = _strip_html(notice.get("content"))
        context = " ".join(part for part in (title, summary, str(notice.get("securityShortName") or "")) if part)
        if query and query not in context:
            continue
        code = str(notice.get("code") or "").strip()
        if not title or not code:
            continue
        url = f"https://data.eastmoney.com/notices/detail/{ticker}/{code}.html"
        if url in seen_urls:
            continue
        seen_urls.add(url)
        published_dt = _parse_publish_time(str(notice.get("date") or ""), market)
        items.append(
            {
                "title": title,
                "url": url,
                "source_name": "EastMoney Announcement",
                "channel": "eastmoney_announcement",
                "published_at": published_dt.isoformat() if published_dt else datetime.now(timezone.utc).isoformat(),
                "published_dt": published_dt,
                "summary": summary or None,
            }
        )
    return items


def _fetch_eastmoney_report_query(ticker: str, market: str, page: int, start_utc: datetime, end_utc: Optional[datetime], proxy_manager: ProxyManager) -> List[Dict[str, Any]]:
    market_tz = MARKET_TZ.get(market, ZoneInfo("Asia/Shanghai"))
    begin_date = start_utc.astimezone(market_tz).date().isoformat()
    end_date = (end_utc or datetime.now(timezone.utc)).astimezone(market_tz).date().isoformat()
    session = requests.Session()
    session.trust_env = False
    proxies = proxy_manager.get_proxy()
    resp = session.get(
        "https://reportapi.eastmoney.com/report/list",
        params={
            "cb": "doxatlas",
            "pageNo": page,
            "pageSize": 20,
            "qType": 0,
            "code": ticker,
            "beginTime": begin_date,
            "endTime": end_date,
        },
        headers={**LIST_HEADERS, "Referer": "https://data.eastmoney.com/report/stock.jshtml"},
        timeout=12,
        proxies=proxies,
    )
    if resp.status_code in {403, 429}:
        proxy_manager.mark_invalid(force=True)
    resp.raise_for_status()
    data = _parse_jsonp(decode_chinese_response(resp))
    reports = (data or {}).get("data") or []
    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for report in reports:
        title = _strip_html(report.get("title"))
        info_code = str(report.get("infoCode") or "").strip()
        encode_url = str(report.get("encodeUrl") or "").strip()
        if not title or not info_code:
            continue
        url = (
            "https://data.eastmoney.com/report/zw_stock.jshtml?"
            + urlencode({"encodeUrl": encode_url})
            if encode_url
            else f"https://data.eastmoney.com/report/{info_code}.html"
        )
        if url in seen_urls:
            continue
        seen_urls.add(url)
        published_dt = _parse_publish_time(str(report.get("publishDate") or ""), market)
        summary = " ".join(
            part
            for part in (
                _strip_html(report.get("stockName")),
                _strip_html(report.get("orgSName") or report.get("orgName")),
                _strip_html(report.get("emRatingName") or report.get("sRatingName")),
                _strip_html(report.get("researcher")),
            )
            if part
        )
        items.append(
            {
                "title": title,
                "url": url,
                "source_name": _strip_html(report.get("orgSName") or report.get("orgName")) or "EastMoney Research",
                "channel": "eastmoney_report",
                "published_at": published_dt.isoformat() if published_dt else datetime.now(timezone.utc).isoformat(),
                "published_dt": published_dt,
                "summary": summary or None,
            }
        )
    return items


def _parse_cls_sw_items(data: Dict[str, Any], source: MediaSource, market: str) -> List[Dict[str, Any]]:
    section_name = "telegram" if source.channel == "cls_telegraph" else "depth"
    docs = (((data or {}).get("data") or {}).get(section_name) or {}).get("data") or []
    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for doc in docs:
        doc_id = doc.get("id")
        if not doc_id:
            continue
        raw_title = _strip_html(doc.get("title"))
        desc = _strip_html(doc.get("descr") or doc.get("content"))
        title = raw_title or _first_sentence_title(desc, f"{source.source_name} {doc_id}")
        if not title:
            continue
        url = f"https://www.cls.cn/detail/{doc_id}"
        if url in seen_urls:
            continue
        seen_urls.add(url)
        published_dt = _parse_epoch_seconds(doc.get("time") or doc.get("ctime"))
        items.append(
            {
                "title": title,
                "url": url,
                "source_name": source.source_name,
                "channel": source.channel,
                "published_at": published_dt.isoformat() if published_dt else datetime.now(timezone.utc).isoformat(),
                "published_dt": published_dt,
                "summary": desc or None,
            }
        )
    return items


def _fetch_cls_sw_query(source: MediaSource, query: str, market: str, page: int, proxy_manager: ProxyManager) -> List[Dict[str, Any]]:
    cls_type = "telegram" if source.channel == "cls_telegraph" else "depth"
    payload = {"type": cls_type, "keyword": query, "rn": 20, "page": max(page - 1, 0), "os": "web", "sv": CLS_SV, "app": CLS_APP}
    session = requests.Session()
    session.trust_env = False
    proxies = proxy_manager.get_proxy()
    resp = session.post(
        "https://www.cls.cn/api/sw",
        params=_cls_signed_params(),
        json=payload,
        headers={
            **LIST_HEADERS,
            "Accept": "application/json,text/plain,*/*",
            "Content-Type": "application/json",
            "Origin": "https://www.cls.cn",
            "Referer": f"https://www.cls.cn/searchPage?keyword={quote(query)}&type={cls_type}",
        },
        timeout=12,
        proxies=proxies,
    )
    if resp.status_code in {403, 429}:
        proxy_manager.mark_invalid(force=True)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errno") not in {0, "0"}:
        return []
    return _parse_cls_sw_items(data, source, market)


def _parse_stcn_items(html: str, base_url: str, source: MediaSource, market: str) -> List[Dict[str, Any]]:
    try:
        soup = BeautifulSoup(html or "", features="lxml")
    except TypeError:
        return []
    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for row in soup.find_all("li"):
        title_anchor = row.select_one(".tt a") or row.find("a")
        if not title_anchor:
            continue
        title = _strip_html(title_anchor.get_text(" ", strip=True))
        href = str(title_anchor.get("href") or "").strip()
        if not title or not href:
            continue
        url = urljoin(base_url, href)
        if not url.startswith("http") or url in seen_urls or "/quotes/index/" in url:
            continue
        seen_urls.add(url)
        summary_node = row.select_one(".text")
        summary = _strip_html(summary_node.get_text(" ", strip=True)) if summary_node else None
        info_text = " ".join(part.get_text(" ", strip=True) for part in row.select(".info span"))
        context_text = " ".join(part for part in (title, summary or "", info_text, row.get_text(" ", strip=True)) if part)
        published_dt = _parse_publish_time(context_text, market)
        source_name = source.source_name
        info_parts = [part.get_text(" ", strip=True) for part in row.select(".info span") if part.get_text(" ", strip=True)]
        if info_parts:
            source_name = info_parts[0]
        items.append(
            {
                "title": title,
                "url": url,
                "source_name": source_name,
                "channel": source.channel,
                "published_at": published_dt.isoformat() if published_dt else datetime.now(timezone.utc).isoformat(),
                "published_dt": published_dt,
                "summary": summary,
            }
        )
    return items


def _fetch_stcn_query(source: MediaSource, query: str, market: str, start_utc: datetime, end_utc: Optional[datetime], proxy_manager: ProxyManager) -> List[Dict[str, Any]]:
    session = requests.Session()
    session.trust_env = False
    search_params = {"search_type": "all", "keyword": query, "uncertainty": 1, "sorter": "time"}
    proxies = proxy_manager.get_proxy()
    search_resp = session.get(
        "https://www.stcn.com/article/search.html",
        params=search_params,
        headers=LIST_HEADERS,
        timeout=12,
        proxies=proxies,
    )
    if search_resp.status_code in {403, 429}:
        proxy_manager.mark_invalid(force=True)
    search_resp.raise_for_status()
    page_time: Any = 1
    last_time: Any = ""
    collected: list[dict[str, Any]] = []
    for _page in range(1, source.max_pages + 1):
        resp = session.get(
            "https://www.stcn.com/article/search_data.html",
            params={**search_params, "page_time": page_time, "last_time": last_time},
            headers={
                **LIST_HEADERS,
                "Accept": "application/json,text/javascript,*/*;q=0.1",
                "Referer": search_resp.url,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=12,
            proxies=proxies,
        )
        if resp.status_code in {403, 429}:
            proxy_manager.mark_invalid(force=True)
        resp.raise_for_status()
        payload = resp.json()
        raw_data = payload.get("data")
        if isinstance(raw_data, dict):
            html = raw_data.get("data") or ""
            page_time = raw_data.get("page_time") or page_time
            last_time = raw_data.get("last_time") or last_time
        else:
            html = raw_data or ""
            page_time = payload.get("page_time") or page_time
            last_time = payload.get("last_time") or last_time
        if not html:
            break
        items = _parse_stcn_items(str(html), search_resp.url, source, market)
        if not items:
            break
        page_valid = [item for item in items if _within_window(item.get("published_dt"), start_utc, end_utc)]
        collected.extend(page_valid)
        dated_items = [item for item in items if item.get("published_dt")]
        if dated_items:
            oldest_dt = min(item["published_dt"] for item in dated_items)
            if end_utc and oldest_dt >= end_utc:
                continue
            if oldest_dt < start_utc:
                break
        time.sleep(0.2)
    return collected


def _is_valid_sina_stock_news_url(url: str) -> bool:
    if not re.search(r"(^https?://)?(finance|stock|cj)\.sina\.(com|cn)", url):
        return False
    blocked = (
        "zixuan",
        "shortcut.php",
        "/mkt/",
        "/stock/index",
        "/fund/",
        "/futuremarket/",
        "/money/",
        "/guide/",
    )
    return not any(fragment in url for fragment in blocked)


def _parse_sina_stock_news_items(html: str, base_url: str, source: MediaSource, market: str) -> List[Dict[str, Any]]:
    try:
        soup = BeautifulSoup(html, features="lxml")
        container = soup.select_one(".datelist")
        fragment = str(container) if container else html
    except TypeError:
        fragment = html or ""

    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    pattern = re.compile(
        r"(\d{4}-\d{1,2}-\d{1,2}(?:&nbsp;|\s)+\d{1,2}:\d{2}).{0,160}?<a[^>]+href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>",
        flags=re.I | re.S,
    )
    for raw_time, href, raw_title in pattern.findall(fragment):
        title = _strip_html(raw_title)
        if not title:
            continue
        url = urljoin(base_url, href)
        if url in seen_urls or not _is_valid_sina_stock_news_url(url):
            continue
        seen_urls.add(url)
        published_dt = _parse_publish_time(_strip_html(raw_time), market)
        if not published_dt:
            continue
        items.append(
            {
                "title": title,
                "url": url,
                "source_name": source.source_name,
                "channel": source.channel,
                "published_at": published_dt.isoformat(),
                "published_dt": published_dt,
                "summary": None,
            }
        )
    return items


def _parse_list_items(html: str, base_url: str, source: MediaSource, query: str, market: str) -> List[Dict[str, Any]]:
    if source.channel == "sina_stock_news":
        return _parse_sina_stock_news_items(html, base_url, source, market)

    try:
        soup = BeautifulSoup(html, features="lxml")
    except TypeError:
        return _parse_list_items_regex(html, base_url, source, query, market)
    terms = [query]
    items: list[dict[str, Any]] = []
    seen_urls = set()

    for anchor in soup.find_all("a"):
        title = anchor.get_text(" ", strip=True)
        href = str(anchor.get("href") or "").strip()
        if not title or len(title) < 4 or not href:
            continue
        context_node = anchor.find_parent(["li", "tr", "div", "article"]) or anchor.parent
        context_text = context_node.get_text(" ", strip=True) if context_node else title
        if not any(term and term in context_text for term in terms):
            continue
        url = urljoin(base_url, href)
        if not url.startswith("http") or url in seen_urls:
            continue
        published_dt = _parse_publish_time(context_text, market)
        if source.channel == "sina_stock_news" and (not published_dt or not _is_valid_sina_stock_news_url(url)):
            continue
        seen_urls.add(url)
        items.append(
            {
                "title": title,
                "url": url,
                "source_name": source.source_name,
                "channel": source.channel,
                "published_at": published_dt.isoformat() if published_dt else datetime.now(timezone.utc).isoformat(),
                "published_dt": published_dt,
                "summary": None,
            }
        )
    return items


def _parse_list_items_regex(html: str, base_url: str, source: MediaSource, query: str, market: str) -> List[Dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_urls = set()
    for match in re.finditer(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html or "", flags=re.I | re.S):
        href, raw_title = match.groups()
        title = re.sub(r"<[^>]+>", "", raw_title)
        title = re.sub(r"\s+", " ", title).strip()
        start = html.rfind("<div", 0, match.start())
        if start < 0:
            start = max(0, match.start() - 80)
        end = html.find("</div>", match.end())
        if end < 0:
            end = min(len(html), match.end() + 160)
        context = re.sub(r"<[^>]+>", " ", html[start:end])
        context = re.sub(r"\s+", " ", context).strip()
        if query not in context or not title:
            continue
        url = urljoin(base_url, href)
        if url in seen_urls:
            continue
        published_dt = _parse_publish_time(context, market)
        if source.channel == "sina_stock_news" and (not published_dt or not _is_valid_sina_stock_news_url(url)):
            continue
        seen_urls.add(url)
        items.append(
            {
                "title": title,
                "url": url,
                "source_name": source.source_name,
                "channel": source.channel,
                "published_at": published_dt.isoformat() if published_dt else datetime.now(timezone.utc).isoformat(),
                "published_dt": published_dt,
                "summary": None,
            }
        )
    return items


def _fetch_source_query(
    source: MediaSource,
    ticker: str,
    market: str,
    query: str,
    start_utc: datetime,
    end_utc: Optional[datetime],
    stats: Optional[dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    started = time.perf_counter()
    proxy_manager = ProxyManager()
    if source.channel == "stcn":
        try:
            items = _fetch_stcn_query(source, query, market, start_utc, end_utc, proxy_manager)
            _add_list_stats(
                stats,
                channel=source.channel,
                count=len(items),
                seconds=time.perf_counter() - started,
                proxy_ips=int(getattr(proxy_manager, "ips_fetched", 0) or 0),
            )
            return items
        except Exception as exc:
            logger.debug("CN/HK media STCN fetch failed query=%s: %s", query, exc)
            _add_list_stats(
                stats,
                channel=source.channel,
                count=0,
                seconds=time.perf_counter() - started,
                proxy_ips=int(getattr(proxy_manager, "ips_fetched", 0) or 0),
                failures=1,
            )
            return []

    collected: list[dict[str, Any]] = []
    failures = 0
    for page in range(1, source.max_pages + 1):
        url = source.build_url(market, query, page)
        if not url:
            break
        try:
            if source.channel == "yicai":
                items = _fetch_yicai_query(query, market, page, proxy_manager)
            elif source.channel == "eastmoney_news":
                items = _fetch_eastmoney_news_query(query, market, page, proxy_manager)
            elif source.channel == "eastmoney_announcement":
                items = _fetch_eastmoney_announcement_query(ticker, query, market, page, proxy_manager)
            elif source.channel == "eastmoney_report":
                items = _fetch_eastmoney_report_query(ticker, market, page, start_utc, end_utc, proxy_manager)
            elif source.channel in {"cls_news", "cls_telegraph"}:
                items = _fetch_cls_sw_query(source, query, market, page, proxy_manager)
            else:
                html = _fetch_list_html(url, proxy_manager)
                items = _parse_list_items(html, url, source, query, market)
        except Exception as exc:
            failures += 1
            logger.debug("CN/HK media list fetch failed channel=%s query=%s page=%s: %s", source.channel, query, page, exc)
            break
        if not items:
            break

        page_valid = [item for item in items if _within_window(item.get("published_dt"), start_utc, end_utc)]
        collected.extend(page_valid)
        dated_items = [item for item in items if item.get("published_dt")]
        if dated_items:
            oldest_dt = min(item["published_dt"] for item in dated_items)
            if end_utc and oldest_dt >= end_utc:
                continue
            if oldest_dt < start_utc:
                break
        time.sleep(0.2)
    _add_list_stats(
        stats,
        channel=source.channel,
        count=len(collected),
        seconds=time.perf_counter() - started,
        proxy_ips=int(getattr(proxy_manager, "ips_fetched", 0) or 0),
        failures=failures,
    )
    return collected


def parse_media_detail_html(html: str) -> Dict[str, Any]:
    text = ""
    try:
        import trafilatura

        text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    except Exception:
        text = ""

    try:
        soup = BeautifulSoup(html, features="lxml")
    except TypeError:
        paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", html or "", flags=re.I | re.S)
        fallback_text = "\n".join(re.sub(r"<[^>]+>", "", p).strip() for p in paragraphs if p)
        return {"full_text": fallback_text.strip(), "summary": None}
    if not text:
        body = (
            soup.select_one(".m-txt")
            or soup.select_one("#ContentBody")
            or soup.find("article")
            or soup.find("main")
            or soup.find("div", class_=re.compile("content|article|detail", re.I))
        )
        if body:
            paragraphs = [p.get_text(" ", strip=True) for p in body.find_all("p")]
        else:
            paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        text = "\n".join(p for p in paragraphs if p)

    description = ""
    meta = soup.find("meta", attrs={"name": "description"})
    if meta:
        description = str(meta.get("content") or "").strip()
    return {"full_text": clean_chinese_text(text), "summary": clean_chinese_text(description) or None}


def _fetch_details(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    selected_items = _select_detail_items(items)
    urls = list(dict.fromkeys(item["url"] for item in selected_items if item.get("url")))
    if not urls:
        return {}
    if len(selected_items) < len(items):
        logger.debug("CN/HK media detail fetch capped selected=%s total_items=%s", len(selected_items), len(items))
    started = time.perf_counter()
    results: dict[str, dict[str, Any]] = {}
    proxy_manager = ProxyManager()
    with ThreadPoolExecutor(max_workers=min(DETAIL_FETCH_WORKERS, len(urls))) as executor:
        future_map = {executor.submit(_fetch_detail_proxy, url, proxy_manager): url for url in urls}
        try:
            for future in as_completed(future_map, timeout=DETAIL_DEADLINE_SECONDS):
                url = future_map[future]
                try:
                    detail = future.result()
                except Exception as exc:
                    logger.debug("CN/HK media proxy detail failed for %s: %s", url, exc)
                    continue
                if detail:
                    results[url] = detail
        except TimeoutError:
            logger.warning("CN/HK media proxy detail deadline reached. Returning partial detail results.")
            for future in future_map:
                future.cancel()
    logger.debug(
        "CN/HK media proxy detail finished success=%s missing=%s duration=%.2fs ips=%s",
        len(results),
        len(urls) - len(results),
        time.perf_counter() - started,
        int(getattr(proxy_manager, "ips_fetched", 0) or 0),
    )
    return results


def _fetch_detail_proxy(url: str, proxy_manager: ProxyManager) -> Dict[str, Any]:
    session = requests.Session()
    session.trust_env = False
    actual_url = url
    if not actual_url.startswith("http"):
        actual_url = urljoin("https://www.cls.cn", actual_url)
    try:
        proxies = proxy_manager.get_proxy()
    except Exception as exc:
        logger.debug("CN/HK media detail proxy unavailable for %s: %s", actual_url, exc)
        return {}
    resp = session.get(actual_url, headers=LIST_HEADERS, timeout=10, proxies=proxies)
    if resp.status_code != 200:
        if resp.status_code in {403, 429}:
            proxy_manager.mark_invalid(force=True)
        return {}
    html = decode_chinese_response(resp)
    if "sys-guard" in html or "访问过于频繁" in html:
        proxy_manager.mark_invalid(force=True)
        return {}
    return parse_media_detail_html(html)


def _select_detail_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(str(item.get("channel") or "unknown"), []).append(item)

    selected: list[dict[str, Any]] = []
    for channel, channel_items in grouped.items():
        limit = DETAIL_CHANNEL_LIMITS.get(channel, 60)
        channel_items.sort(
            key=lambda item: (
                item.get("published_dt") or datetime.min.replace(tzinfo=timezone.utc),
                len(str(item.get("summary") or "")),
            ),
            reverse=True,
        )
        selected.extend(channel_items[:limit])
    return selected


def _dedupe_list_items(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_url: dict[str, dict[str, Any]] = {}
    for item in items:
        url = item.get("url")
        if not url:
            continue
        existing = by_url.get(url)
        if not existing:
            by_url[url] = item
            continue
        existing_dt = existing.get("published_dt")
        item_dt = item.get("published_dt")
        if item_dt and (not existing_dt or item_dt < existing_dt):
            by_url[url] = item
    return list(by_url.values())


def fetch_cn_hk_media_sync(
    ticker: str,
    market: str = DEFAULT_MARKET,
    lookback_days: int = 7,
    window_start=None,
    window_end=None,
    ticker_info: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    market = normalize_market(market)
    ticker = normalize_ticker(ticker, market)
    if market not in {"cn", "hk"}:
        return []

    if ticker_info is None:
        ticker_info = fetch_akshare_snapshot_sync(ticker, market)
    search_terms = _build_search_terms(ticker, ticker_info)
    crawl_started = time.perf_counter()
    stats = _new_crawl_stats()

    custom_start = _parse_optional_utc_datetime(window_start)
    custom_end = _parse_optional_utc_datetime(window_end)
    has_custom_window = bool(custom_start and custom_end and custom_end > custom_start)
    start_utc = custom_start if has_custom_window else datetime.now(timezone.utc) - timedelta(days=lookback_days)
    end_utc = custom_end if has_custom_window else None

    sources = [source for source in _build_sources() if market in source.enabled_markets]
    jobs = []
    list_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=LIST_FETCH_WORKERS) as executor:
        for source in sources:
            for term in search_terms:
                jobs.append(executor.submit(_fetch_source_query, source, ticker, market, term, start_utc, end_utc, stats))

        list_items: list[dict[str, Any]] = []
        for future in as_completed(jobs):
            try:
                list_items.extend(future.result())
            except Exception as exc:
                logger.debug("CN/HK media list job failed for %s/%s: %s", market, ticker, exc)
    list_seconds = time.perf_counter() - list_started

    list_items = _dedupe_list_items(list_items)
    if not list_items:
        summary = {
            "market": market,
            "ticker": ticker,
            "terms": search_terms,
            "list_seconds": round(list_seconds, 2),
            "detail_seconds": 0,
            "total_seconds": round(time.perf_counter() - crawl_started, 2),
            "list_items": 0,
            "records": 0,
            "list_by_channel": dict(stats["list_by_channel"]),
            "final_by_channel": {},
            "list_seconds_by_channel": {key: round(value, 2) for key, value in stats["list_seconds_by_channel"].items()},
            "list_failures_by_channel": dict(stats["list_failures_by_channel"]),
            "list_proxy_ips_by_channel": dict(stats["list_proxy_ips_by_channel"]),
            "detail_proxy_ips": 0,
            "detail_selected": 0,
            "detail_success": 0,
            "detail_failed": 0,
            "detail_success_rate": 0,
            "detail_success_by_channel": {},
            "detail_failed_by_channel": {},
            "garbled_dropped": 0,
            "length_filtered": 0,
        }
        logger.info(format_media_crawl_summary(summary))
        return []

    detail_started = time.perf_counter()
    selected_detail_urls = set(item["url"] for item in _select_detail_items(list_items) if item.get("url"))
    detail_map = _fetch_details(list_items)
    detail_seconds = time.perf_counter() - detail_started
    final_records: list[dict[str, Any]] = []
    detail_success_by_channel: Counter[str] = Counter()
    detail_failed_by_channel: Counter[str] = Counter()
    garbled_dropped = 0
    for item in list_items:
        detail = detail_map.get(item["url"]) or {}
        full_text = clean_chinese_text(detail.get("full_text") or "")
        summary = clean_chinese_text(detail.get("summary") or item.get("summary") or "")
        title = clean_chinese_text(item.get("title") or "")
        content = full_text or summary or title
        if item.get("url") in selected_detail_urls:
            if full_text and full_text != title:
                detail_success_by_channel[item["channel"]] += 1
            else:
                detail_failed_by_channel[item["channel"]] += 1
        if looks_garbled(title) or looks_garbled(summary) or looks_garbled(content):
            garbled_dropped += 1
            logger.debug("CN/HK media dropping garbled record channel=%s url=%s title=%s", item.get("channel"), item.get("url"), title[:80])
            continue
        final_records.append(
            {
                "ticker": ticker,
                "published_at": item["published_at"],
                "source_type": "media",
                "channel": item["channel"],
                "source_name": item["source_name"],
                "title": title,
                "summary": summary or None,
                "content": content,
                "url": item["url"],
            }
        )
    final_records = apply_length_relevance_filter(final_records, source_type="media")
    final_by_channel = Counter(record.get("channel") or "unknown" for record in final_records)
    detail_success = sum(detail_success_by_channel.values())
    detail_failed = max(len(selected_detail_urls) - len(detail_map), 0) + sum(detail_failed_by_channel.values())
    detail_total = detail_success + detail_failed
    summary = {
        "market": market,
        "ticker": ticker,
        "terms": search_terms,
        "list_seconds": round(list_seconds, 2),
        "detail_seconds": round(detail_seconds, 2),
        "total_seconds": round(time.perf_counter() - crawl_started, 2),
        "list_items": len(list_items),
        "records": len(final_records),
        "list_by_channel": dict(stats["list_by_channel"]),
        "final_by_channel": dict(final_by_channel),
        "list_seconds_by_channel": {key: round(value, 2) for key, value in stats["list_seconds_by_channel"].items()},
        "list_failures_by_channel": dict(stats["list_failures_by_channel"]),
        "list_proxy_ips_by_channel": dict(stats["list_proxy_ips_by_channel"]),
        "detail_proxy_ips": 0,
        "detail_selected": len(selected_detail_urls),
        "detail_success": detail_success,
        "detail_failed": detail_failed,
        "detail_success_rate": round(detail_success / detail_total, 4) if detail_total else 0,
        "detail_success_by_channel": dict(detail_success_by_channel),
        "detail_failed_by_channel": dict(detail_failed_by_channel),
        "garbled_dropped": garbled_dropped,
        "length_filtered": sum(1 for record in final_records if record.get("is_content_relevant") is False),
    }
    logger.info(format_media_crawl_summary(summary))
    return final_records


async def fetch_cn_hk_media(
    ticker: str,
    market: str = DEFAULT_MARKET,
    lookback_days: int = 7,
    window_start=None,
    window_end=None,
    ticker_info: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(
        fetch_cn_hk_media_sync,
        ticker,
        market,
        lookback_days,
        window_start,
        window_end,
        ticker_info,
    )
