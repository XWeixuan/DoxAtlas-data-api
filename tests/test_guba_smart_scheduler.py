from __future__ import annotations

import os
import unittest
from unittest import mock

from cn_hk_collector.collectors import guba_client
from cn_hk_collector.collectors.guba_utils.SmartBatchCrawler import GlobalScheduler, UrlPool


class GubaSmartSchedulerTest(unittest.TestCase):
    def test_smart_scheduler_keeps_original_eight_by_eight_shape(self) -> None:
        scheduler = GlobalScheduler([f"https://example.test/{i}" for i in range(17)], deadline_seconds=1)

        self.assertEqual(len(scheduler.pools), 8)
        self.assertTrue(all(pool.num_threads == 8 for pool in scheduler.pools))
        self.assertTrue(all(pool.proxy_manager.max_ips == 15 for pool in scheduler.pools))

    def test_url_pool_respects_single_detail_attempt(self) -> None:
        pool = UrlPool(pool_id=0, urls=[], max_attempts_per_url=1)

        self.assertFalse(pool.report_failure("https://example.test/1", fatal=True))
        self.assertIn("https://example.test/1", pool.failed_urls)

    def test_api_pool_mode_uses_smart_scheduler(self) -> None:
        class FakeScheduler:
            instances: list["FakeScheduler"] = []

            def __init__(self, urls, max_attempts_per_url=1, deadline_seconds=360):
                self.urls = urls
                self.max_attempts_per_url = max_attempts_per_url
                self.deadline_seconds = deadline_seconds
                self.instances.append(self)

            def run(self):
                return {
                    "data": {},
                    "total_urls": len(self.urls),
                    "success_count": 0,
                    "missing_count": len(self.urls),
                    "duration_seconds": 0.0,
                    "total_ips_used": 0,
                    "timed_out": False,
                }

        with mock.patch.dict(os.environ, {"GUBA_PROXY_MODE": "api_pool"}), mock.patch.object(
            guba_client, "GlobalScheduler", FakeScheduler
        ), mock.patch.object(guba_client, "_fetch_guba_details_direct_pool") as direct_pool:
            report = guba_client._fetch_guba_details_proxy(["https://example.test/a"])

        direct_pool.assert_not_called()
        self.assertEqual(len(FakeScheduler.instances), 1)
        self.assertEqual(FakeScheduler.instances[0].max_attempts_per_url, guba_client.DETAIL_MAX_ATTEMPTS_PER_URL)
        self.assertEqual(FakeScheduler.instances[0].deadline_seconds, guba_client.DETAIL_DEADLINE_SECONDS)
        self.assertEqual(report["candidate_urls"], 1)

    def test_direct_mode_keeps_standalone_local_path(self) -> None:
        expected = {"data": {}, "total_urls": 1, "success_count": 0, "missing_count": 1}

        with mock.patch.dict(os.environ, {"GUBA_PROXY_MODE": "direct"}), mock.patch.object(
            guba_client, "_fetch_guba_details_direct_pool", return_value=expected
        ) as direct_pool, mock.patch.object(guba_client, "GlobalScheduler") as scheduler:
            report = guba_client._fetch_guba_details_proxy(["https://example.test/a"])

        direct_pool.assert_called_once()
        scheduler.assert_not_called()
        self.assertIs(report, expected)

    def test_parse_mguba_api_list_items_uses_content_when_title_is_empty(self) -> None:
        payload = {
            "rc": 1,
            "re": [
                {
                    "post_id": 1726810225,
                    "post_title": "",
                    "post_content": "<p>贵州茅台今日成交活跃，投资者继续讨论分红。</p>",
                    "post_publish_time": "2026-06-15 21:01:11",
                    "post_user": {"user_nickname": "tester"},
                }
            ],
        }

        rows = guba_client._parse_mguba_api_list_items(payload, "600519", "cn")

        self.assertEqual(len(rows), 1)
        self.assertIn("贵州茅台", rows[0]["title"])
        self.assertIn("投资者继续讨论分红", rows[0]["summary"])
        self.assertEqual(rows[0]["source_name"], "tester")
        self.assertEqual(rows[0]["url"], "https://mguba.eastmoney.com/mguba/article/0/1726810225")


if __name__ == "__main__":
    unittest.main()
