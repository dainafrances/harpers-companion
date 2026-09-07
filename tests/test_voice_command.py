from __future__ import annotations

import importlib
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")

main = importlib.import_module("src.main")


class FakeHistoryChannel:
    def __init__(self, messages) -> None:
        self.messages = messages

    def history(self, *, limit):
        async def iterator():
            for message in self.messages[:limit]:
                yield message

        return iterator()


def fake_message(author_id: int, content: str):
    return SimpleNamespace(
        author=SimpleNamespace(id=author_id),
        content=content,
    )


def fake_interaction(channel, *, user_id: int = 2):
    return SimpleNamespace(
        id=12345,
        user=SimpleNamespace(id=user_id),
        channel=channel,
        response=SimpleNamespace(
            defer=AsyncMock(),
            send_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


class LatestBotMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_latest_bot_message_reassembles_discord_chunks(self) -> None:
        channel = FakeHistoryChannel(
            [
                fake_message(1, "second chunk"),
                fake_message(1, "first chunk "),
                fake_message(2, "older human message"),
                fake_message(1, "older Colin message"),
            ]
        )
        with patch.object(main, "bot", SimpleNamespace(user=SimpleNamespace(id=1))):
            text = await main._latest_bot_message_text(channel)

        self.assertEqual(text, "first chunk second chunk")


class VoiceCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_uses_latest_message_and_uploads_mp3(self) -> None:
        channel = FakeHistoryChannel([fake_message(1, "**Hello**, Goose.")])
        interaction = fake_interaction(channel)

        with (
            patch.object(main, "bot", SimpleNamespace(user=SimpleNamespace(id=1))),
            patch.object(main, "owner_id", 2),
            patch.object(main, "ELEVENLABS_API_KEY", "secret-key"),
            patch.object(main, "VOICE_MAX_CHARS", 5000),
            patch.object(main.asyncio, "to_thread", new=AsyncMock(return_value=b"ID3-audio")) as to_thread,
        ):
            await main.voice_command.callback(interaction)

        interaction.response.defer.assert_awaited_once_with(thinking=True)
        to_thread.assert_awaited_once()
        self.assertEqual(to_thread.await_args.args[1], "Hello, Goose.")
        self.assertEqual(to_thread.await_args.kwargs["voice_id"], "uTTVBQHpmHNum2rmocA4")
        interaction.followup.send.assert_awaited_once()
        sent_file = interaction.followup.send.await_args.kwargs["file"]
        self.assertEqual(sent_file.filename, "colin-voice-12345.mp3")

    async def test_voice_is_owner_only_when_owner_is_configured(self) -> None:
        interaction = fake_interaction(FakeHistoryChannel([]), user_id=99)
        with (
            patch.object(main, "owner_id", 2),
            patch.object(main, "ELEVENLABS_API_KEY", "secret-key"),
        ):
            await main.voice_command.callback(interaction, "Nope")

        interaction.response.send_message.assert_awaited_once_with(
            "This command is private for Goose right now.",
            ephemeral=True,
        )
        interaction.response.defer.assert_not_awaited()

    async def test_voice_reports_missing_api_key_without_calling_service(self) -> None:
        interaction = fake_interaction(FakeHistoryChannel([]))
        with (
            patch.object(main, "owner_id", 2),
            patch.object(main, "ELEVENLABS_API_KEY", ""),
        ):
            await main.voice_command.callback(interaction, "Hello")

        interaction.response.send_message.assert_awaited_once()
        self.assertIn(
            "ELEVENLABS_API_KEY",
            interaction.response.send_message.await_args.args[0],
        )
        interaction.response.defer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
