import logging
import os
import threading
import time

from dotenv import load_dotenv

from .proxy_provider import fetch_proxy_server

load_dotenv()


class ProxyNode:
    def __init__(self, ip: str):
        self.ip = ip
        self.active_threads = 0
        self.success_count = 0
        self.normal_error_count = 0
        self.fatal_error_count = 0
        self.is_dead = False
        self.fetch_time = time.time()
        self.lock = threading.Lock()

    def report(self, is_success: bool, is_fatal: bool = False):
        with self.lock:
            if is_success:
                self.success_count += 1
                self.fatal_error_count = 0
            elif is_fatal:
                self.fatal_error_count += 1
                if self.fatal_error_count >= 3:
                    self.is_dead = True
            else:
                self.normal_error_count += 1


class SmartProxyManager:
    def __init__(self, max_ips: int = 20):
        self.api_url = os.environ.get("GUBA_PROXY_API_URL", "").strip()
        self.ttl = int(os.environ.get("GUBA_PROXY_TTL_SECONDS", 180))
        self.max_ips = max_ips

        self.ips_fetched = 0
        self.lock = threading.Lock()

    def fetch_new_proxy(self) -> ProxyNode:
        if not self.api_url:
            raise Exception("GUBA_PROXY_API_URL is required but not set. MUST use external IP proxy.")

        with self.lock:
            if self.ips_fetched >= self.max_ips:
                raise Exception(f"Pool proxy limit reached: {self.max_ips}")

            logging.debug("SmartProxyManager fetching new IP...")
            try:
                ip = fetch_proxy_server(self.api_url, timeout=10)
                self.ips_fetched += 1
                logging.debug("Successfully fetched proxy: %s (%s/%s)", ip, self.ips_fetched, self.max_ips)
                return ProxyNode(ip)
            except Exception as exc:
                logging.debug("Failed to fetch proxy: %s", exc)
                raise
