import os
import time
import logging
import requests
import threading
from dotenv import load_dotenv

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
                self.fatal_error_count = 0  # Reset fatal count on success
            else:
                if is_fatal:
                    self.fatal_error_count += 1
                    # 如果连续致命错误达到3次，认为该IP失效
                    if self.fatal_error_count >= 3:
                        self.is_dead = True
                else:
                    self.normal_error_count += 1

class SmartProxyManager:
    """
    负责维护单个 Pool 的 IP 预算，支持节点状态监控和连续失败标记。
    """
    def __init__(self, max_ips: int = 20):
        self.api_url = os.environ.get("GUBA_PROXY_API_URL", "").strip()
        self.ttl = int(os.environ.get("GUBA_PROXY_TTL_SECONDS", 180))
        self.max_ips = max_ips
        
        self.ips_fetched = 0
        self.lock = threading.Lock()

    def fetch_new_proxy(self) -> ProxyNode:
        """从 API 获取一个全新代理节点，受限于本池预算"""
        if not self.api_url:
            raise Exception("GUBA_PROXY_API_URL is required but not set. MUST use external IP proxy.")

        with self.lock:
            if self.ips_fetched >= self.max_ips:
                raise Exception(f"Pool proxy limit reached: {self.max_ips}")

            logging.debug("SmartProxyManager fetching new IP...")
            try:
                response = requests.get(self.api_url, timeout=10)
                if response.status_code == 200:
                    ip = response.text.strip()
                    # 校验返回的是否是真实的 IP:PORT 格式
                    if not ip or "{" in ip or "<" in ip or "error" in ip.lower() or "未检索" in ip or "." not in ip:
                        raise Exception(f"Invalid proxy response: {ip}")
                    
                    ip = ip.split(",")[0].strip() # 去除巨量代理可能会附加的剩余存活时间 (例如 ,180)
                    
                    self.ips_fetched += 1
                    logging.debug(f"Successfully fetched proxy: {ip} ({self.ips_fetched}/{self.max_ips})")
                    return ProxyNode(ip)
                else:
                    raise Exception(f"API HTTP {response.status_code}")
            except Exception as e:
                logging.debug(f"Failed to fetch proxy: {e}")
                raise e
