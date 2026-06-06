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
    def __init__(self, channel_id: int = 500) -> None:
        self.id = channel_id
        self.sent: list[tuple[str, dict]] = []

    async def send(self, content: str, **kwargs) -> None:
        self.sent.append((content, kwargs))


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
    ) -> None:
        self.id = message_id
        self.author = author
        self.content = content
        self.channel = channel
        self.guild = SimpleNamespace(id=700)
        self.mentions = mentions or []
        self.mention_everyone = False
        self.attachments = attachments or []
        self.embeds = []
        self.created_at = datetime.now(timezone.utc)
        self.reference = (
            SimpleNamespace(resolved=SimpleNamespace(author=reply_to), message_id=900)
            if reply_to is not None
            else None
        )
        self.replies: list[tuple[str, dict]] = []

    async def reply(self, content: str, **kwargs) -> None:
        self.replies.append((content, kwargs))


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
        self.channel = FakeChannel()
        self.fake_bot = FakeBot(self.colin)
        main.bot_to_bot_cooldowns.clear()
        main.bot_reply_cooldown_by_channel.clear()
        self.bot_patch = patch.object(main, "bot", self.fake_bot)
        self.bot_patch.start()

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
        self.assertIn("Room context", self.saved_messages()[0]["content"])

    async def test_unaddressed_companion_is_observed_without_reply(self) -> None:
        message = FakeMessage(11, self.ben, "Colin is plain text only", channel=self.channel)
        with patch.object(main, "handle_chat_message", new=AsyncMock()) as handler:
            await main.on_message(message)
        handler.assert_not_awaited()
        self.assertIn("Colin is plain text only", self.saved_messages()[0]["content"])

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
