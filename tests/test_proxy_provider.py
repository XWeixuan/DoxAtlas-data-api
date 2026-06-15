from __future__ import annotations

import os
import unittest
from unittest import mock

from cn_hk_collector.collectors.guba_utils.proxy_provider import (
    format_proxy_url,
    parse_proxy_api_response,
)


class ProxyProviderTest(unittest.TestCase):
    def test_parse_qingguo_json_response_uses_server_field(self) -> None:
        payload = """
        {
          "code": "SUCCESS",
          "data": [
            {
              "proxy_ip": "119.176.41.173",
              "server": "222.139.246.25:20055",
              "deadline": "2026-06-15 20:51:31"
            }
          ],
          "request_id": "request-id"
        }
        """

        self.assertEqual(parse_proxy_api_response(payload), "222.139.246.25:20055")

    def test_parse_legacy_text_response(self) -> None:
        self.assertEqual(parse_proxy_api_response("1.2.3.4:5678,180"), "1.2.3.4:5678")

    def test_qingguo_error_code_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "BALANCE_INSUFFICIENT"):
            parse_proxy_api_response('{"code":"BALANCE_INSUFFICIENT","request_id":"abc"}')

    def test_proxy_auth_is_added_only_when_configured(self) -> None:
        env = {"GUBA_PROXY_AUTH_USER": "user", "GUBA_PROXY_AUTH_PASSWORD": "pa:ss"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(format_proxy_url("1.2.3.4:5678"), "http://user:pa%3Ass@1.2.3.4:5678")


if __name__ == "__main__":
    unittest.main()
