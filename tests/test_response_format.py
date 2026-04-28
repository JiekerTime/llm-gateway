from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from llm_gateway.backends.deepseek import _apply_deepseek_v4_options
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

    def test_deepseek_thinking_accepts_bool_and_dict_shapes(self) -> None:
        enabled = ChatRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "hi"}],
                "caller": "test/thinking",
                "thinking": True,
            }
        )
        disabled = ChatRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "hi"}],
                "caller": "test/thinking",
                "thinking": {"type": "disabled"},
            }
        )

        self.assertEqual(enabled.thinking, "enabled")
        self.assertEqual(disabled.thinking, "disabled")


class DeepSeekBackendOptionTests(unittest.TestCase):
    def test_default_thinking_is_sent_via_openai_extra_body(self) -> None:
        kwargs: dict = {}

        _apply_deepseek_v4_options(
            kwargs,
            thinking=None,
            reasoning_effort=None,
            default_thinking="disabled",
        )

        self.assertEqual(kwargs["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertNotIn("reasoning_effort", kwargs)

    def test_reasoning_effort_is_only_sent_when_thinking_enabled(self) -> None:
        kwargs: dict = {}

        _apply_deepseek_v4_options(
            kwargs,
            thinking="enabled",
            reasoning_effort="max",
            default_thinking="disabled",
        )

        self.assertEqual(kwargs["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertEqual(kwargs["reasoning_effort"], "max")


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
            model="deepseek-v4-flash",
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

    def test_usage_analysis_groups_by_source_and_caller(self) -> None:
        with TemporaryDirectory() as log_dir:
            logger = PromptLogger(log_dir=log_dir, enabled=True)
            logger.log_call(
                caller="gp/weekly-health",
                backend="deepseek",
                model="deepseek-v4-flash",
                role_card="gp-default-expert",
                session_id="sess_gp",
                prompt_tokens=100,
                completion_tokens=50,
                cache_hit_tokens=40,
                latency_ms=1000,
                status="ok",
            )
            logger.log_call(
                caller="media/subtitle",
                backend="deepseek",
                model="deepseek-v4-pro",
                prompt_tokens=20,
                completion_tokens=10,
                latency_ms=2000,
                status="ok",
            )

            analysis = logger.get_usage_analysis(
                since=datetime.now(timezone.utc) - timedelta(hours=1),
                limit=10,
            )

            self.assertEqual(analysis["total"]["calls"], 2)
            self.assertEqual(analysis["total"]["total_tokens"], 180)
            self.assertEqual(analysis["by_service"][0]["service"], "group-portrait")
            self.assertEqual(analysis["by_service"][0]["total_tokens"], 150)
            self.assertEqual(analysis["by_source"][0]["source"], "gp")
            self.assertEqual(analysis["by_source"][0]["total_tokens"], 150)
            self.assertEqual(analysis["by_source"][0]["top_callers"][0]["caller"], "gp/weekly-health")
            self.assertEqual(analysis["by_caller"][0]["caller"], "gp/weekly-health")
            self.assertEqual(analysis["by_model"][0]["model"], "deepseek-v4-flash")
            self.assertEqual(analysis["by_role_card"][0]["role_card"], "gp-default-expert")
            self.assertEqual(analysis["by_session"][0]["session"], "sess_gp")
            self.assertEqual(analysis["recent_heavy_calls"][0]["caller"], "gp/weekly-health")
            self.assertEqual(analysis["recent_heavy_calls"][0]["service"], "group-portrait")

    def test_usage_analysis_supports_caller_prefix_filter(self) -> None:
        logger = PromptLogger(enabled=False)
        logger.log_call(
            caller="gp/weekly-health",
            backend="deepseek",
            model="deepseek-v4-flash",
            prompt_tokens=100,
            completion_tokens=50,
            latency_ms=1000,
            status="ok",
        )
        logger.log_call(
            caller="media/subtitle",
            backend="deepseek",
            model="deepseek-v4-flash",
            prompt_tokens=20,
            completion_tokens=10,
            latency_ms=1000,
            status="ok",
        )

        analysis = logger.get_usage_analysis(
            since=datetime.now(timezone.utc) - timedelta(hours=1),
            caller="gp/",
        )

        self.assertEqual(analysis["total"]["calls"], 1)
        self.assertEqual(analysis["by_service"][0]["service"], "group-portrait")
        self.assertEqual(analysis["by_source"][0]["source"], "gp")

    def test_usage_analysis_maps_media_autoheal_to_homeserver_service(self) -> None:
        logger = PromptLogger(enabled=False)
        logger.log_call(
            caller="hs-media-autoheal",
            backend="deepseek",
            model="deepseek-v4-flash",
            prompt_tokens=20,
            completion_tokens=5,
            latency_ms=1000,
            status="ok",
        )

        analysis = logger.get_usage_analysis(
            since=datetime.now(timezone.utc) - timedelta(hours=1),
        )

        self.assertEqual(analysis["by_service"][0]["service"], "homeserver-cli/media-autoheal")


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
