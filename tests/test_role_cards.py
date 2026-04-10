from __future__ import annotations

import unittest

from fastapi import HTTPException

from llm_gateway import app as app_module
from llm_gateway.core.role_card_registry import RoleCardRegistry
from llm_gateway.models import ChatRequest, RoleCard, RoleCardDimension


class RoleCardModelTests(unittest.TestCase):
    def test_build_system_prompt_orders_sections_and_applies_overrides(self) -> None:
        card = RoleCard(
            name="architect",
            display_name="GP Architect",
            system_prompt="Stay rigorous.",
            personality=RoleCardDimension(content="Analytical.", priority=5),
            constraints=RoleCardDimension(content="Do not invent facts.", priority=10),
            extra_dimensions={
                "audience": RoleCardDimension(content="Write for operators.", priority=7),
            },
        )

        prompt = card.build_system_prompt(
            {
                "personality": "Analytical and terse.",
                "audience": "Write for engineering leads.",
            }
        )

        self.assertIn("## Identity\nYou are GP Architect.", prompt)
        self.assertIn("## Core Instructions\nStay rigorous.", prompt)
        self.assertIn("## Personality\nAnalytical and terse.", prompt)
        self.assertIn("## Audience\nWrite for engineering leads.", prompt)
        self.assertIn("## Constraints\nDo not invent facts.", prompt)
        self.assertLess(prompt.index("## Personality"), prompt.index("## Audience"))
        self.assertLess(prompt.index("## Audience"), prompt.index("## Constraints"))


class StatelessRoleCardPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_chat_request_injects_role_card_without_session(self) -> None:
        original_registry = app_module._role_card_registry
        try:
            registry = RoleCardRegistry()
            registry.load_from_config(
                {
                    "gp-expert": {
                        "display_name": "GP Expert",
                        "system_prompt": "Hold the expert baseline.",
                        "constraints": {"content": "Answer precisely.", "priority": 1},
                    }
                }
            )
            app_module._role_card_registry = registry

            prepared = await app_module._prepare_chat_request(
                ChatRequest(
                    messages=[
                        {"role": "system", "content": "Caller-specific instruction."},
                        {"role": "user", "content": "Explain the tradeoff."},
                    ],
                    caller="gp/test",
                    role_card="gp-expert",
                )
            )

            self.assertEqual(prepared.role_card_name, "gp-expert")
            self.assertEqual(prepared.messages[0]["role"], "system")
            self.assertIn("Hold the expert baseline.", prepared.messages[0]["content"])
            self.assertEqual(prepared.messages[1]["content"], "Caller-specific instruction.")
            self.assertEqual(prepared.messages[2]["content"], "Explain the tradeoff.")
        finally:
            app_module._role_card_registry = original_registry

    async def test_prepare_chat_request_rejects_unknown_stateless_role_card(self) -> None:
        with self.assertRaises(HTTPException):
            await app_module._prepare_chat_request(
                ChatRequest(
                    messages=[{"role": "user", "content": "hi"}],
                    caller="gp/test",
                    role_card="missing-card",
                )
            )


if __name__ == "__main__":
    unittest.main()
