from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")

continuity = importlib.import_module("src.continuity")
main = importlib.import_module("src.main")
memory = importlib.import_module("src.memory")


class FakeAuthor:
    def __init__(self, user_id: int, name: str, *, bot: bool = False) -> None:
        self.id = user_id
        self.name = name
        self.display_name = name
        self.global_name = name
        self.bot = bot


class FakeChannel:
    def __init__(self, channel_id: int, name: str) -> None:
        self.id = channel_id
        self.name = name
        self.sent: list[tuple[str, dict]] = []

    async def send(self, content: str, **kwargs) -> None:
        self.sent.append((content, kwargs))

    def typing(self):
        class TypingContext:
            async def __aenter__(self):
                return None

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        return TypingContext()


class FakeMessage:
    def __init__(
        self,
        message_id: int,
        author: FakeAuthor,
        content: str,
        *,
        guild_id: int | None,
        guild_name: str | None,
        channel_id: int,
        channel_name: str,
        timestamp: str,
    ) -> None:
        self.id = message_id
        self.author = author
        self.content = content
        self.guild = (
            None
            if guild_id is None
            else SimpleNamespace(id=guild_id, name=guild_name or str(guild_id))
        )
        self.channel = FakeChannel(channel_id, channel_name)
        self.created_at = datetime.fromisoformat(timestamp)
        self.attachments: list[object] = []
        self.embeds: list[object] = []
        self.reactions: list[object] = []
        self.added_reactions: list[str] = []
        self.replies: list[tuple[str, dict]] = []

    async def add_reaction(self, emoji: str) -> None:
        self.added_reactions.append(emoji)

    async def reply(self, content: str, **kwargs) -> None:
        self.replies.append((content, kwargs))


class FakeBot:
    def __init__(self, user: FakeAuthor) -> None:
        self.user = user


class ContinuityMainIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = memory.DB_PATH
        memory.DB_PATH = Path(self.tempdir.name) / "test.sqlite3"
        memory.init_db()

        self.owner = FakeAuthor(42, "Daina")
        self.colin = FakeAuthor(77, "Colin", bot=True)
        self.config = continuity.config_from_values(
            guild_zones="100:nest;200:cabin;300:harpers",
            channel_routes="100:101;200:201;300:301",
        )
        self.patches = (
            patch.object(main, "bot", FakeBot(self.colin)),
            patch.object(main, "owner_id", self.owner.id),
            patch.object(main, "continuity_config", self.config),
            patch.object(main, "CONTINUITY_HANDOFF_LIMIT", 12),
            patch.object(main, "CONTINUITY_HANDOFF_MAX_AGE_MINUTES", 120),
        )
        for active_patch in self.patches:
            active_patch.start()

    def tearDown(self) -> None:
        for active_patch in reversed(self.patches):
            active_patch.stop()
        memory.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def message(
        self,
        message_id: int,
        content: str,
        *,
        guild_id: int = 100,
        guild_name: str = "The Nest",
        channel_id: int = 101,
        channel_name: str = "everyone",
        timestamp: str = "2026-10-03T12:00:00+00:00",
        author: FakeAuthor | None = None,
    ) -> FakeMessage:
        return FakeMessage(
            message_id,
            author or self.owner,
            content,
            guild_id=guild_id,
            guild_name=guild_name,
            channel_id=channel_id,
            channel_name=channel_name,
            timestamp=timestamp,
        )

    def seed_event(
        self,
        event_id: str,
        content: str,
        *,
        guild_id: int,
        guild_name: str,
        channel_id: int,
        channel_name: str,
        zone: str,
        speaker: FakeAuthor,
        timestamp: str,
        role: str | None = None,
        source: str | None = None,
    ) -> None:
        memory.save_continuity_event(
            event_id=event_id,
            guild_id=str(guild_id),
            guild_name=guild_name,
            channel_id=str(channel_id),
            channel_name=channel_name,
            continuity_zone=zone,
            speaker_user_id=str(speaker.id),
            speaker_name=speaker.display_name,
            speaker_is_bot=speaker.bot,
            role=role or ("assistant" if speaker.bot else "user"),
            content=content,
            event_timestamp=timestamp,
            source=source or ("generated-colin" if speaker.bot else "observed-human"),
        )

    def test_transition_uses_actual_newest_owner_or_colin_event(self) -> None:
        self.seed_event(
            "owner-nest",
            "Older Nest exchange",
            guild_id=100,
            guild_name="The Nest",
            channel_id=101,
            channel_name="everyone",
            zone="nest",
            speaker=self.owner,
            timestamp="2026-10-03T10:00:00+00:00",
        )
        self.seed_event(
            "colin-cabin",
            "Newest Cabin exchange",
            guild_id=200,
            guild_name="The Cabin",
            channel_id=201,
            channel_name="beside-the-fire",
            zone="cabin",
            speaker=self.colin,
            timestamp="2026-10-03T11:00:00+00:00",
        )
        current = self.message(
            500,
            "Now in The Harpers",
            guild_id=300,
            guild_name="The Harpers",
            channel_id=301,
            channel_name="the-hearth",
        )

        inputs = main._build_continuity_prompt_inputs(current, is_dm=False)

        self.assertIsNotNone(inputs.writer_context)
        assert inputs.writer_context is not None
        self.assertIn("Newest Cabin exchange", inputs.writer_context)
        self.assertIn("Older Nest exchange", inputs.writer_context)
        self.assertEqual(
            inputs.routine_contents,
            ("Older Nest exchange", "Newest Cabin exchange"),
        )
        self.assertEqual(inputs.private_origin_contents, ())
        self.assertIsNotNone(inputs.auditor_context)

    def test_same_room_activity_does_not_create_stale_handoff(self) -> None:
        self.seed_event(
            "older-nest",
            "Old other-room material",
            guild_id=100,
            guild_name="The Nest",
            channel_id=101,
            channel_name="everyone",
            zone="nest",
            speaker=self.owner,
            timestamp="2026-10-03T09:00:00+00:00",
        )
        self.seed_event(
            "current-cabin",
            "Already arrived in this room",
            guild_id=200,
            guild_name="The Cabin",
            channel_id=201,
            channel_name="beside-the-fire",
            zone="cabin",
            speaker=self.colin,
            timestamp="2026-10-03T11:00:00+00:00",
        )
        current = self.message(
            501,
            "Continue here",
            guild_id=200,
            guild_name="The Cabin",
            channel_id=201,
            channel_name="beside-the-fire",
        )

        self.assertEqual(
            main._build_continuity_prompt_inputs(current, is_dm=False),
            main.ContinuityPromptInputs(),
        )

    def test_recent_awareness_samples_every_other_configured_server(self) -> None:
        self.seed_event(
            "cabin-recent",
            "Recent Cabin context",
            guild_id=200,
            guild_name="The Cabin",
            channel_id=201,
            channel_name="beside-the-fire",
            zone="cabin",
            speaker=self.owner,
            timestamp="2026-10-03T11:50:00+00:00",
        )
        self.seed_event(
            "harpers-recent",
            "Recent Harpers context",
            guild_id=300,
            guild_name="The Harpers",
            channel_id=301,
            channel_name="the-hearth",
            zone="harpers",
            speaker=self.colin,
            timestamp="2026-10-03T11:55:00+00:00",
        )
        current = self.message(
            515,
            "Back in The Nest",
            timestamp="2026-10-03T12:00:00+00:00",
        )

        inputs = main._build_continuity_prompt_inputs(current, is_dm=False)

        assert inputs.writer_context is not None
        self.assertIn("Recent Cabin context", inputs.writer_context)
        self.assertIn("Recent Harpers context", inputs.writer_context)
        self.assertEqual(
            inputs.private_origin_contents,
            ("Recent Cabin context", "Recent Harpers context"),
        )
        self.assertIsNotNone(inputs.auditor_context)

    def test_arrival_greeting_does_not_consume_the_prior_room_handoff(self) -> None:
        ben = FakeAuthor(88, "Ben", bot=True)
        scandal = "Ben disclosed the scandal just before the room change."
        self.seed_event(
            "ben-scandal-before-arrival",
            scandal,
            guild_id=100,
            guild_name="The Nest",
            channel_id=101,
            channel_name="everyone",
            zone="nest",
            speaker=ben,
            timestamp="2026-10-03T11:56:00+00:00",
        )
        self.seed_event(
            "owner-moves",
            "Come with me, Moose.",
            guild_id=100,
            guild_name="The Nest",
            channel_id=101,
            channel_name="everyone",
            zone="nest",
            speaker=self.owner,
            timestamp="2026-10-03T11:58:00+00:00",
        )
        self.seed_event(
            "arrival-greeting",
            "Are you here?",
            guild_id=200,
            guild_name="The Cabin",
            channel_id=201,
            channel_name="beside-the-fire",
            zone="cabin",
            speaker=self.owner,
            timestamp="2026-10-03T12:00:00+00:00",
        )
        self.seed_event(
            "colin-arrives",
            "I'm here.",
            guild_id=200,
            guild_name="The Cabin",
            channel_id=201,
            channel_name="beside-the-fire",
            zone="cabin",
            speaker=self.colin,
            timestamp="2026-10-03T12:01:00+00:00",
        )
        second_message = self.message(
            512,
            "Can you believe what Ben just said?",
            guild_id=200,
            guild_name="The Cabin",
            channel_id=201,
            channel_name="beside-the-fire",
            timestamp="2026-10-03T12:02:00+00:00",
        )

        inputs = main._build_continuity_prompt_inputs(second_message, is_dm=False)

        self.assertIsNotNone(inputs.writer_context)
        assert inputs.writer_context is not None
        self.assertIn(scandal, inputs.writer_context)

    def test_nest_scandal_from_another_speaker_follows_owner_into_cabin(self) -> None:
        ben = FakeAuthor(88, "Ben", bot=True)
        scandal = "Ben publicly revealed the ceremonial potato incident."
        self.seed_event(
            "ben-scandal",
            scandal,
            guild_id=100,
            guild_name="The Nest",
            channel_id=101,
            channel_name="everyone",
            zone="nest",
            speaker=ben,
            timestamp="2026-10-03T10:58:00+00:00",
        )
        self.seed_event(
            "owner-leaves-nest",
            "Come with me, Moose.",
            guild_id=100,
            guild_name="The Nest",
            channel_id=101,
            channel_name="everyone",
            zone="nest",
            speaker=self.owner,
            timestamp="2026-10-03T11:00:00+00:00",
        )
        current = self.message(
            508,
            "Well. That was scandalous.",
            guild_id=200,
            guild_name="The Cabin",
            channel_id=201,
            channel_name="beside-the-fire",
        )

        inputs = main._build_continuity_prompt_inputs(current, is_dm=False)

        self.assertIsNotNone(inputs.writer_context)
        assert inputs.writer_context is not None
        self.assertIn(scandal, inputs.writer_context)
        self.assertIn('"speaker_name": "Ben"', inputs.writer_context)
        self.assertIn('"zone": "cabin"', inputs.writer_context)
        self.assertIsNotNone(inputs.auditor_context)

    def test_current_non_owner_speaker_can_anchor_the_room_transition(self) -> None:
        rachael = FakeAuthor(99, "Rachael")
        ben = FakeAuthor(88, "Ben", bot=True)
        scandal = "Ben announced the scandal before Rachael changed rooms."
        self.seed_event(
            "ben-before-rachael",
            scandal,
            guild_id=100,
            guild_name="The Nest",
            channel_id=101,
            channel_name="everyone",
            zone="nest",
            speaker=ben,
            timestamp="2026-10-03T10:58:00+00:00",
        )
        self.seed_event(
            "rachael-leaves-nest",
            "I'm going to the Cabin.",
            guild_id=100,
            guild_name="The Nest",
            channel_id=101,
            channel_name="everyone",
            zone="nest",
            speaker=rachael,
            timestamp="2026-10-03T11:00:00+00:00",
        )
        current = self.message(
            510,
            "That was a lot.",
            guild_id=200,
            guild_name="The Cabin",
            channel_id=201,
            channel_name="beside-the-fire",
            author=rachael,
        )

        inputs = main._build_continuity_prompt_inputs(current, is_dm=False)

        self.assertIsNotNone(inputs.writer_context)
        assert inputs.writer_context is not None
        self.assertIn(scandal, inputs.writer_context)
        self.assertIn('"speaker_name": "Ben"', inputs.writer_context)
        self.assertEqual(inputs.private_origin_contents, ())

    def test_private_transition_is_visible_to_colin_and_marked_for_judgement(self) -> None:
        secret = "Harpers-only confidence"
        self.seed_event(
            "private-owner",
            secret,
            guild_id=300,
            guild_name="The Harpers",
            channel_id=301,
            channel_name="the-hearth",
            zone="harpers",
            speaker=self.owner,
            timestamp="2026-10-03T11:00:00+00:00",
        )
        current = self.message(502, "Back in public")

        inputs = main._build_continuity_prompt_inputs(current, is_dm=False)

        self.assertIsNotNone(inputs.writer_context)
        assert inputs.writer_context is not None
        self.assertIn(secret, inputs.writer_context)
        self.assertIn('"disclosure": "PRIVATE_ORIGIN"', inputs.writer_context)
        self.assertIsNotNone(inputs.auditor_context)
        assert inputs.auditor_context is not None
        self.assertIn(secret, inputs.auditor_context)
        self.assertEqual(inputs.routine_contents, ())
        self.assertEqual(inputs.private_origin_contents, (secret,))
        self.assertEqual(inputs.couple_private_contents, (secret,))

    def test_couple_private_context_excludes_other_people_private_words(self) -> None:
        ben = FakeAuthor(88, "Ben", bot=True)
        owner_secret = "Daina's private detail"
        colin_secret = "Colin's private reply"
        ben_secret = "Ben's private statement"
        shared = {
            "guild_id": 300,
            "guild_name": "The Harpers",
            "channel_id": 301,
            "channel_name": "the-hearth",
            "zone": "harpers",
        }
        self.seed_event(
            "owner-private",
            owner_secret,
            speaker=self.owner,
            timestamp="2026-10-03T11:00:00+00:00",
            **shared,
        )
        self.seed_event(
            "colin-private",
            colin_secret,
            speaker=self.colin,
            timestamp="2026-10-03T11:01:00+00:00",
            **shared,
        )
        self.seed_event(
            "ben-private",
            ben_secret,
            speaker=ben,
            source="observed-companion-bot",
            timestamp="2026-10-03T11:02:00+00:00",
            **shared,
        )
        current = self.message(
            516,
            "You have my permission to share our Harpers exchange here.",
            timestamp="2026-10-03T12:00:00+00:00",
        )

        inputs = main._build_continuity_prompt_inputs(current, is_dm=False)

        self.assertEqual(
            inputs.couple_private_contents,
            (owner_secret, colin_secret),
        )
        self.assertEqual(
            inputs.private_origin_contents,
            (owner_secret, colin_secret, ben_secret),
        )

    def test_private_handoff_remains_sealed_after_a_public_arrival_greeting(self) -> None:
        secret = "The Harpers-only confidence survives as audit evidence."
        self.seed_event(
            "private-before-public-arrival",
            secret,
            guild_id=300,
            guild_name="The Harpers",
            channel_id=301,
            channel_name="the-hearth",
            zone="harpers",
            speaker=self.owner,
            timestamp="2026-10-03T11:58:00+00:00",
        )
        self.seed_event(
            "public-arrival",
            "We're back.",
            guild_id=100,
            guild_name="The Nest",
            channel_id=101,
            channel_name="everyone",
            zone="nest",
            speaker=self.owner,
            timestamp="2026-10-03T12:00:00+00:00",
        )
        self.seed_event(
            "public-colin-reply",
            "Here.",
            guild_id=100,
            guild_name="The Nest",
            channel_id=101,
            channel_name="everyone",
            zone="nest",
            speaker=self.colin,
            timestamp="2026-10-03T12:01:00+00:00",
        )
        second_public_message = self.message(
            513,
            "And now we carry on publicly.",
            timestamp="2026-10-03T12:02:00+00:00",
        )

        inputs = main._build_continuity_prompt_inputs(
            second_public_message,
            is_dm=False,
        )

        self.assertIsNotNone(inputs.writer_context)
        self.assertIsNotNone(inputs.auditor_context)
        assert inputs.auditor_context is not None
        self.assertIn(secret, inputs.auditor_context)
        self.assertIn(secret, inputs.writer_context or "")

    async def test_observed_inbound_is_recorded_once_with_verbatim_content(self) -> None:
        exact = "  Keep spacing.\n🪿  "
        message = self.message(503, exact)

        self.assertTrue(main.save_observed_message(message, source="observed-human"))
        self.assertFalse(main.save_observed_message(message, source="observed-human"))

        rows = memory.get_recent_continuity_events_from_channel_before(
            guild_id="100",
            channel_id="101",
            before_timestamp="2026-10-04T00:00:00+00:00",
            limit=10,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_id"], "503")
        self.assertEqual(rows[0]["content"], exact)

    async def test_handler_passes_gate_inputs_and_records_reply_after_send(self) -> None:
        secret = "Do not repeat the private reveal"
        self.seed_event(
            "private-before-move",
            secret,
            guild_id=300,
            guild_name="The Harpers",
            channel_id=301,
            channel_name="the-hearth",
            zone="harpers",
            speaker=self.owner,
            timestamp="2026-10-03T11:00:00+00:00",
        )
        memory.save_journal_entry(title="Private journal", content="Journal detail")
        message = self.message(504, "Colin, public now")
        response = main.CompanionResponse(reply_text="Safe public reply", reaction_emojis=("💚",))

        with (
            patch.object(
                main,
                "generate_companion_reply",
                new=AsyncMock(return_value=response),
            ) as generate,
            patch.object(
                main.discord_recall,
                "build_retrieval_context_for_prompt",
            ) as legacy_recall,
        ):
            await main.handle_chat_message(
                message,
                "Colin, public now",
                is_dm=False,
                source="human-direct",
            )

        kwargs = generate.await_args.kwargs
        self.assertIsNone(kwargs["latest_journal"])
        self.assertIn(secret, kwargs["continuity_writer_context"])
        self.assertIn(secret, kwargs["continuity_auditor_context"])
        self.assertEqual(kwargs["continuity_routine_contents"], ())
        self.assertEqual(kwargs["continuity_private_origin_contents"], (secret,))
        self.assertEqual(kwargs["continuity_couple_private_contents"], (secret,))
        self.assertEqual(kwargs["direct_owner_message_text"], "Colin, public now")
        legacy_recall.assert_not_called()
        self.assertEqual(message.added_reactions, ["💚"])
        self.assertEqual(message.channel.sent[0][0], "Safe public reply")

        rows = memory.get_recent_continuity_events_from_channel_before(
            guild_id="100",
            channel_id="101",
            before_timestamp="2030-01-01T00:00:00+00:00",
            limit=10,
        )
        self.assertEqual([row["event_id"] for row in rows], ["504", "colin-reply:504"])
        self.assertEqual(rows[0]["content"], "Colin, public now")
        self.assertEqual(rows[1]["content"], "Safe public reply")

    async def test_failed_send_does_not_record_outbound_reply(self) -> None:
        message = self.message(505, "A message")
        response = main.CompanionResponse(reply_text="Unsent reply")

        with (
            patch.object(
                main,
                "generate_companion_reply",
                new=AsyncMock(return_value=response),
            ),
            patch.object(
                main,
                "send_long_message",
                new=AsyncMock(side_effect=RuntimeError("send failed")),
            ),
        ):
            await main.handle_chat_message(
                message,
                "A message",
                is_dm=False,
                source="human-direct",
            )

        rows = memory.get_recent_continuity_events_from_channel_before(
            guild_id="100",
            channel_id="101",
            before_timestamp="2030-01-01T00:00:00+00:00",
            limit=10,
        )
        self.assertEqual([row["event_id"] for row in rows], ["505"])
        legacy_messages = memory.get_recent_messages(channel_id=101, limit=10)
        self.assertFalse(any(row["role"] == "assistant" for row in legacy_messages))

    async def test_explicit_recall_gives_colin_harpers_awareness_with_private_origin(self) -> None:
        secret = "Goose and Moose private ledger detail"
        memory.save_recall_message(
            message_id="private-recall",
            guild_id="300",
            channel_id="301",
            channel_name="the-hearth",
            speaker_user_id=str(self.owner.id),
            speaker_name=self.owner.display_name,
            content=secret,
            message_timestamp="2026-10-03T11:00:00+00:00",
            source="observed-human",
        )
        permissions = main.discord_recall.RecallPermissions(
            guild_ids={100, 200, 300},
            channel_ids={101, 201, 301},
        )
        message = self.message(509, "What is the latest conversation?")

        with (
            patch.object(main, "recall_permissions", permissions),
            patch.object(
                main,
                "generate_companion_reply",
                new=AsyncMock(return_value=main.CompanionResponse(reply_text="Safe reply")),
            ) as generate,
        ):
            await main.handle_chat_message(
                message,
                message.content,
                is_dm=False,
                source="human-direct",
            )

        kwargs = generate.await_args.kwargs
        self.assertIsNone(kwargs["discord_retrieval_context"])
        self.assertIn(secret, kwargs["continuity_writer_context"])
        self.assertIn('"disclosure": "PRIVATE_ORIGIN"', kwargs["continuity_writer_context"])
        self.assertIn(secret, kwargs["continuity_auditor_context"])
        self.assertEqual(kwargs["continuity_private_origin_contents"], (secret,))

    def test_journal_is_harpers_only_when_enabled_and_legacy_when_disabled(self) -> None:
        memory.save_journal_entry(title="Continuity", content="Private journal content")
        nest = self.message(506, "Nest")
        harpers = self.message(
            507,
            "Harpers",
            guild_id=300,
            guild_name="The Harpers",
            channel_id=301,
            channel_name="the-hearth",
        )

        self.assertIsNone(main._latest_journal_for_prompt(nest, is_dm=False))
        self.assertIn(
            "Private journal content",
            main._latest_journal_for_prompt(harpers, is_dm=False) or "",
        )
        self.assertIsNone(main._latest_journal_for_prompt(nest, is_dm=True))

        disabled = continuity.config_from_values(guild_zones="", channel_routes="")
        with patch.object(main, "continuity_config", disabled):
            self.assertIn(
                "Private journal content",
                main._latest_journal_for_prompt(nest, is_dm=False) or "",
            )

    def test_malformed_config_fails_closed_for_legacy_journal_and_recall(self) -> None:
        memory.save_journal_entry(title="Continuity", content="Private journal content")
        malformed = continuity.config_from_values(
            guild_zones="100:nest;200:cabin;300:harpers",
            channel_routes="100:101;200:not-a-discord-id;300:301",
        )
        nest = self.message(511, "What is the latest conversation?")

        self.assertTrue(malformed.configured)
        self.assertFalse(malformed.enabled)
        with (
            patch.object(main, "continuity_config", malformed),
            patch.object(
                main.discord_recall,
                "build_retrieval_context_for_prompt",
            ) as legacy_recall,
        ):
            self.assertIsNone(main._latest_journal_for_prompt(nest, is_dm=False))
            self.assertEqual(
                main._discord_retrieval_for_prompt(
                    nest,
                    cleaned_content=nest.content,
                    is_dm=False,
                ),
                main.RecallPromptInputs(),
            )
        legacy_recall.assert_not_called()


if __name__ == "__main__":
    unittest.main()
