"""
DoxAtlas - Channel CN: EastMoney Guba
Fetches posts for a specific ticker symbol.
Integrates exact Proxy scheduling from reference project.
"""

import os
import time
import logging
import re
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
import requests
import asyncio
from zoneinfo import ZoneInfo

from .guba_utils.Parser import parse_detail_html
from .guba_utils.ProxyManager import ProxyManager
from .guba_utils.SmartBatchCrawler import GlobalScheduler
from cn_hk_collector.collectors.chinese_text import clean_chinese_text, decode_chinese_response
from cn_hk_collector.collectors.crawl_log import format_social_crawl_summary
from cn_hk_collector.content_filters import apply_length_relevance_filter

logger = logging.getLogger(__name__)

# Basic headers for the list page
LIST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
}
DETAIL_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Origin": "https://mguba.eastmoney.com",
    "Referer": "https://mguba.eastmoney.com/mguba/article/0/",
    "Content-Type": "application/x-www-form-urlencoded",
}

DETAIL_DEADLINE_SECONDS = 360
DETAIL_MAX_ATTEMPTS_PER_URL = 1
DETAIL_DIRECT_DEADLINE_SECONDS = 60
DETAIL_DIRECT_WORKERS = 32
DETAIL_DIRECT_LIMIT = int(os.environ.get("GUBA_DETAIL_DIRECT_LIMIT", "500"))
MAX_LIST_PAGES = 100
DIRECT_PROXY_MODES = {"direct", "none", "off"}


def _extract_guba_ticker_from_url(url: str) -> str | None:
    hk_match = re.search(r"list,hk(\d{4,5})_", url or "", flags=re.I)
    if hk_match:
        return hk_match.group(1).zfill(5).upper()
    match = re.search(r"(?:news|list),([A-Za-z]{0,2}\d{4,6}),", url or "")
    if not match:
        return None
    return re.sub(r"^[A-Za-z]+", "", match.group(1)).upper()


def _extract_guba_post_id_from_url(url: str) -> str | None:
    for pattern in (
        r"/mguba/article/\d+/(\d+)",
        r"[?&]postid=(\d+)",
        r"news,[^,]+,(\d+)\.html",
    ):
        match = re.search(pattern, url or "", flags=re.I)
        if match:
            return match.group(1)
    return None


def _is_ticker_match(item: dict, ticker: str) -> bool:
    url_ticker = _extract_guba_ticker_from_url(item.get("url", ""))
    if not url_ticker:
        logger.debug("Keeping Guba URL without parseable ticker: %s", item.get("url"))
        return True
    return url_ticker == ticker.upper()


def _is_guba_block_page(html: str) -> bool:
    if not html:
        return True
    markers = (
        "验证码",
        "访问过于频繁",
        "sys-guard",
        "fd_guba_validate",
        "em_capt",
        "validate.js",
        "身份核实",
        "韬唤鏍稿疄",
    )
    return any(marker in html for marker in markers)

def _parse_window_dt(value) -> datetime:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None

def _build_guba_list_url(ticker: str, page: int, market: str = "cn") -> str:
    if market == "hk":
        return f"https://guba.eastmoney.com/list,hk{ticker.zfill(5)}_{page}.html"
    return f"https://guba.eastmoney.com/list,{ticker},f_{page}.html"


def _build_mguba_list_url(ticker: str, market: str = "cn") -> str:
    code = f"hk{ticker.zfill(5)}" if market == "hk" else ticker
    return f"https://mguba.eastmoney.com/mguba/list/{code}"


def _parse_mobile_publish_time(text: str) -> datetime:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    cleaned = re.sub(r"\s+", " ", text or "")
    match = re.search(r"(今天|昨天)\s*(\d{1,2}:\d{2})", cleaned)
    if match:
        day = now.date() if match.group(1) == "今天" else (now - timedelta(days=1)).date()
        return datetime.combine(day, datetime.strptime(match.group(2), "%H:%M").time()).replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc)
    match = re.search(r"(\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2})", cleaned)
    if match:
        dt = datetime.strptime(f"{now.year}-{match.group(1)}", "%Y-%m-%d %H:%M")
        if dt.replace(tzinfo=ZoneInfo("Asia/Shanghai")) > now + timedelta(days=1):
            dt = dt.replace(year=now.year - 1)
        return dt.replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _parse_mguba_list_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, features="lxml")
    items: list[dict] = []
    seen_urls: set[str] = set()
    for link in soup.find_all("a", href=True):
        href = str(link.get("href") or "")
        if "/mguba/article/" not in href:
            continue
        row = link.find_parent("li")
        if not row:
            continue
        text = re.sub(r"\s+", " ", row.get_text(" ", strip=True)).strip()
        if not text:
            continue
        full_url = href if href.startswith("http") else f"https://mguba.eastmoney.com{href}"
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)
        author = "Unknown"
        author_match = re.search(r"^(.{1,40}?)\s*发表于", text)
        if author_match:
            author = author_match.group(1).strip()
        title_match = re.search(r"\d+次浏览\s*(.*?)\s*(?:分享|评论|赞|$)", text)
        title = (title_match.group(1).strip() if title_match else text[:120]).strip()
        if not title:
            continue
        published_dt = _parse_mobile_publish_time(text)
        items.append(
            {
                "title": title,
                "url": full_url,
                "source_name": author,
                "published_at": published_dt.isoformat(),
                "published_dt": published_dt,
            }
        )
    return items


