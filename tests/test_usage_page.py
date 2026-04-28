from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from llm_gateway import app as app_module


class UsagePageTests(unittest.TestCase):
    def test_usage_page_serves_visual_dashboard(self) -> None:
        client = TestClient(app_module.app)

        response = client.get("/usage")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("LLM Gateway Usage", response.text)
        self.assertIn("/usage/sources", response.text)
        self.assertIn("Services", response.text)
        self.assertIn("by_service", response.text)
        self.assertIn("recent_heavy_calls", response.text)


if __name__ == "__main__":
    unittest.main()
