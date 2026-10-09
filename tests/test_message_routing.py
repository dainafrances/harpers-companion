from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
os.environ.setdefault("BOT_REPLY_COOLDOWN_SECONDS", "12")

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
    def __init__(self, channel_id: int = 500, name: str = "the-nest") -> None:
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
        channel: FakeChannel,
        mentions: list[FakeAuthor] | None = None,
        reply_to: FakeAuthor | None = None,
        attachments: list[object] | None = None,
        mention_everyone: bool = False,
    ) -> None:
        self.id = message_id
        self.author = author
        self.content = content
        self.channel = channel
        self.guild = SimpleNamespace(id=700, name="Nest Guild")
        self.mentions = mentions or []
        self.mention_everyone = mention_everyone
        self.attachments = attachments or []
        self.embeds = []
        self.created_at = datetime.now(timezone.utc)
        self.reference = (
            SimpleNamespace(resolved=SimpleNamespace(author=reply_to), message_id=900)
            if reply_to is not None
            else None
        )
        self.replies: list[tuple[str, dict]] = []
        self.reactions: list[object] = []
        self.added_reactions: list[str] = []

    async def reply(self, content: str, **kwargs) -> None:
        self.replies.append((content, kwargs))

    async def add_reaction(self, emoji: str) -> None:
        self.added_reactions.append(emoji)