def _fetch_mguba_list_page(ticker: str, market: str = "cn", proxy_manager=None) -> list[dict]:
    url = _build_mguba_list_url(ticker, market)
    if not proxy_manager:
        return []
    try:
        proxies = proxy_manager.get_proxy()
        response = requests.get(url, headers=LIST_HEADERS, timeout=12, proxies=proxies)
        if response.status_code in {403, 429}:
            proxy_manager.mark_invalid(force=True)
        response.raise_for_status()
        html = decode_chinese_response(response)
    except Exception as exc:
        logger.debug("mguba fallback failed ticker=%s market=%s: %s", ticker, market, exc)
        return []
    return _parse_mguba_list_html(html)


def fetch_guba_list_page(ticker: str, page: int, proxy_manager=None, market: str = "cn") -> (list, bool):
    """
    Fetch a single list page and return its parsed items.
    Returns (items, should_continue)
    """
    url = _build_guba_list_url(ticker, page, market)
    retry_count = 1
    html = ""
    
    for use_proxy in (True,):
        if use_proxy and not proxy_manager:
            continue
        retry_count = max(1, int(getattr(proxy_manager, "max_ips", 1) or 1)) if use_proxy else 1
        if html:
            break
        while retry_count > 0:
            proxies = {}
            try:
                proxies = proxy_manager.get_proxy() if use_proxy and proxy_manager else {}
                # If proxies format from ProxyManager is just a string IP:
                if proxies and isinstance(proxies, str):
                    proxies = {"http": f"http://{proxies}", "https": f"http://{proxies}"}

                response = requests.get(url, headers=LIST_HEADERS, timeout=10, proxies=proxies)
                if response.status_code != 200:
                    logger.debug("HTTP %s for %s use_proxy=%s", response.status_code, url, use_proxy)
                    if use_proxy and proxy_manager:
                        proxy_manager.mark_invalid(force=True)
                    retry_count -= 1
                    time.sleep(1)
                    continue

                html = decode_chinese_response(response)
                if _is_guba_block_page(html):
                    logger.debug("Anti-bot detected for %s use_proxy=%s", url, use_proxy)
                    if use_proxy and proxy_manager:
                        proxy_manager.mark_invalid(force=True)
                    retry_count -= 1
                    time.sleep(1)
                    continue

                break
            except Exception as e:
                logger.debug("Exception fetching %s use_proxy=%s: %s", url, use_proxy, e)
                if use_proxy and proxy_manager:
                    proxy_manager.mark_invalid(force=True)
                retry_count -= 1
                time.sleep(1)
            
    if not html:
        if page == 1:
            mobile_items = _fetch_mguba_list_page(ticker, market, proxy_manager)
            return mobile_items, bool(mobile_items)
        logger.debug("Failed to fetch Guba list page ticker=%s market=%s page=%s url=%s", ticker, market, page, url)
        return [], False

    soup = BeautifulSoup(html, features="lxml")
    data_list = soup.find_all("tr", "listitem")
    if not data_list and page == 1:
        mobile_items = _fetch_mguba_list_page(ticker, market, proxy_manager)
        if mobile_items:
            return mobile_items, True
    
    items = []
    current_year = datetime.now().year
    
    for item in data_list:
        tds = item.find_all("td")
        if len(tds) < 5:
            continue
            
        try:
            read_cnt = tds[0].text.strip()
            comment_cnt = tds[1].text.strip()
            a_tag = tds[2].find("a")
            if not a_tag:
                continue
            title = a_tag.text.strip()
            href = a_tag.get("href", "")
            
            author_tag = tds[3].find("a")
            author = author_tag.text.strip() if author_tag else "Unknown"
            
            date_str = tds[4].text.strip() # Usually MM-DD HH:MM
            
            if "caifuhao" in href:
                try:
                    # extract year from url like .../20230501...
                    year_part = href.split("/")[-1][0:4]
                    if year_part.isdigit():
                        current_year = int(year_part)
                except:
                    pass
            
            # naive parsing
            try:
                dt = datetime.strptime(f"{current_year}-{date_str}", "%Y-%m-%d %H:%M")
                if dt > datetime.now():
                    dt = datetime.strptime(f"{current_year - 1}-{date_str}", "%Y-%m-%d %H:%M")
            except ValueError:
                dt = datetime.now()
                
            # Convert to UTC (Assuming Guba times are CST Asia/Shanghai)
            cst = ZoneInfo("Asia/Shanghai")
            dt_aware = dt.replace(tzinfo=cst)
            utc_dt = dt_aware.astimezone(timezone.utc)
            
            # Format href
            full_url = href
            if "caifuhao" in full_url and not full_url.startswith("http"):
                full_url = "https:" + full_url
            elif not full_url.startswith("http"):
                full_url = "http://guba.eastmoney.com" + full_url
                
            items.append({
                "title": title,
                "url": full_url,
                "source_name": author,
                "published_at": utc_dt.isoformat(),
                "published_dt": utc_dt # used for filtering
            })
        except Exception as e:
            logger.warning(f"Error parsing item on page {page}: {e}")
            continue
            
    return items, len(items) > 0


