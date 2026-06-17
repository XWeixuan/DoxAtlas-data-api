from __future__ import annotations

import os
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

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

    def test_detail_fetch_no_longer_caps_urls_at_legacy_limit(self) -> None:
        urls = [f"https://example.test/{index}" for index in range(650)]
        expected = {"data": {}, "total_urls": len(urls), "success_count": 0, "missing_count": len(urls)}

        with mock.patch.dict(os.environ, {"GUBA_PROXY_MODE": "direct", "GUBA_DETAIL_DIRECT_LIMIT": "500"}), mock.patch.object(
            guba_client, "_fetch_guba_details_direct_pool", return_value=expected
        ) as direct_pool:
            report = guba_client._fetch_guba_details_proxy(urls)

        sent_urls = direct_pool.call_args.args[0]
        self.assertEqual(len(sent_urls), len(urls))
        self.assertEqual(report["total_urls"], len(urls))

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

    def test_mguba_api_list_page_retries_transient_proxy_failure(self) -> None:
        class FakeProxyManager:
            def __init__(self) -> None:
                self.invalidated = 0

            def get_proxy(self) -> dict:
                return {"http": "http://proxy.test:8080", "https": "http://proxy.test:8080"}

            def mark_invalid(self, force=False) -> None:
                if force:
                    self.invalidated += 1

        class FakeResponse:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {
                    "rc": 1,
                    "re": [
                        {
                            "post_id": 1726810225,
                            "post_title": "Ningde retry test",
                            "post_publish_time": "2026-06-16 12:00:00",
                            "post_user": {"user_nickname": "tester"},
                        }
                    ],
                }

        proxy_manager = FakeProxyManager()
        with mock.patch.dict(os.environ, {"GUBA_LIST_API_MAX_ATTEMPTS": "2"}), mock.patch.object(
            guba_client.requests,
            "post",
            side_effect=[guba_client.requests.exceptions.Timeout("boom"), FakeResponse()],
        ) as post:
            rows = guba_client._fetch_mguba_api_list_page("300750", 7, "cn", proxy_manager)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Ningde retry test")
        self.assertEqual(proxy_manager.invalidated, 1)
        self.assertEqual(post.call_count, 2)

    def test_custom_window_seeks_to_older_guba_pages(self) -> None:
        calls: list[int] = []
        base_dt = datetime(2026, 6, 17, 12, tzinfo=timezone.utc)

        def fake_fetch_page(ticker, page, proxy_manager=None, market="cn", allow_html_fallback=True):
            calls.append(page)
            published_dt = base_dt - timedelta(days=page)
            return [
                {
                    "title": f"page {page}",
                    "url": f"https://mguba.eastmoney.com/mguba/article/0/{page}",
                    "source_name": "tester",
                    "published_at": published_dt.isoformat(),
                    "published_dt": published_dt,
                    "summary": "body text for retry seek test",
                }
            ], True

        with mock.patch.dict(os.environ, {"GUBA_MAX_LIST_PAGES": "64"}), mock.patch.object(
            guba_client,
            "fetch_guba_list_page",
            side_effect=fake_fetch_page,
        ), mock.patch.object(guba_client, "ProxyManager", return_value=object()), mock.patch.object(
            guba_client,
            "_fetch_guba_details_proxy",
            return_value={"data": {}, "total_urls": 1, "success_count": 0, "total_ips_used": 0, "timed_out": False},
        ):
            rows = guba_client.fetch_guba_posts_sync(
                "300750",
                window_start="2026-05-18T00:00:00+00:00",
                window_end="2026-05-19T00:00:00+00:00",
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "page 30")
        self.assertIn(30, calls)
        self.assertLess(len(calls), 15)

    def test_custom_window_list_page_failure_is_not_treated_as_end(self) -> None:
        base_dt = datetime(2026, 6, 17, 12, tzinfo=timezone.utc)

        def fake_fetch_page(ticker, page, proxy_manager=None, market="cn", allow_html_fallback=True):
            if page == 1:
                return [
                    {
                        "title": "new page",
                        "url": "https://mguba.eastmoney.com/mguba/article/0/1",
                        "source_name": "tester",
                        "published_at": base_dt.isoformat(),
                        "published_dt": base_dt,
                        "summary": "业绩改善和火电利润修复",
                    }
                ], True
            return [], False

        with mock.patch.dict(os.environ, {"GUBA_MAX_LIST_PAGES": "8"}), mock.patch.object(
            guba_client,
            "fetch_guba_list_page",
            side_effect=fake_fetch_page,
        ), mock.patch.object(guba_client, "ProxyManager", return_value=object()):
            with self.assertRaisesRegex(RuntimeError, "guba_list_page_failed:page=2"):
                guba_client.fetch_guba_posts_sync(
                    "601991",
                    window_start="2026-06-10T00:00:00+00:00",
                    window_end="2026-06-11T00:00:00+00:00",
                )


if __name__ == "__main__":
    unittest.main()
