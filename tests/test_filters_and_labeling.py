from __future__ import annotations

import unittest
from importlib.resources import files

from cn_hk_collector.content_filters import MEDIA_CONTENT_MAX_CHARS, SOCIAL_CONTENT_MAX_CHARS, apply_length_relevance_filter
from cn_hk_collector.media_content_relevance import evaluate_content_relevance
from cn_hk_collector.runner import label_media_records


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


if __name__ == "__main__":
    unittest.main()