def _guba_html_fragment_to_text(value: str) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, features="lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = text.replace("\xa0", " ")
    return clean_chinese_text(text)


def _parse_guba_api_post_payload(payload: dict) -> dict:
    post = payload.get("post") if isinstance(payload, dict) else None
    if not isinstance(post, dict):
        nested_data = payload.get("data") if isinstance(payload, dict) else None
        post = nested_data.get("post") if isinstance(nested_data, dict) else None
    if not isinstance(post, dict):
        return {}

    content_html = (
        post.get("post_content")
        or post.get("post_content2")
        or post.get("post_abstract")
        or post.get("post_title")
        or ""
    )
    title = clean_chinese_text(post.get("post_title") or post.get("post_abstract") or "")
    full_text = _guba_html_fragment_to_text(content_html) or title
    if not full_text:
        return {}
    return {
        "time": post.get("post_publish_time") or "",
        "full_text": full_text,
        "title": title,
    }


def _fetch_guba_detail_api(post_id: str, proxies: dict) -> dict:
    if not proxies:
        return {}
    data = {
        "deviceid": "ugc",
        "version": "200",
        "plat": "wap",
        "product": "guba",
        "ctoken": "",
        "utoken": "",
        "postid": post_id,
        "type": "0",
        "cutword": "true",
        "paytext": "true",
        "location": "",
        "env": "prod",
        "bizfrom": "ugc",
    }
    response = requests.post(
        f"https://mguba.eastmoney.com/api/getArticle?postid={post_id}",
        headers=DETAIL_API_HEADERS,
        data=data,
        timeout=10,
        proxies=proxies,
    )
    if response.status_code != 200:
        return {}
    try:
        payload = response.json()
    except ValueError:
        return {}
    return _parse_guba_api_post_payload(payload)


def _fetch_guba_detail_via_proxy(url: str, proxies: dict | None = None) -> dict:
    if not proxies:
        return {}
    actual_url = url
    if "caifuhao" in actual_url and not actual_url.startswith("http"):
        actual_url = "https:" + actual_url
    elif not actual_url.startswith("http"):
        actual_url = "https://guba.eastmoney.com" + actual_url
    post_id = _extract_guba_post_id_from_url(actual_url)
    if post_id:
        try:
            api_detail = _fetch_guba_detail_api(post_id, proxies)
            if api_detail and api_detail.get("full_text"):
                return api_detail
        except Exception as exc:
            logger.debug("Guba API detail failed post_id=%s url=%s: %s", post_id, actual_url, exc)
    response = requests.get(actual_url, headers=LIST_HEADERS, timeout=10, proxies=proxies)
    if response.status_code != 200:
        return {}
    html = decode_chinese_response(response)
    if _is_guba_block_page(html):
        return {}
    return parse_detail_html(html)