class FakeBot:
    def __init__(self, user: FakeAuthor) -> None:
        self.user = user
        self.process_commands = AsyncMock()


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        memory.DB_PATH = Path(self.tempdir.name) / "test.sqlite3"
        memory.init_db()
        self.colin = FakeAuthor(1, "Colin", bot=True)
        self.human = FakeAuthor(2, "Daina", bot=False)
        self.other_human = FakeAuthor(3, "Rachael", bot=False)
        self.ben = FakeAuthor(4, "Ben Morgan", bot=True)
        # Deliberately does not match Colin's configured full-name alias. Solace
        # must be recognized by her stable Discord ID.
        self.solace = FakeAuthor(1496237287825080390, "Solace D Salvatore", bot=True)
        self.channel = FakeChannel()
        self.fake_bot = FakeBot(self.colin)
        main.bot_to_bot_cooldowns.clear()
        main.bot_reply_cooldown_by_channel.clear()
        self.bot_patch = patch.object(main, "bot", self.fake_bot)
        self.bot_patch.start()
        # Keep routing expectations independent of local deployment settings.
        for attribute, value in (
            ("auto_reply_guild_ids", set()),
            ("configured_guild_ids", set()),
            ("companion_channel_ids", set()),
            ("SPONTANEOUS_REPLY_CHANCE", 0.0),
        ):
            routing_patch = patch.object(main, attribute, value)
            routing_patch.start()
            self.addCleanup(routing_patch.stop)

    def tearDown(self) -> None:
        self.bot_patch.stop()
        self.tempdir.cleanup()

    def saved_messages(self) -> list[dict[str, str]]:
        return memory.get_recent_messages(channel_id=self.channel.id, limit=100)

    async def test_unaddressed_human_is_observed_without_reply(self) -> None:
        message = FakeMessage(10, self.other_human, "Room context", channel=self.channel)
        with patch.object(main, "handle_chat_message", new=AsyncMock()) as handler:
            await main.on_message(message)
        handler.assert_not_awaited()
        self.assertIn("name=Rachael", self.saved_messages()[0]["content"])
        self.assertIn("[ROOM_CONTEXT]", self.saved_messages()[0]["content"])
        self.assertIn("guild_name: Nest Guild", self.saved_messages()[0]["content"])
        self.assertIn("channel_name: the-nest", self.saved_messages()[0]["content"])
        self.assertIn("Room context", self.saved_messages()[0]["content"])

    async def test_auto_reply_guild_answers_unaddressed_human_without_random_chance(self) -> None:
        message = FakeMessage(40, self.other_human, "  How was your day?  ", channel=self.channel)
        with (
            patch.object(main, "auto_reply_guild_ids", {message.guild.id}),
            patch.object(main, "handle_chat_message", new=AsyncMock()) as handler,
        ):
            await main.on_message(message)

        handler.assert_awaited_once_with(
            message,
            "How was your day?",
            is_dm=False,
            source="human-auto",
            reset_companion_exchange=True,
        )
        self.fake_bot.process_commands.assert_not_awaited()
        self.assertEqual(self.saved_messages(), [])

    async def test_auto_reply_guild_does_not_enable_another_guild(self) -> None:
        message = FakeMessage(41, self.human, "Ordinary conversation", channel=self.channel)
        with (
            patch.object(main, "auto_reply_guild_ids", {message.guild.id + 1}),
            patch.object(main, "handle_chat_message", new=AsyncMock()) as handler,
        ):
            await main.on_message(message)

        handler.assert_not_awaited()
        self.assertIn("Ordinary conversation", self.saved_messages()[0]["content"])
        self.fake_bot.process_commands.assert_awaited_once_with(message)

    async def test_auto_reply_still_obeys_configured_guild_allowlist(self) -> None:
        message = FakeMessage(42, self.human, "Outside allowed guilds", channel=self.channel)
        with (
            patch.object(main, "auto_reply_guild_ids", {message.guild.id}),
            patch.object(main, "configured_guild_ids", {message.guild.id + 1}),
            patch.object(main, "handle_chat_message", new=AsyncMock()) as handler,
        ):
            await main.on_message(message)

        handler.assert_not_awaited()
        self.assertEqual(self.saved_messages(), [])
        self.fake_bot.process_commands.assert_awaited_once_with(message)

    async def test_auto_reply_still_obeys_companion_channel_allowlist(self) -> None:
        message = FakeMessage(43, self.human, "Outside allowed channels", channel=self.channel)
        with (
            patch.object(main, "auto_reply_guild_ids", {message.guild.id}),
            patch.object(main, "companion_channel_ids", {self.channel.id + 1}),
            patch.object(main, "handle_chat_message", new=AsyncMock()) as handler,
        ):
            await main.on_message(message)

        handler.assert_not_awaited()
        self.assertEqual(self.saved_messages(), [])
        self.fake_bot.process_commands.assert_awaited_once_with(message)

    async def test_auto_reply_guild_keeps_unaddressed_bot_observation_only(self) -> None:
        message = FakeMessage(44, self.ben, "Just talking to the room", channel=self.channel)
        with (
            patch.object(main, "auto_reply_guild_ids", {message.guild.id}),
            patch.object(main, "handle_chat_message", new=AsyncMock()) as handler,
        ):
            await main.on_message(message)

        handler.assert_not_awaited()
        self.assertIn("Just talking to the room", self.saved_messages()[0]["content"])

    async def test_auto_reply_guild_ignores_own_messages(self) -> None:
        message = FakeMessage(45, self.colin, "My own answer", channel=self.channel)
        with (
            patch.object(main, "auto_reply_guild_ids", {message.guild.id}),
            patch.object(main, "handle_chat_message", new=AsyncMock()) as handler,
        ):
            await main.on_message(message)

        handler.assert_not_awaited()
        self.assertEqual(self.saved_messages(), [])
        self.fake_bot.process_commands.assert_not_awaited()

    async def test_auto_reply_guild_preserves_explicit_trigger_sources(self) -> None:
        messages = (
            (
                FakeMessage(46, self.human, "<@1> hello", channel=self.channel, mentions=[self.colin]),
                "hello",
                "human-direct",
            ),
            (
                FakeMessage(47, self.human, "Colin, hello", channel=self.channel),
                "Colin, hello",
                "human-direct",
            ),
            (
                FakeMessage(48, self.human, "replying", channel=self.channel, reply_to=self.colin),
                "replying",
                "human-direct",
            ),
            (
                FakeMessage(49, self.human, "@everyone hello", channel=self.channel, mention_everyone=True),
                "hello",
                "human-everyone",
            ),
        )
        with patch.object(main, "auto_reply_guild_ids", {700}):
            for message, cleaned, source in messages:
                with self.subTest(source=source, content=message.content):
                    with patch.object(main, "handle_chat_message", new=AsyncMock()) as handler:
                        await main.on_message(message)
                    handler.assert_awaited_once_with(
                        message,
                        cleaned,
                        is_dm=False,
                        source=source,
                        reset_companion_exchange=True,
                    )

    async def test_duplicate_auto_reply_message_generates_and_resets_once(self) -> None:
        message = FakeMessage(50, self.human, "A fresh conversation", channel=self.channel)
        main.bot_to_bot_cooldowns.add(self.ben.id)
        with (
            patch.object(main, "auto_reply_guild_ids", {message.guild.id}),
            patch.object(main, "generate_companion_reply", new=AsyncMock(return_value="Hello back")) as generate,
            patch.object(main, "_reset_companion_exchange", wraps=main._reset_companion_exchange) as reset,
        ):
            await main.on_message(message)
            await main.on_message(message)

        generate.assert_awaited_once()
        reset.assert_called_once_with(channel_id=self.channel.id, message_id=message.id)
        self.assertNotIn(self.ben.id, main.bot_to_bot_cooldowns)
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(sum(item["role"] == "user" for item in self.saved_messages()), 1)

    async def test_auto_reply_guild_attachment_only_message_reaches_generation(self) -> None:
        attachment = SimpleNamespace(
            content_type="image/png",
            filename="picture.png",
            url="https://example.test/picture.png",
        )
        message = FakeMessage(51, self.human, "", channel=self.channel, attachments=[attachment])
        with (
            patch.object(main, "auto_reply_guild_ids", {message.guild.id}),
            patch.object(main, "generate_companion_reply", new=AsyncMock(return_value="I can see it.")) as generate,
        ):
            await main.on_message(message)

        generate.assert_awaited_once()
        self.assertEqual(generate.await_args.kwargs["image_urls"], [attachment.url])
        self.assertEqual(self.channel.sent[0][0], "I can see it.")
        inbound = next(item for item in self.saved_messages() if item["role"] == "user")
        self.assertIn("[ATTACHMENTS: 1 image(s)/gif(s)]", inbound["content"])

    async def test_normal_reply_path_passes_actual_owner_speaker_metadata(self) -> None:
        message = FakeMessage(21, self.human, "Hello", channel=self.channel)
        with (
            patch.object(main, "owner_id", self.human.id),
            patch.object(main, "generate_companion_reply", new=AsyncMock(return_value="Hello back")) as generate,
        ):
            await main.handle_chat_message(
                message,
                "Hello",
                is_dm=False,
                source="human-direct",
            )

        self.assertEqual(generate.await_args.kwargs["speaker_name"], "Daina")
        self.assertTrue(generate.await_args.kwargs["speaker_is_owner"])

    async def test_reaction_only_response_adds_reaction_without_message(self) -> None:
        message = FakeMessage(29, self.human, "Good news", channel=self.channel)
        response = main.CompanionResponse(reaction_emojis=("🎉",))
        with patch.object(main, "generate_companion_reply", new=AsyncMock(return_value=response)):
            await main.handle_chat_message(message, "Good news", is_dm=False, source="human-direct")

        self.assertEqual(message.added_reactions, ["🎉"])
        self.assertEqual(message.replies, [])
        self.assertEqual(self.channel.sent, [])
        self.assertFalse(any(item["role"] == "assistant" for item in self.saved_messages()))

    async def test_multiple_reactions_and_written_reply_are_both_sent(self) -> None:
        message = FakeMessage(33, self.human, "Excellent news", channel=self.channel)
        response = main.CompanionResponse(
            reply_text="That deserves the full set.",
            reaction_emojis=("🎉", "💚", "🫎"),
        )
        with patch.object(main, "generate_companion_reply", new=AsyncMock(return_value=response)):
            await main.handle_chat_message(
                message,
                "Excellent news",
                is_dm=False,
                source="human-direct",
            )

        self.assertEqual(message.added_reactions, ["🎉", "💚", "🫎"])
        self.assertEqual(self.channel.sent[0][0], "That deserves the full set.")

    def test_original_response_object_still_exposes_its_reaction(self) -> None:
        original_response = SimpleNamespace(reaction_emoji="💚")

        self.assertEqual(main._response_reactions(original_response), ("💚",))

    async def test_existing_identical_reaction_is_not_added_again(self) -> None:
        message = FakeMessage(30, self.human, "Already seen", channel=self.channel)
        message.reactions = [SimpleNamespace(emoji="💚", me=True)]

        added = await main.add_optional_reaction(message, "💚")

        self.assertFalse(added)
        self.assertEqual(message.added_reactions, [])

    async def test_bot_does_not_react_to_its_own_message(self) -> None:
        message = FakeMessage(31, self.colin, "My message", channel=self.channel)

        added = await main.add_optional_reaction(message, "👍")

        self.assertFalse(added)
        self.assertEqual(message.added_reactions, [])

    async def test_empty_response_can_choose_to_do_nothing(self) -> None:
        message = FakeMessage(32, self.human, "Quiet moment", channel=self.channel)
        with patch.object(
            main,
            "generate_companion_reply",
            new=AsyncMock(return_value=main.CompanionResponse()),
        ):
            await main.handle_chat_message(message, "Quiet moment", is_dm=False, source="human-direct")

        self.assertEqual(message.added_reactions, [])
        self.assertEqual(message.replies, [])
        self.assertEqual(self.channel.sent, [])

    async def test_unaddressed_companion_is_observed_without_reply(self) -> None:
        message = FakeMessage(11, self.ben, "Colin is plain text only", channel=self.channel)
        with patch.object(main, "handle_chat_message", new=AsyncMock()) as handler:
            await main.on_message(message)
        handler.assert_not_awaited()
        self.assertIn("Colin is plain text only", self.saved_messages()[0]["content"])

    async def test_configured_room_label_is_saved_with_observed_message(self) -> None:
        message = FakeMessage(28, self.other_human, "Cottage room context", channel=self.channel)
        config = main.room_context.RoomContextConfig(
            guild_labels={},
            channel_labels={
                self.channel.id: main.room_context.RoomLabel(
                    main.room_context.RoomMode.PRIVATE_HOME,
                    "Cottage Home",
                )
            },
        )

        with (
            patch.object(main, "room_context_config", config),
            patch.object(main, "handle_chat_message", new=AsyncMock()) as handler,
        ):
            await main.on_message(message)

        handler.assert_not_awaited()
        saved = self.saved_messages()[0]["content"]
        self.assertIn("room_mode: private_home", saved)
        self.assertIn("room_label: Cottage Home", saved)
        self.assertIn("label_source: channel", saved)

    async def test_direct_companion_mention_is_accepted_once(self) -> None:
        first = FakeMessage(12, self.ben, "<@1> hello", channel=self.channel, mentions=[self.colin])
        second = FakeMessage(13, self.ben, "<@1> again", channel=self.channel, mentions=[self.colin])
        with patch.object(main, "handle_chat_message", new=AsyncMock()) as handler:
            await main.on_message(first)
            await main.on_message(second)
        handler.assert_awaited_once()
        self.assertEqual(handler.await_args.kwargs["source"], "companion-bot")
        self.assertTrue(handler.await_args.kwargs["reply_to_trigger"])
        self.assertIn("again", self.saved_messages()[0]["content"])

    async def test_companion_reply_to_colin_is_accepted_once(self) -> None:
        message = FakeMessage(14, self.ben, "replying", channel=self.channel, reply_to=self.colin)
        with patch.object(main, "handle_chat_message", new=AsyncMock()) as handler:
            await main.on_message(message)
        handler.assert_awaited_once()
        self.assertTrue(handler.await_args.kwargs["reply_to_trigger"])

    async def test_any_bot_everyone_is_accepted_once(self) -> None:
        first = FakeMessage(
            22,
            self.solace,
            "@everyone What is everyone grateful for today?",
            channel=self.channel,
            mention_everyone=True,
        )
        second = FakeMessage(
            23,
            self.solace,
            "@everyone And one more question?",
            channel=self.channel,
            mention_everyone=True,
        )
        with patch.object(main, "handle_chat_message", new=AsyncMock()) as handler:
            await main.on_message(first)
            await main.on_message(second)

        handler.assert_awaited_once()
        self.assertEqual(
            handler.await_args.args[1],
            "What is everyone grateful for today?",
        )
        self.assertEqual(
            handler.await_args.kwargs["source"],
            "bot-everyone",
        )
        self.assertTrue(handler.await_args.kwargs["reply_to_trigger"])
        self.assertIn("one more question", self.saved_messages()[0]["content"])

    async def test_any_bot_raw_here_text_triggers_without_discord_mention_flag(self) -> None:
        message = FakeMessage(
            25,
            self.solace,
            "@here What made everyone smile today?",
            channel=self.channel,
            mention_everyone=False,
        )
        with patch.object(main, "handle_chat_message", new=AsyncMock()) as handler:
            await main.on_message(message)

        handler.assert_awaited_once()
        self.assertEqual(
            handler.await_args.args[1],
            "What made everyone smile today?",
        )
        self.assertEqual(
            handler.await_args.kwargs["source"],
            "bot-everyone",
        )

    async def test_bot_everyone_opens_new_exchange_after_previous_latch(self) -> None:
        message = FakeMessage(
            26,
            self.solace,
            "@everyone What are we thinking about today?",
            channel=self.channel,
            mention_everyone=True,
        )
        main.bot_to_bot_cooldowns.add(self.solace.id)

        with patch.object(main, "handle_chat_message", new=AsyncMock()) as handler:
            await main.on_message(message)

        handler.assert_awaited_once()
        self.assertEqual(
            handler.await_args.args[1],
            "What are we thinking about today?",
        )
        self.assertEqual(
            handler.await_args.kwargs["source"],
            "bot-everyone",
        )
        self.assertIn(self.solace.id, main.bot_to_bot_cooldowns)

    async def test_bot_everyone_still_obeys_channel_time_cooldown(self) -> None:
        message = FakeMessage(
            27,
            self.solace,
            "@everyone A second question too quickly",
            channel=self.channel,
            mention_everyone=True,
        )
        main.bot_to_bot_cooldowns.add(self.solace.id)
        main.bot_reply_cooldown_by_channel[self.channel.id] = 112.0

        with (
            patch.object(main.time, "monotonic", return_value=100.0),
            patch.object(main, "handle_chat_message", new=AsyncMock()) as handler,
        ):
            await main.on_message(message)

        handler.assert_not_awaited()
        self.assertIn("second question", self.saved_messages()[0]["content"])
        self.assertIn(self.solace.id, main.bot_to_bot_cooldowns)

    async def test_unlisted_bot_everyone_is_accepted(self) -> None:
        newsletter_bot = FakeAuthor(99, "Newsletter Bot", bot=True)
        message = FakeMessage(
            24,
            newsletter_bot,
            "@everyone broad question",
            channel=self.channel,
            mention_everyone=True,
        )
        with patch.object(main, "handle_chat_message", new=AsyncMock()) as handler:
            await main.on_message(message)

        handler.assert_awaited_once()
        self.assertEqual(handler.await_args.args[1], "broad question")
        self.assertEqual(handler.await_args.kwargs["source"], "bot-everyone")
        self.assertTrue(handler.await_args.kwargs["reply_to_trigger"])

    async def test_different_companion_is_stored_when_channel_time_cooldown_is_active(self) -> None:
        rafayel = FakeAuthor(5, "Rafayel", bot=True)
        message = FakeMessage(20, rafayel, "<@1> hello", channel=self.channel, mentions=[self.colin])
        main.bot_reply_cooldown_by_channel[self.channel.id] = 112.0
        with (
            patch.object(main.time, "monotonic", return_value=100.0),
            patch.object(main, "handle_chat_message", new=AsyncMock()) as handler,
        ):
            await main.on_message(message)
        handler.assert_not_awaited()
        self.assertIn("hello", self.saved_messages()[0]["content"])

    async def test_unrelated_human_does_not_reset_latch(self) -> None:
        main.bot_to_bot_cooldowns.add(self.ben.id)
        message = FakeMessage(15, self.other_human, "ordinary room message", channel=self.channel)
        await main.on_message(message)
        self.assertIn(self.ben.id, main.bot_to_bot_cooldowns)

    async def test_addressed_human_resets_latch_then_companion_can_reply(self) -> None:
        main.bot_to_bot_cooldowns.add(self.ben.id)
        human_message = FakeMessage(16, self.human, "<@1> hello", channel=self.channel, mentions=[self.colin])
        companion_message = FakeMessage(17, self.ben, "<@1> hello", channel=self.channel, mentions=[self.colin])

        async def claim_and_reset(message, cleaned, **kwargs):
            claimed = memory.try_claim_discord_message(
                message_id=message.id,
                channel_id=message.channel.id,
                author_id=message.author.id,
                source=kwargs["source"],
            )
            self.assertTrue(claimed)
            if kwargs.get("reset_companion_exchange"):
                main._reset_companion_exchange(
                    channel_id=message.channel.id,
                    message_id=message.id,
                )

        with patch.object(main, "handle_chat_message", new=AsyncMock(side_effect=claim_and_reset)) as handler:
            await main.on_message(human_message)
            await main.on_message(companion_message)
        self.assertEqual(handler.await_count, 2)
        self.assertEqual(handler.await_args.kwargs["source"], "companion-bot")

    async def test_first_bot_chunk_is_reply_and_later_chunks_are_channel_messages(self) -> None:
        message = FakeMessage(18, self.ben, "trigger", channel=self.channel)
        text = "A" * 1798 + "  \n" + "B" * 1820
        await main.send_long_message(self.channel, text, reply_to=message)
        self.assertEqual(len(message.replies), 1)
        self.assertGreaterEqual(len(self.channel.sent), 1)
        self.assertTrue(message.replies[0][1]["mention_author"])
        sent_chunks = [message.replies[0][0], *[item[0] for item in self.channel.sent]]
        self.assertTrue(all(len(chunk) <= 1800 for chunk in sent_chunks))
        self.assertEqual("".join(sent_chunks), text)

    async def test_duplicate_observed_message_id_is_saved_once(self) -> None:
        message = FakeMessage(19, self.other_human, "once", channel=self.channel, attachments=[object()])
        await main.on_message(message)
        await main.on_message(message)
        saved = self.saved_messages()
        self.assertEqual(len(saved), 1)
        self.assertIn("[ATTACHMENTS: 1 attachment(s)]", saved[0]["content"])


if __name__ == "__main__":
    unittest.main()
