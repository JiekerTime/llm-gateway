from __future__ import annotations

import os
import unittest
from tempfile import NamedTemporaryFile

from llm_gateway.config import load_config


class ConfigTests(unittest.TestCase):
    def test_api_key_can_be_overridden_by_environment(self) -> None:
        old_value = os.environ.get("LLM_GATEWAY_API_KEY")
        try:
            os.environ["LLM_GATEWAY_API_KEY"] = "env-key"
            with NamedTemporaryFile("w", encoding="utf-8", suffix=".yaml") as fh:
                fh.write('api_key: "file-key"\n')
                fh.flush()

                config = load_config(fh.name)

            self.assertEqual(config["api_key"], "env-key")
        finally:
            if old_value is None:
                os.environ.pop("LLM_GATEWAY_API_KEY", None)
            else:
                os.environ["LLM_GATEWAY_API_KEY"] = old_value


if __name__ == "__main__":
    unittest.main()
