from __future__ import annotations

import unittest

from cn_hk_collector.market import normalize_market, normalize_ticker
from cn_hk_collector.media_dedupe import dedupe_media_records


class MarketAndDedupeTest(unittest.TestCase):
    def test_market_defaults_to_cn_for_unknown_values(self) -> None:
        self.assertEqual(normalize_market(None), "cn")
        self.assertEqual(normalize_market("unknown"), "cn")

    def test_ticker_validation_supports_cn_and_hk(self) -> None:
        self.assertEqual(normalize_ticker("600519", "cn"), "600519")
        self.assertEqual(normalize_ticker("0700", "hk"), "0700")

    def test_media_dedupe_keeps_representative_and_duplicate_urls(self) -> None:
        records = [
            {
                "title": "腾讯控股新闻",
                "content": "腾讯控股发布业务进展",
                "published_at": "2026-06-11T00:00:00+00:00",
                "url": "https://example.test/a",
            },
            {
                "title": "腾讯控股新闻",
                "content": "腾讯控股发布业务进展",
                "published_at": "2026-06-11T00:01:00+00:00",
                "url": "https://example.test/b",
            },
        ]

        [record] = dedupe_media_records(records)

        self.assertEqual(record["duplicate_count"], 2)
        self.assertEqual(record["duplicate_urls"], ["https://example.test/a", "https://example.test/b"])


if __name__ == "__main__":
    unittest.main()
