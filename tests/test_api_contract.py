from __future__ import annotations

import os
import unittest
from importlib.resources import files
from unittest import mock

from doxatlas_data_api.config import get_settings


class ApiContractTest(unittest.TestCase):
    def test_default_security_config_allows_only_hong_kong_server(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = get_settings()

        self.assertEqual(settings.port, 8028)
        self.assertEqual(settings.allowed_client_ips, {"43.135.22.202"})
        self.assertFalse(settings.disable_ip_allowlist)
        self.assertFalse(settings.trust_proxy_headers)

    def test_schema_has_task_state_and_one_time_pull_marker(self) -> None:
        schema = files("cn_hk_collector").joinpath("schema.sql").read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE IF NOT EXISTS crawler_tasks", schema)
        self.assertIn("status TEXT NOT NULL DEFAULT 'queued'", schema)
        self.assertIn("pulled_at TIMESTAMPTZ", schema)

    def test_protocol_documents_required_endpoints(self) -> None:
        protocol_path = os.path.join(os.path.dirname(__file__), "..", "API_PROTOCOL.md")
        with open(protocol_path, encoding="utf-8") as handle:
            protocol = handle.read()

        self.assertIn("POST /v1/crawl-tasks", protocol)
        self.assertIn("GET /v1/crawl-tasks/{task_id}", protocol)
        self.assertIn("POST /v1/crawl-tasks/{task_id}/pull", protocol)
        self.assertIn("43.135.22.202", protocol)


if __name__ == "__main__":
    unittest.main()
