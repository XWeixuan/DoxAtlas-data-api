import logging
import os
import threading
import time

from dotenv import load_dotenv

from .proxy_provider import fetch_proxy_server, format_requests_proxies

load_dotenv()


class MaxProxyException(Exception):
    pass


class ProxyManager:
    def __init__(self):
        self.api_url = os.environ.get("GUBA_PROXY_API_URL", "").strip()
        self.ttl = int(os.environ.get("GUBA_PROXY_TTL_SECONDS", 180))
        self.max_ips = int(os.environ.get("GUBA_PROXY_MAX_PER_TASK", 3))
        self.mode = os.environ.get("GUBA_PROXY_MODE", "direct").strip().lower()

        self.current_proxy = None
        self.fetch_time = 0
        self.ips_fetched = 0
        self.lock = threading.Lock()

        self.current_proxy_fail_count = 0
        self.fail_threshold = 3

        if not self.api_url and self.mode == "api_pool":
            logging.debug("GUBA_PROXY_API_URL is not set. Proxy manager will not work correctly.")

    def get_proxy(self) -> dict:
        if self.mode in {"direct", "none", "off"}:
            return {}

        if not self.api_url:
            raise Exception("GUBA_PROXY_API_URL is required but not set. MUST use external IP proxy.")

        with self.lock:
            now = time.time()
            if self.current_proxy and (now - self.fetch_time < self.ttl):
                return self._format_proxy(self.current_proxy)

            if self.ips_fetched >= self.max_ips:
                logging.debug("Proxy fetch limit reached: %s. Cannot fetch more IPs for this task.", self.max_ips)
                raise MaxProxyException(f"Max proxy fetch limit {self.max_ips} reached.")

            self._fetch_new_proxy()
            self.current_proxy_fail_count = 0
            return self._format_proxy(self.current_proxy)

    def _fetch_new_proxy(self):
        try:
            logging.debug("Fetching new proxy IP...")
            self.current_proxy = fetch_proxy_server(self.api_url, timeout=10)
            self.fetch_time = time.time()
            self.ips_fetched += 1
            logging.debug(
                "Successfully fetched new proxy: %s (used %s/%s)",
                self.current_proxy,
                self.ips_fetched,
                self.max_ips,
            )
        except Exception as exc:
            logging.debug("Error fetching new proxy: %s", exc)
            raise

    def mark_invalid(self, force=False):
        with self.lock:
            if not self.current_proxy:
                return
            if force:
                logging.debug("Force marking proxy %s as invalid.", self.current_proxy)
                self.current_proxy = None
                self.fetch_time = 0
                self.current_proxy_fail_count = 0
            else:
                self.current_proxy_fail_count += 1
                if self.current_proxy_fail_count >= self.fail_threshold:
                    logging.debug("Proxy %s failed %s times. Marking invalid.", self.current_proxy, self.fail_threshold)
                    self.current_proxy = None
                    self.fetch_time = 0
                    self.current_proxy_fail_count = 0

    def _format_proxy(self, proxy_ip: str) -> dict:
        return format_requests_proxies(proxy_ip)