def _fetch_guba_detail_proxy_worker(url: str, proxy_manager: ProxyManager) -> dict:
    try:
        proxies = proxy_manager.get_proxy()
    except Exception as exc:
        logger.debug("Guba detail proxy unavailable url=%s: %s", url, exc)
        return {}
    try:
        detail = _fetch_guba_detail_via_proxy(url, proxies=proxies)
    except requests.exceptions.RequestException as exc:
        logger.debug("Guba detail proxy request failed url=%s: %s", url, exc)
        proxy_manager.mark_invalid(force=True)
        return {}
    except Exception as exc:
        logger.debug("Guba detail proxy worker failed url=%s: %s", url, exc)
        return {}
    if not detail or not (detail.get("full_text") or detail.get("text")):
        proxy_manager.mark_invalid()
    return detail or {}


def _fetch_guba_details_direct_pool(urls: list[str], candidate_count: int | None = None) -> dict:
    selected_urls = urls[:DETAIL_DIRECT_LIMIT]
    results: dict[str, dict] = {}
    if not selected_urls:
        return {"data": results, "total_urls": 0, "success_count": 0, "missing_count": 0, "duration_seconds": 0.0, "total_ips_used": 0, "timed_out": False}
    started = time.perf_counter()
    timed_out = False
    proxy_manager = ProxyManager()
    with ThreadPoolExecutor(max_workers=min(DETAIL_DIRECT_WORKERS, len(selected_urls))) as executor:
        future_map = {executor.submit(_fetch_guba_detail_proxy_worker, url, proxy_manager): url for url in selected_urls}
        try:
            for future in as_completed(future_map, timeout=DETAIL_DIRECT_DEADLINE_SECONDS):
                url = future_map[future]
                try:
                    detail = future.result()
                except Exception as exc:
                    logger.debug("Guba proxy detail failed url=%s: %s", url, exc)
                    continue
                if detail and (detail.get("full_text") or detail.get("text")):
                    results[url] = detail
        except TimeoutError:
            timed_out = True
            for future in future_map:
                future.cancel()
    return {
        "data": results,
        "total_urls": len(selected_urls),
        "success_count": len(results),
        "missing_count": len(selected_urls) - len(results),
        "duration_seconds": round(time.perf_counter() - started, 2),
        "total_ips_used": int(getattr(proxy_manager, "ips_fetched", 0) or 0),
        "timed_out": timed_out,
        "detail_capped": len(selected_urls) < (candidate_count or len(urls)),
        "candidate_urls": candidate_count or len(urls),
    }


def _fetch_guba_details_proxy(urls: list[str]) -> dict:
    selected_urls = urls[:DETAIL_DIRECT_LIMIT]
    if not selected_urls:
        return {"data": {}, "total_urls": 0, "success_count": 0, "missing_count": 0, "duration_seconds": 0.0, "total_ips_used": 0, "timed_out": False}

    proxy_mode = os.environ.get("GUBA_PROXY_MODE", "direct").strip().lower()
    if proxy_mode in DIRECT_PROXY_MODES:
        return _fetch_guba_details_direct_pool(selected_urls, candidate_count=len(urls))

    scheduler = GlobalScheduler(
        selected_urls,
        max_attempts_per_url=DETAIL_MAX_ATTEMPTS_PER_URL,
        deadline_seconds=DETAIL_DEADLINE_SECONDS,
    )
    report = scheduler.run()
    report["detail_capped"] = len(selected_urls) < len(urls)
    report["candidate_urls"] = len(urls)
    return report


def _fetch_guba_detail_direct(url: str, proxies: dict | None = None) -> dict:
    return _fetch_guba_detail_via_proxy(url, proxies=proxies)


def _fetch_guba_details_direct(urls: list[str]) -> dict:
    return _fetch_guba_details_direct_pool(urls)


