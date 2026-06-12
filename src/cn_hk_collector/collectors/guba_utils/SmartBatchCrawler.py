import logging
import queue
import threading
import time
from typing import Any, Callable, Dict, List

import requests

from cn_hk_collector.collectors.chinese_text import decode_chinese_response
from .Parser import parse_detail_html
from .SmartProxyManager import ProxyNode, SmartProxyManager

ParserFunc = Callable[[str], dict]


class WorkerThread(threading.Thread):
    def __init__(self, pool_id: int, thread_id: int, url_queue: queue.Queue, pool: "UrlPool"):
        super().__init__(daemon=True)
        self.pool_id = pool_id
        self.thread_id = thread_id
        self.url_queue = url_queue
        self.pool = pool
        self.slot_id = self.thread_id % self.pool.num_slots
        self.header = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/91.0.4472.124 Safari/537.36"
            ),
        }

    def run(self):
        while not self.pool.stop_event.is_set():
            try:
                url = self.url_queue.get(timeout=1)
            except queue.Empty:
                continue

            try:
                node = self._get_valid_node()
                if node == "EXHAUSTED":
                    self.url_queue.put(url)
                    self.pool.mark_exhausted()
                    break
                if not node:
                    self.url_queue.put(url)
                    time.sleep(2)
                    continue

                success, fatal, data = self._fetch_url(url, node)
                node.report(is_success=success, is_fatal=fatal)

                if success:
                    self.pool.report_success(url, data)
                elif self.pool.report_failure(url, fatal):
                    self.url_queue.put(url)
                    time.sleep(1)
            finally:
                self.url_queue.task_done()

    def _get_valid_node(self):
        with self.pool.slots_lock:
            node = self.pool.slots[self.slot_id]
            if node and (node.is_dead or (time.time() - node.fetch_time > self.pool.proxy_manager.ttl)):
                node = None
                self.pool.slots[self.slot_id] = None

            if not node:
                try:
                    node = self.pool.proxy_manager.fetch_new_proxy()
                    self.pool.slots[self.slot_id] = node
                except Exception as exc:
                    logging.debug("Pool %s slot %s failed to fetch proxy: %s", self.pool_id, self.slot_id, exc)
                    if "limit reached" in str(exc):
                        return "EXHAUSTED"
                    return None
            return node

    def _fetch_url(self, url: str, node: ProxyNode):
        actual_url = url
        if "caifuhao" in url and not url.startswith("http"):
            actual_url = "https:" + url
        elif not url.startswith("http"):
            actual_url = "http://guba.eastmoney.com" + url

        proxies = {"http": f"http://{node.ip}", "https": f"http://{node.ip}"} if node.ip else {}
        try:
            resp = requests.get(actual_url, headers=self.header, timeout=10, proxies=proxies)
            if resp.status_code in [403, 429]:
                logging.debug("Fatal HTTP %s for URL %s using proxy %s", resp.status_code, actual_url, node.ip)
                return False, True, None
            if resp.status_code != 200:
                logging.debug("Normal HTTP %s for URL %s using proxy %s", resp.status_code, actual_url, node.ip)
                return False, False, None

            html = decode_chinese_response(resp)
            if "验证码" in html or "访问过于频繁" in html or "sys-guard" in html:
                logging.debug("Captcha/block detected for URL %s using proxy %s", actual_url, node.ip)
                return False, True, None

            return True, False, self.pool.parser_func(html)
        except requests.exceptions.RequestException as exc:
            logging.debug(
                "Network error for URL %s using proxy %s: %s - %s",
                actual_url,
                node.ip,
                type(exc).__name__,
                exc,
            )
            return False, True, None
        except Exception as exc:
            logging.debug("Unknown error for URL %s using proxy %s: %s", actual_url, node.ip, exc)
            return False, False, None


class UrlPool:
    def __init__(
        self,
        pool_id: int,
        urls: List[str],
        max_ips: int = 20,
        num_threads: int = 8,
        num_slots: int = 4,
        max_attempts_per_url: int = 1,
        parser_func: ParserFunc | None = None,
    ):
        self.pool_id = pool_id
        self.queue = queue.Queue()
        for url in urls:
            self.queue.put(url)

        self.num_threads = num_threads
        self.num_slots = num_slots
        self.proxy_manager = SmartProxyManager(max_ips=max_ips)
        self.slots: List[ProxyNode] = [None] * num_slots
        self.slots_lock = threading.Lock()

        self.stop_event = threading.Event()
        self.exhausted = False

        self.success_count = 0
        self.fatal_errors = 0
        self.normal_errors = 0
        self.max_attempts_per_url = max(1, max_attempts_per_url)
        self.parser_func = parser_func or parse_detail_html
        self.attempt_counts: Dict[str, int] = {}
        self.failed_urls = set()

        self.results: Dict[str, dict] = {}
        self.stats_lock = threading.Lock()
        self.threads: List[WorkerThread] = []

    def start(self):
        for i in range(self.num_threads):
            worker = WorkerThread(self.pool_id, i, self.queue, self)
            worker.start()
            self.threads.append(worker)

    def stop(self):
        self.stop_event.set()
        for worker in self.threads:
            worker.join(timeout=2)

    def mark_exhausted(self):
        self.exhausted = True

    def report_success(self, url: str, data: dict):
        with self.stats_lock:
            self.success_count += 1
            self.results[url] = data

    def report_failure(self, url: str, fatal: bool) -> bool:
        with self.stats_lock:
            if fatal:
                self.fatal_errors += 1
            else:
                self.normal_errors += 1

            attempts = self.attempt_counts.get(url, 0) + 1
            self.attempt_counts[url] = attempts
            if attempts >= self.max_attempts_per_url:
                self.failed_urls.add(url)
                return False
            return True

    def get_remaining_urls(self) -> List[str]:
        remaining = []
        while not self.queue.empty():
            try:
                remaining.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return remaining


