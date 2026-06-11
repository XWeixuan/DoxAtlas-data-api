import os
import time
import logging
import requests
import threading
from dotenv import load_dotenv

# 加载环境变量
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
        self.fail_threshold = 3  # consecutive distinct URL failures
        
        if not self.api_url and self.mode == "api_pool":
            logging.debug("GUBA_PROXY_API_URL is not set. Proxy manager will not work correctly.")

    def get_proxy(self) -> dict:
        """
        获取当前可用代理，如果已过期或被标记为失效，则重新获取。
        返回格式为 requests 所需的 proxies 字典。
        """
        if self.mode in {"direct", "none", "off"}:
            return {}

        if not self.api_url:
            raise Exception("GUBA_PROXY_API_URL is required but not set. MUST use external IP proxy.")

        with self.lock:
            now = time.time()
            if self.current_proxy and (now - self.fetch_time < self.ttl):
                return self._format_proxy(self.current_proxy)
            
            if self.ips_fetched >= self.max_ips:
                logging.debug(f"Proxy fetch limit reached: {self.max_ips}. Cannot fetch more IPs for this task.")
                raise MaxProxyException(f"Max proxy fetch limit {self.max_ips} reached.")

            self._fetch_new_proxy()
            self.current_proxy_fail_count = 0
            return self._format_proxy(self.current_proxy)

    def _fetch_new_proxy(self):
        try:
            logging.debug("Fetching new proxy IP...")
            response = requests.get(self.api_url, timeout=10)
            if response.status_code == 200:
                ip = response.text.strip()
                if "," in ip:
                    ip = ip.split(",")[0]
                
                # 严格校验，防止返回“未检索到满足要求的代理IP”等中文字符串
                if ":" not in ip or "{" in ip or "<" in ip or "error" in ip.lower() or "未检索到" in ip:
                    logging.debug(f"Failed to fetch proxy, unexpected API response: {ip}")
                    time.sleep(2)  # 强制休眠 2 秒，防止 API 接口被并发刷爆
                    raise Exception(f"Invalid proxy response: {ip}")
                
                self.current_proxy = ip
                self.fetch_time = time.time()
                self.ips_fetched += 1
                logging.debug(f"Successfully fetched new proxy: {self.current_proxy} (used {self.ips_fetched}/{self.max_ips})")
            else:
                logging.debug(f"Failed to fetch proxy, HTTP {response.status_code}")
                time.sleep(2)
                raise Exception(f"Failed to fetch proxy HTTP {response.status_code}")
        except Exception as e:
            logging.debug(f"Error fetching new proxy: {e}")
            raise e

    def mark_invalid(self, force=False):
        """
        遇到严重封禁或验证码时调用，强制下一次请求重新获取 IP。
        普通错误累积达到阈值后也会废弃。
        """
        with self.lock:
            if not self.current_proxy:
                return
            if force:
                logging.debug(f"Force marking proxy {self.current_proxy} as invalid.")
                self.current_proxy = None
                self.fetch_time = 0
                self.current_proxy_fail_count = 0
            else:
                self.current_proxy_fail_count += 1
                if self.current_proxy_fail_count >= self.fail_threshold:
                    logging.debug(f"Proxy {self.current_proxy} failed {self.fail_threshold} times. Marking invalid.")
                    self.current_proxy = None
                    self.fetch_time = 0
                    self.current_proxy_fail_count = 0

    def _format_proxy(self, proxy_ip: str) -> dict:
        if not proxy_ip:
            return {}
        return {
            "http": f"http://{proxy_ip}",
            "https": f"http://{proxy_ip}"
        }
