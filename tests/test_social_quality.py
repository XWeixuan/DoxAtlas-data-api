from __future__ import annotations

import unittest

from cn_hk_collector.social_quality import annotate_social_records


class SocialQualityTest(unittest.TestCase):
    def test_quality_filter_drops_noise_but_keeps_signal_posts(self) -> None:
        rows = [
            {
                "title": "$大唐发电(SH601991)$",
                "content": "$大唐发电(SH601991)$",
                "url": "https://example.test/drop-marker",
                "published_at": "2026-06-13T01:00:00+00:00",
            },
            {
                "title": "煤价下降利好业绩，火电利润弹性会兑现",
                "content": "煤价下降利好业绩，火电利润弹性会兑现",
                "url": "https://example.test/keep-signal",
                "published_at": "2026-06-13T02:00:00+00:00",
            },
        ]

        annotated = annotate_social_records(
            rows,
            window_start="2026-06-10T00:00:00+00:00",
            window_end="2026-06-17T00:00:00+00:00",
        )

        self.assertFalse(annotated[0]["is_content_relevant"])
        self.assertEqual(annotated[0]["social_quality_tier"], "drop")
        self.assertTrue(annotated[0]["content_relevance_reason"].startswith("quality_drop:"))
        self.assertTrue(annotated[1]["is_content_relevant"])
        self.assertTrue(annotated[1]["social_selected_for_analysis"])
        self.assertIn(annotated[1]["social_quality_tier"], {"medium", "high"})

    def test_sampling_keeps_full_payload_and_marks_unselected_rows(self) -> None:
        rows = []
        for index in range(10):
            rows.append(
                {
                    "title": f"业绩改善和煤价下降逻辑第{index}条",
                    "content": f"业绩改善和煤价下降逻辑第{index}条，预计利润修复，市场关注分红和资产质量。",
                    "url": f"https://example.test/post-{index}",
                    "published_at": "2026-06-13T02:00:00+00:00",
                }
            )

        annotated = annotate_social_records(
            rows,
            window_start="2026-06-10T00:00:00+00:00",
            window_end="2026-06-17T00:00:00+00:00",
            quota_per_7d=3,
        )

        self.assertEqual(len(annotated), 10)
        self.assertEqual(sum(1 for row in annotated if row["social_selected_for_analysis"]), 3)
        self.assertEqual(sum(1 for row in annotated if row["is_content_relevant"]), 3)
        self.assertTrue(
            any(str(row["content_relevance_reason"]).startswith("sampling_excluded:quota_3") for row in annotated)
        )

    def test_detail_requirement_skips_complete_short_text(self) -> None:
        rows = [
            {
                "title": "涨停了",
                "content": "涨停了",
                "url": "https://example.test/short-complete",
                "published_at": "2026-06-13T02:00:00+00:00",
            },
            {
                "title": "业绩预增公告带来火电利润弹性",
                "summary": "业绩预增公告带来火电利润弹性，因为煤价下降、发电量增长、分红预期提升，市场可能重新定价...",
                "content": "业绩预增公告带来火电利润弹性，因为煤价下降、发电量增长、分红预期提升，市场可能重新定价...",
                "url": "https://example.test/truncated",
                "published_at": "2026-06-13T03:00:00+00:00",
            },
        ]

        annotated = annotate_social_records(
            rows,
            window_start="2026-06-10T00:00:00+00:00",
            window_end="2026-06-17T00:00:00+00:00",
        )

        self.assertTrue(annotated[0]["social_selected_for_analysis"])
        self.assertFalse(annotated[0]["social_detail_required"])
        self.assertTrue(annotated[1]["social_selected_for_analysis"])
        self.assertTrue(annotated[1]["social_detail_required"])


if __name__ == "__main__":
    unittest.main()
