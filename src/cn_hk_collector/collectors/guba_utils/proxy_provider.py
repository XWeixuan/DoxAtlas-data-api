from __future__ import annotations

import json
import os
import re
import threading
import time
from urllib.parse import quote

import requests

_PROXY_SERVER_RE = re.compile(r"^[A-Za-z0-9.-]+:\d{2,5}$")
_FETCH_LOCK = threading.Lock()
_LAST_FETCH_AT = 0.0


def _min_fetch_interval_seconds() -> float:
    raw_value = str(os.environ.get("GUBA_PROXY_API_MIN_INTERVAL_SECONDS") or "0").strip()
    try:
        return max(0.0, float(raw_value))
    except ValueError:
        return 0.0


def _wait_for_fetch_slot() -> None:
    global _LAST_FETCH_AT
    min_interval = _min_fetch_interval_seconds()
    if min_interval <= 0:
        return
    with _FETCH_LOCK:
        now = time.monotonic()
        wait_seconds = _LAST_FETCH_AT + min_interval - now
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        _LAST_FETCH_AT = time.monotonic()


def _is_proxy_server(value: str) -> bool:
    return bool(_PROXY_SERVER_RE.match((value or "").strip()))


def _iter_text_candidates(text: str):
    for candidate in re.split(r"[\r\n,]+", text or ""):
        candidate = candidate.strip()
        if candidate:
            yield candidate


def parse_proxy_api_response(text: str) -> str:
    value = (text or "").strip()
    if not value:
        raise ValueError("Proxy API returned an empty response")

    if value.startswith("{"):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Proxy API returned invalid JSON: {exc}") from exc

        code = str(payload.get("code") or "").upper()
        if code and code != "SUCCESS":
            request_id = payload.get("request_id") or payload.get("requestId") or ""
            suffix = f" request_id={request_id}" if request_id else ""
            raise ValueError(f"Proxy API returned {code}{suffix}")

        data = payload.get("data")
        if isinstance(data, dict):
            data = [data]
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    for key in ("server", "proxy", "proxy_server", "ip_port", "addr"):
                        candidate = str(item.get(key) or "").strip()
                        if _is_proxy_server(candidate):
                            return candidate
                elif isinstance(item, str):
                    for candidate in _iter_text_candidates(item):
                        if _is_proxy_server(candidate):
                            return candidate

        for key in ("server", "proxy", "proxy_server", "ip_port", "addr"):
            candidate = str(payload.get(key) or "").strip()
            if _is_proxy_server(candidate):
                return candidate

        raise ValueError("Proxy API JSON did not contain a usable server field")

    for candidate in _iter_text_candidates(value):
        if _is_proxy_server(candidate):
            return candidate

    raise ValueError(f"Invalid proxy response: {value[:120]}")


def fetch_proxy_server(api_url: str, timeout: int = 10) -> str:
    _wait_for_fetch_slot()
    session = requests.Session()
    session.trust_env = False
    response = session.get(api_url, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(f"Proxy API HTTP {response.status_code}")
    return parse_proxy_api_response(response.text)


def _proxy_auth() -> tuple[str, str]:
    username = (
        os.environ.get("GUBA_PROXY_AUTH_USER")
        or os.environ.get("GUBA_PROXY_AUTH_USERNAME")
        or os.environ.get("GUBA_PROXY_AUTHKEY")
        or ""
    ).strip()
    password = (
        os.environ.get("GUBA_PROXY_AUTH_PASSWORD")
        or os.environ.get("GUBA_PROXY_AUTHPWD")
        or ""
    ).strip()
    return username, password


def format_proxy_url(proxy_server: str) -> str:
    proxy_server = (proxy_server or "").strip()
    username, password = _proxy_auth()
    if username and password:
        return f"http://{quote(username, safe='')}:{quote(password, safe='')}@{proxy_server}"
    return f"http://{proxy_server}"


def format_requests_proxies(proxy_server: str) -> dict:
    if not proxy_server:
        return {}
    proxy_url = format_proxy_url(proxy_server)
    return {"http": proxy_url, "https": proxy_url}
