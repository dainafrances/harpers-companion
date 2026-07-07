from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("OPENROUTER_API_KEY", "test-key")

memory = importlib.import_module("src.memory")
recall = importlib.import_module("src.discord_recall")
router = importlib.import_module("src.router")


class DiscordRecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        memory.DB_PATH = Path(self.tempdir.name) / "test.sqlite3"
        memory.init_db()
        self.permissions = recall.RecallPermissions(guild_ids={700}, channel_ids={500})

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def message(
        self,
        message_id: int,
        speaker_id: int,
        speaker_name: str,
        content: str,
        *,
        channel_id: int = 500,
        guild_id: int = 700,
        timestamp: str = "2026-07-07T12:00:00+00:00",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            id=message_id,
            content=content,
            created_at=datetime.fromisoformat(timestamp),
            guild=SimpleNamespace(id=guild_id),
            channel=SimpleNamespace(id=channel_id, name="the-nest"),
            author=SimpleNamespace(
                id=speaker_id,
                name=speaker_name,
                display_name=speaker_name,
            ),
        )

    def test_allowed_message_is_indexed_with_metadata(self) -> None:
        indexed = recall.index_message(
            self.message(10, 42, "Rachael", "Evidence, not vibes."),
            permissions=self.permissions,
            source="observed-human",
        )

        self.assertTrue(indexed)
        rows = memory.search_recall_messages(allowed_channel_ids={"500"}, limit=1)
        self.assertEqual(rows[0]["message_id"], "10")
        self.assertEqual(rows[0]["speaker_user_id"], "42")
        self.assertEqual(rows[0]["speaker_name"], "Rachael")
        self.assertEqual(rows[0]["guild_id"], "700")
        self.assertEqual(rows[0]["channel_id"], "500")
        self.assertEqual(rows[0]["channel_name"], "the-nest")

    def test_denied_channel_is_not_indexed(self) -> None:
        indexed = recall.index_message(
            self.message(11, 42, "Rachael", "Secret side room", channel_id=999),
            permissions=self.permissions,
            source="observed-human",
        )

        self.assertFalse(indexed)
        self.assertEqual(memory.search_recall_messages(limit=10), [])

    def test_latest_and_second_latest_are_sorted_by_discord_timestamp(self) -> None:
        recall.index_message(
            self.message(20, 42, "Rachael", "Older", timestamp="2026-07-07T12:00:00+00:00"),
            permissions=self.permissions,
            source="observed-human",
        )
        recall.index_message(
            self.message(21, 42, "Rachael", "Newer", timestamp="2026-07-07T12:05:00+00:00"),
            permissions=self.permissions,
            source="observed-human",
        )

        status, rows, note = recall.retrieve_for_query(
            "What is the latest thing from Rachael?",
            guild_id=700,
            channel_id=500,
            permissions=self.permissions,
        )
        self.assertEqual(status, recall.RecallStatus.COMPLETE)
        self.assertEqual(rows[0]["content"], "Newer")
        self.assertIn("approved", note)

        status, rows, _ = recall.retrieve_for_query(
            "What is the second latest thing from Rachael?",
            guild_id=700,
            channel_id=500,
            permissions=self.permissions,
        )
        self.assertEqual(status, recall.RecallStatus.COMPLETE)
        self.assertEqual(rows[0]["content"], "Older")

    def test_same_display_name_can_be_disambiguated_by_discord_id(self) -> None:
        recall.index_message(
            self.message(30, 42, "Alex", "First Alex"),
            permissions=self.permissions,
            source="observed-human",
        )
        recall.index_message(
            self.message(31, 99, "Alex", "Second Alex"),
            permissions=self.permissions,
            source="observed-human",
        )

        status, rows, _ = recall.retrieve_for_query(
            "latest from <@99>",
            guild_id=700,
            channel_id=500,
            permissions=self.permissions,
        )
        self.assertEqual(status, recall.RecallStatus.COMPLETE)
        self.assertEqual(rows[0]["speaker_user_id"], "99")
        self.assertEqual(rows[0]["content"], "Second Alex")

    def test_permission_limited_status_for_unapproved_channel(self) -> None:
        status, rows, note = recall.retrieve_for_query(
            "Can you see the other conversation?",
            guild_id=700,
            channel_id=999,
            permissions=self.permissions,
        )

        self.assertEqual(status, recall.RecallStatus.PERMISSION_LIMITED)
        self.assertEqual(rows, [])
        self.assertIn("not approved", note)

    def test_formatted_context_has_receipts_and_attribution(self) -> None:
        recall.index_message(
            self.message(40, 42, "Rachael", "Receipts here."),
            permissions=self.permissions,
            source="observed-human",
        )
        context = recall.build_retrieval_context_for_prompt(
            "What is the latest thing from Rachael?",
            guild_id=700,
            channel_id=500,
            permissions=self.permissions,
        )

        self.assertIsNotNone(context)
        assert context is not None
        self.assertIn("[DISCORD_RETRIEVAL]", context)
        self.assertIn("status: COMPLETE", context)
        self.assertIn("speaker_name: Rachael", context)
        self.assertIn("speaker_user_id: 42", context)
        self.assertIn("content: Receipts here.", context)


class RouterRecallContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_discord_retrieval_context_is_injected_with_policy(self) -> None:
        create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="Reply"))]
            )
        )
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        with (
            patch.object(router, "_client", fake_client),
            patch.object(router, "build_system_prompt", return_value="IDENTITY"),
        ):
            reply = await router.generate_companion_reply(
                user_text="What did I miss?",
                history=[],
                latest_journal=None,
                is_dm=False,
                speaker_name="Daina",
                speaker_is_owner=True,
                discord_retrieval_context="[DISCORD_RETRIEVAL]\nstatus: COMPLETE\n[/DISCORD_RETRIEVAL]",
            )

        self.assertEqual(reply, "Reply")
        sent_messages = create.await_args.kwargs["messages"]
        contents = [message["content"] for message in sent_messages if message["role"] == "system"]
        self.assertTrue(any("DISCORD RETRIEVAL EVIDENCE POLICY" in item for item in contents))
        self.assertTrue(any("status: COMPLETE" in item for item in contents))


if __name__ == "__main__":
    unittest.main()
