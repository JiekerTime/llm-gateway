from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from llm_gateway.core.session_manager import SessionManager
from llm_gateway.models import RoleCard, RoleCardDimension


class SessionManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        db_path = Path(self._tmpdir.name) / "sessions.db"
        self.manager = SessionManager(db_path=str(db_path), default_ttl_hours=12, cleanup_interval_s=60)
        self.role_card = RoleCard(
            name="gp-expert",
            system_prompt="You are the shared GP expert persona.",
            constraints=RoleCardDimension(content="Stay precise.", priority=1),
            max_history_turns=2,
            max_history_tokens=1000,
        )

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()

    async def test_get_or_create_binds_role_card_and_caller(self) -> None:
        session = await self.manager.get_or_create(
            session_id="sess_1",
            role_card="gp-expert",
            caller="gp/task",
            dimension_overrides={"scenario": "Handle ops triage."},
        )

        self.assertEqual(session.role_card, "gp-expert")
        self.assertEqual(session.caller, "gp/task")
        self.assertEqual(session.dimension_overrides["scenario"], "Handle ops triage.")

        with self.assertRaisesRegex(ValueError, "role_card"):
            await self.manager.get_or_create(
                session_id="sess_1",
                role_card="other-card",
                caller="gp/task",
            )

        with self.assertRaisesRegex(ValueError, "caller"):
            await self.manager.get_or_create(
                session_id="sess_1",
                role_card="gp-expert",
                caller="other/task",
            )

    async def test_build_full_messages_keeps_role_prompt_and_recent_history(self) -> None:
        session = await self.manager.get_or_create(
            session_id="sess_2",
            role_card="gp-expert",
            caller="gp/task",
        )
        await self.manager.append_messages(
            session_id="sess_2",
            messages=[
                {"role": "user", "content": "turn1 user"},
                {"role": "assistant", "content": "turn1 assistant"},
                {"role": "user", "content": "turn2 user"},
                {"role": "assistant", "content": "turn2 assistant"},
                {"role": "user", "content": "turn3 user"},
                {"role": "assistant", "content": "turn3 assistant"},
            ],
            usage_tokens=123,
        )
        session = await self.manager.get("sess_2")
        self.assertIsNotNone(session)

        full_messages = await self.manager.build_full_messages(
            session=session,
            new_messages=[{"role": "user", "content": "follow-up"}],
            role_card=self.role_card,
        )

        contents = [message["content"] for message in full_messages]
        self.assertIn("You are the shared GP expert persona.", full_messages[0]["content"])
        self.assertNotIn("turn1 user", contents)
        self.assertIn("turn2 user", contents)
        self.assertIn("turn3 assistant", contents)
        self.assertEqual(contents[-1], "follow-up")

    async def test_append_messages_skips_system_and_tracks_usage(self) -> None:
        await self.manager.get_or_create(
            session_id="sess_3",
            role_card="gp-expert",
            caller="gp/task",
        )
        await self.manager.append_messages(
            session_id="sess_3",
            messages=[
                {"role": "system", "content": "ignore me"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "world"},
            ],
            usage_tokens=42,
        )

        session = await self.manager.get("sess_3")
        self.assertIsNotNone(session)
        self.assertEqual([message["role"] for message in session.messages], ["user", "assistant"])
        self.assertEqual(session.total_tokens_used, 42)


if __name__ == "__main__":
    unittest.main()
