from __future__ import annotations

import unittest
from importlib.resources import files
from unittest import mock

from cn_hk_collector.content_filters import MEDIA_CONTENT_MAX_CHARS, SOCIAL_CONTENT_MAX_CHARS, apply_length_relevance_filter
from cn_hk_collector.media_content_relevance import evaluate_content_relevance
from cn_hk_collector.runner import collect_ticker, label_media_records


class FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FilterAndLabelingTest(unittest.TestCase):
    def test_social_length_filter_marks_oversized_content(self) -> None:
        [record] = apply_length_relevance_filter(
            [{"content": "x" * (SOCIAL_CONTENT_MAX_CHARS + 1), "url": "https://example.test/post"}],
            source_type="social",
        )

        self.assertIs(record["is_content_relevant"], False)
        self.assertEqual(record["content_relevance_reason"], "length_filter:content_chars>250")

    def test_media_length_filter_marks_oversized_content(self) -> None:
        [record] = apply_length_relevance_filter(
            [{"content": "x" * (MEDIA_CONTENT_MAX_CHARS + 1), "url": "https://example.test/news"}],
            source_type="media",
        )

        self.assertIs(record["is_content_relevant"], False)
        self.assertEqual(record["content_relevance_reason"], "length_filter:content_chars>5000")

    def test_relevance_rule_keeps_target_focused_record(self) -> None:
        decision = evaluate_content_relevance(
            {
                "title": "贵州茅台发布经营更新",
                "content": "贵州茅台 600519 管理层称贵州茅台渠道库存稳定。",
            },
            "600519",
            target_aliases=["贵州茅台"],
        )

        self.assertTrue(decision.is_content_relevant)
        self.assertEqual(decision.content_relevance_reason, "target_in_title_and_repeated")

    def test_label_media_records_preserves_length_filter(self) -> None:
        [record] = label_media_records(
            [
                {
                    "title": "贵州茅台长文",
                    "content": "x" * (MEDIA_CONTENT_MAX_CHARS + 1),
                    "is_content_relevant": False,
                    "content_relevance_reason": "length_filter:content_chars>5000",
                }
            ],
            ticker="600519",
            aliases=["贵州茅台"],
        )

        self.assertIs(record["is_content_relevant"], False)
        self.assertEqual(record["content_relevance_reason"], "length_filter:content_chars>5000")

    def test_schema_contains_required_tables(self) -> None:
        schema = files("cn_hk_collector").joinpath("schema.sql").read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE IF NOT EXISTS ticker_entities", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS raw_media", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS raw_social", schema)

    def test_collect_ticker_writes_labeled_media_records(self) -> None:
        captured: dict[str, list[dict]] = {}

        def fake_upsert(conn, table_name, records, *, market, ticker, task_id=None):
            captured[table_name] = list(records)
            return len(captured[table_name])

        media_rows = [
            {
                "title": "600519 business update",
                "content": "600519 management said 600519 channel inventory stayed stable.",
                "url": "https://example.test/media/1",
                "source_type": "media",
                "channel": "unit",
            },
            {
                "title": "璐靛窞鑼呭彴闀挎枃",
                "content": "x" * (MEDIA_CONTENT_MAX_CHARS + 1),
                "url": "https://example.test/media/2",
                "source_type": "media",
                "channel": "unit",
                "is_content_relevant": False,
                "content_relevance_reason": "length_filter:content_chars>5000",
            },
        ]
        social_rows = [
            {
                "title": "social",
                "content": "social body",
                "url": "https://example.test/social/1",
                "source_type": "social",
                "channel": "guba",
            }
        ]

        with mock.patch("cn_hk_collector.db.connect", return_value=FakeConnection()), mock.patch(
            "cn_hk_collector.db.upsert_raw_records", side_effect=fake_upsert
        ), mock.patch(
            "cn_hk_collector.runner.fetch_akshare_snapshot_sync", return_value={"org_short_name_cn": "600519"}
        ), mock.patch(
            "cn_hk_collector.runner.get_ticker_entity_aliases", return_value=["600519"]
        ), mock.patch(
            "cn_hk_collector.runner.fetch_cn_hk_media_sync", return_value=media_rows
        ), mock.patch(
            "cn_hk_collector.runner.fetch_guba_posts_sync", return_value=social_rows
        ):
            result = collect_ticker(market="cn", ticker="600519", task_id="task-clean-test")

        self.assertEqual(result.media_written, 2)
        self.assertEqual(result.social_written, 1)
        self.assertEqual(captured["raw_media"][0]["is_content_relevant"], True)
        self.assertEqual(captured["raw_media"][0]["content_relevance_reason"], "target_in_title_and_repeated")
        self.assertEqual(captured["raw_media"][1]["is_content_relevant"], False)
        self.assertEqual(captured["raw_media"][1]["content_relevance_reason"], "length_filter:content_chars>5000")

    def test_pull_api_returns_cleaned_raw_fields(self) -> None:
        from doxatlas_data_api import app as api_app

        payload = {
            "task": {
                "task_id": "task-clean-test",
                "market": "cn",
                "ticker": "600519",
                "status": "succeeded",
                "lookback_days": 7,
                "collect_media": True,
                "collect_social": True,
                "media_fetched": 1,
                "media_written": 1,
                "social_fetched": 0,
                "social_written": 0,
                "media_relevant": 0,
                "media_irrelevant": 1,
            },
            "raw_media": [
                {
                    "market": "cn",
                    "ticker": "600519",
                    "source_type": "media",
                    "channel": "unit",
                    "title": "cleaned",
                    "content": "cleaned content",
                    "url": "https://example.test/media/1",
                    "is_content_relevant": False,
                    "content_relevance_reason": "length_filter:content_chars>5000",
                }
            ],
            "raw_social": [],
        }

        with mock.patch.object(api_app, "connect", return_value=FakeConnection()), mock.patch.object(
            api_app, "pull_crawler_task_once", return_value=payload
        ):
            response = api_app.pull_task("task-clean-test")

        self.assertEqual(response.raw_media[0]["is_content_relevant"], False)
        self.assertEqual(response.raw_media[0]["content_relevance_reason"], "length_filter:content_chars>5000")


if __name__ == "__main__":
    unittest.main()