class GlobalScheduler:
    def __init__(
        self,
        urls: List[str],
        max_attempts_per_url: int = 1,
        deadline_seconds: int = 360,
        parser_func: ParserFunc | None = None,
    ):
        self.total_urls = len(urls)
        self.start_time = time.time()
        self.max_attempts_per_url = max(1, max_attempts_per_url)
        self.deadline_seconds = max(1, deadline_seconds)
        self.parser_func = parser_func or parse_detail_html
        self.timed_out = False

        num_pools = 8
        chunk_size = (self.total_urls + num_pools - 1) // num_pools if self.total_urls else 0
        self.pools: List[UrlPool] = []

        for i in range(num_pools):
            chunk = urls[i * chunk_size : (i + 1) * chunk_size] if chunk_size else []
            self.pools.append(
                UrlPool(
                    pool_id=i,
                    urls=chunk,
                    max_ips=15,
                    num_threads=8,
                    num_slots=4,
                    max_attempts_per_url=self.max_attempts_per_url,
                    parser_func=self.parser_func,
                )
            )

    def run(self) -> Dict[str, Any]:
        logging.debug(
            "GlobalScheduler started with %s URLs across %s pools.",
            self.total_urls,
            len(self.pools),
        )
        for pool in self.pools:
            pool.start()

        while True:
            if time.time() - self.start_time >= self.deadline_seconds:
                self.timed_out = True
                logging.warning("GlobalScheduler deadline reached. Returning partial detail results.")
                break

            time.sleep(1)
            all_done = True
            active_pools = []

            for pool in self.pools:
                if not pool.queue.empty():
                    all_done = False
                if not pool.exhausted and not pool.queue.empty():
                    active_pools.append(pool)

            self._log_stats()

            if all_done:
                logging.debug("All queues are empty. Detail fetch finished.")
                break

            if not active_pools:
                logging.warning("All active pools are exhausted. Returning partial detail results.")
                break

            exhausted_urls = []
            for pool in self.pools:
                if pool.exhausted and not pool.queue.empty():
                    exhausted_urls.extend(pool.get_remaining_urls())

            if exhausted_urls:
                logging.debug("Re-balancing %s URLs from exhausted pools.", len(exhausted_urls))
                for i, url in enumerate(exhausted_urls):
                    active_pools[i % len(active_pools)].queue.put(url)

            idle_pools = [pool for pool in self.pools if not pool.exhausted and pool.queue.empty()]
            busy_pools = [pool for pool in self.pools if not pool.exhausted and pool.queue.qsize() > 10]
            if idle_pools and busy_pools:
                busy_pools.sort(key=lambda pool: pool.queue.qsize(), reverse=True)
                for idle_pool in idle_pools:
                    busiest_pool = busy_pools[0]
                    steal_count = busiest_pool.queue.qsize() // 2
                    for _ in range(steal_count):
                        try:
                            idle_pool.queue.put(busiest_pool.queue.get_nowait())
                        except queue.Empty:
                            break
                    busy_pools.sort(key=lambda pool: pool.queue.qsize(), reverse=True)

        for pool in self.pools:
            pool.stop()

        return self._aggregate_results()

    def _log_stats(self):
        if not logging.getLogger().isEnabledFor(logging.DEBUG):
            return
        for pool in self.pools:
            logging.debug(
                "Pool %s | Q: %s | Succ: %s | F-Err: %s | N-Err: %s | IPs: %s/15 | Exhausted: %s",
                pool.pool_id,
                pool.queue.qsize(),
                pool.success_count,
                pool.fatal_errors,
                pool.normal_errors,
                pool.proxy_manager.ips_fetched,
                pool.exhausted,
            )

    def _aggregate_results(self) -> Dict[str, Any]:
        final_results = {}
        total_success = 0
        total_ips = 0
        remaining_urls = []
        failed_urls = []

        for pool in self.pools:
            final_results.update(pool.results)
            total_success += pool.success_count
            total_ips += pool.proxy_manager.ips_fetched
            remaining_urls.extend(pool.get_remaining_urls())
            failed_urls.extend(sorted(pool.failed_urls))

        duration = time.time() - self.start_time
        return {
            "total_urls": self.total_urls,
            "success_count": total_success,
            "missing_count": self.total_urls - total_success,
            "duration_seconds": round(duration, 2),
            "total_ips_used": total_ips,
            "data": final_results,
            "remaining_urls": remaining_urls,
            "failed_urls": failed_urls,
            "timed_out": self.timed_out,
            "deadline_seconds": self.deadline_seconds,
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    test_urls = [f"http://guba.eastmoney.com/news,600519,{i}.html" for i in range(100)]
    scheduler = GlobalScheduler(test_urls)
    results = scheduler.run()
    print("Final Report:")
    print(f"Duration: {results['duration_seconds']}s")
    print(f"Success: {results['success_count']} / {results['total_urls']}")
    print(f"IPs Used: {results['total_ips_used']}")