def fetch_guba_posts_sync(ticker: str, lookback_days: int = 7, window_start=None, window_end=None, market: str = "cn") -> list:
    crawl_started = time.perf_counter()
    logger.debug("Fetching Guba posts for %s (lookback=%sd)...", ticker, lookback_days)
    
    custom_start = _parse_window_dt(window_start)
    custom_end = _parse_window_dt(window_end)
    
    now_utc = datetime.now(timezone.utc)
    has_custom_window = bool(custom_start and custom_end and custom_end > custom_start)
    if has_custom_window:
        start_utc = custom_start
    else:
        start_utc = now_utc - timedelta(days=lookback_days)
        
    proxy_manager = ProxyManager()
    all_items = []
    page = 1
    
    # --- Step 1: Fetch list pages ---
    list_started = time.perf_counter()
    while True:
        items, success = fetch_guba_list_page(ticker, page, proxy_manager, market=market)
        if not items:
            break
            
        valid_items = []
        
        # Pinned posts at the top of page 1 might be very old (e.g. from 2021). 
        # Using the last item on the page provides a reliable chronological anchor.
        oldest_dt = items[-1]["published_dt"] if items else now_utc
        
        for it in items:
            pdt = it["published_dt"]
                
            if not _is_ticker_match(it, ticker):
                logger.debug("Dropping cross-ticker Guba URL for %s: %s", ticker, it.get("url"))
                continue

            # Window check
            if has_custom_window:
                if custom_start <= pdt < custom_end:
                    valid_items.append(it)
            else:
                if pdt >= start_utc:
                    valid_items.append(it)
                    
        all_items.extend(valid_items)
        
        # Custom windows may be older than page 1; keep paging while pages are newer than window_end.
        if has_custom_window and oldest_dt >= custom_end:
            page += 1
            if page > MAX_LIST_PAGES:
                break
            continue

        # Stop condition: if the oldest post on this page is older than our cutoff
        if oldest_dt < start_utc:
            logger.debug("Reached posts older than cutoff (%s) at page %s. Stopping list fetch.", start_utc, page)
            break
            
        page += 1
        if page > MAX_LIST_PAGES: # hard limit safety
            break
            
    list_seconds = time.perf_counter() - list_started
    list_proxy_ips = int(getattr(proxy_manager, "ips_fetched", 0) or 0)
    if not all_items:
        summary = {
            "ticker": ticker,
            "channel": "guba",
            "list_seconds": round(list_seconds, 2),
            "detail_seconds": 0,
            "total_seconds": round(time.perf_counter() - crawl_started, 2),
            "list_items": 0,
            "records": 0,
            "list_proxy_ips": list_proxy_ips,
            "detail_proxy_ips": 0,
            "detail_success": 0,
            "detail_failed": 0,
            "detail_success_rate": 0,
            "timed_out": False,
            "length_filtered": 0,
        }
        logger.info(format_social_crawl_summary(summary))
        return []
        
    # --- Step 2: Concurrently fetch full text through the configured proxy pool ---
    urls_to_fetch = list(dict.fromkeys(it["url"] for it in all_items))
    logger.debug("Dispatching %s Guba URLs to proxy-only detail workers...", len(urls_to_fetch))
    
    detail_started = time.perf_counter()
    results_report = _fetch_guba_details_proxy(urls_to_fetch)
    detail_seconds = time.perf_counter() - detail_started
    content_map = results_report.get("data", {})
    
    # --- Step 3: Merge ---
    final_records = []
    body_success = 0
    for it in all_items:
        content_dict = content_map.get(it["url"])
        full_text = ""
        if content_dict and "full_text" in content_dict:
            full_text = content_dict["full_text"] or ""
        elif content_dict and "text" in content_dict:
            full_text = content_dict["text"] or ""

        full_text = full_text.strip()
        if full_text:
            body_success += 1
        full_text = full_text or it["title"]
            
        final_records.append({
            "ticker": ticker,
            "published_at": it["published_at"],
            "source_type": "social",
            "channel": "guba",
            "source_name": it["source_name"],
            "title": it["title"],
            "summary": None,
            "content": full_text,
            "url": it["url"],
        })
        
    final_records = apply_length_relevance_filter(final_records, source_type="social")
    detail_success = int(results_report.get("success_count") or body_success)
    detail_failed = max(len(urls_to_fetch) - detail_success, 0)
    detail_total = detail_success + detail_failed
    summary = {
        "ticker": ticker,
        "channel": "guba",
        "list_seconds": round(list_seconds, 2),
        "detail_seconds": round(detail_seconds, 2),
        "total_seconds": round(time.perf_counter() - crawl_started, 2),
        "list_items": len(all_items),
        "records": len(final_records),
        "final_by_channel": dict(Counter(record.get("channel") or "unknown" for record in final_records)),
        "list_proxy_ips": list_proxy_ips,
        "detail_proxy_ips": int(results_report.get("total_ips_used") or 0),
        "detail_success": detail_success,
        "detail_failed": detail_failed,
        "detail_success_rate": round(detail_success / detail_total, 4) if detail_total else 0,
        "timed_out": bool(results_report.get("timed_out")),
        "length_filtered": sum(1 for record in final_records if record.get("is_content_relevant") is False),
    }
    logger.info(format_social_crawl_summary(summary))
    return final_records


async def fetch_guba_posts(ticker: str, lookback_days: int = 7, window_start=None, window_end=None, market: str = "cn") -> list:
    """
    Async wrapper for the completely preserved synchronous proxy/multithreaded logic.
    """
    return await asyncio.to_thread(
        fetch_guba_posts_sync,
        ticker,
        lookback_days,
        window_start,
        window_end,
        market,
    )
