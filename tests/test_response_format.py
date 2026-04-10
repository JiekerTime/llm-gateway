from __future__ import annotations

import unittest

from pydantic import ValidationError

from llm_gateway.core.incremental_json import IncrementalJSONParser
from llm_gateway.core.prompt_logger import PromptLogger
from llm_gateway.models import ChatRequest


class ResponseFormatModelTests(unittest.TestCase):
    def test_json_schema_requires_schema_payload(self) -> None:
        with self.assertRaises(ValidationError):
            ChatRequest.model_validate(
                {
                    "messages": [{"role": "user", "content": "hi"}],
                    "caller": "test/invalid",
                    "response_format": {"type": "json_schema"},
                }
            )

    def test_json_schema_accepts_schema_alias(self) -> None:
        request = ChatRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "hi"}],
                "caller": "test/json",
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "demo",
                        "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                        "strict": True,
                    },
                },
            }
        )

        self.assertIsNotNone(request.response_format)
        self.assertEqual(request.response_format.type, "json_schema")
        self.assertIsNotNone(request.response_format.json_schema)
        self.assertEqual(request.response_format.json_schema.name, "demo")
        self.assertEqual(
            request.response_format.json_schema.schema_["properties"]["ok"]["type"],
            "boolean",
        )


class PromptLoggerTests(unittest.TestCase):
    def test_recent_logs_include_response_format_type(self) -> None:
        logger = PromptLogger(enabled=False)
        logger.log_call(
            caller="test/json",
            backend="ollama",
            model="qwen2.5:7b",
            response_format_type="json_schema",
            prompt_tokens=10,
            completion_tokens=5,
            latency_ms=42,
            status="ok",
        )

        logs = logger.get_recent(limit=1)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["response_format_type"], "json_schema")

    def test_recent_logs_filter_by_caller_prefix(self) -> None:
        logger = PromptLogger(enabled=False)
        logger.log_call(
            caller="test/smoke-json",
            backend="deepseek",
            model="deepseek-chat",
            stream=True,
            response_format_type="json_object",
            prompt_tokens=10,
            completion_tokens=5,
            latency_ms=42,
            status="ok",
        )

        logs = logger.get_recent(limit=5, caller="test/smoke")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["caller"], "test/smoke-json")


class IncrementalJSONParserTests(unittest.TestCase):
    def test_snapshot_recovers_partial_object(self) -> None:
        parser = IncrementalJSONParser()
        parser.append('{"title":"abc","score":')
        self.assertEqual(parser.snapshot(), {"title": "abc", "score": None})

    def test_snapshot_recovers_partial_array_and_string(self) -> None:
        parser = IncrementalJSONParser()
        parser.append('{"tags":["a","b')
        self.assertEqual(parser.snapshot(), {"tags": ["a", "b"]})

    def test_non_json_prefix_falls_back_to_none(self) -> None:
        parser = IncrementalJSONParser()
        parser.append("hello")
        self.assertIsNone(parser.snapshot())


if __name__ == "__main__":
    unittest.main()
