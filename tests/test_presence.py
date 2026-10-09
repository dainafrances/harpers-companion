from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from src.presence import StatusPresence, normalize_status_text


class StatusTextTests(unittest.TestCase):
    def test_accepts_trimmed_empty_and_boundary_text(self) -> None:
        self.assertEqual(normalize_status_text("  Kettle on. ☕  "), "Kettle on. ☕")
        self.assertEqual(normalize_status_text("   "), "")
        self.assertEqual(normalize_status_text("x" * 128), "x" * 128)

    def test_rejects_invalid_types_length_and_control_characters(self) -> None:
        for value in (None, 5, "x" * 129, "one\ntwo", "one\r", "\tone", "one\x00two", "one\u2028two", "one\u2029two", "\ud800"):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                normalize_status_text(value)


class StatusPresenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "data" / "discord_status.json"
        self.bot = SimpleNamespace(
            activity=None,
            status=discord.Status.idle,
            change_presence=AsyncMock(),
        )
        self.logs: list[str] = []
        self.presence = StatusPresence(self.bot, self.path, cooldown_seconds=20, log=self.logs.append)

    async def test_sends_real_custom_activity_and_preserves_online_indicator(self) -> None:
        self.assertTrue(await self.presence.update("  Kettle on.  "))

        kwargs = self.bot.change_presence.await_args.kwargs
        self.assertEqual(kwargs["activity"].to_dict(), {"type": 4, "state": "Kettle on.", "name": "Custom Status"})
        self.assertIs(kwargs["status"], discord.Status.idle)
        self.assertIs(self.bot.activity, kwargs["activity"])
        self.assertEqual(self.presence.current_text, "Kettle on.")
        self.assertEqual(json.loads(self.path.read_text()), {"text": "Kettle on."})

    async def test_duplicate_and_cooldown_updates_are_skipped(self) -> None:
        with patch("src.presence.time.monotonic", return_value=100):
            self.assertTrue(await self.presence.update("First"))
        with patch("src.presence.time.monotonic", return_value=120):
            self.assertFalse(await self.presence.update("First"))
        with patch("src.presence.time.monotonic", return_value=119.9):
            self.assertFalse(await self.presence.update("Too soon"))
        with patch("src.presence.time.monotonic", return_value=120):
            self.assertTrue(await self.presence.update("Next"))
        self.assertEqual(self.bot.change_presence.await_count, 2)

    async def test_minimum_cooldown_is_twenty_seconds(self) -> None:
        self.assertEqual(StatusPresence(self.bot, self.path, cooldown_seconds=0).cooldown_seconds, 20)

    async def test_concurrent_updates_send_only_one_change_inside_cooldown(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def send(**kwargs) -> None:
            entered.set()
            await release.wait()

        self.bot.change_presence.side_effect = send
        first = asyncio.create_task(self.presence.update("First"))
        await entered.wait()
        second = asyncio.create_task(self.presence.update("Second"))
        release.set()
        self.assertEqual(await asyncio.gather(first, second), [True, False])
        self.bot.change_presence.assert_awaited_once()
        self.assertEqual(self.presence.current_text, "First")

    async def test_clear_status_sends_none_and_persists_empty_string(self) -> None:
        with patch("src.presence.time.monotonic", return_value=100):
            await self.presence.update("Kettle on.")
        with patch("src.presence.time.monotonic", return_value=120):
            self.assertTrue(await self.presence.update("  "))
        self.assertIsNone(self.bot.change_presence.await_args.kwargs["activity"])
        self.assertIsNone(self.bot.activity)
        self.assertEqual(json.loads(self.path.read_text()), {"text": ""})

    async def test_failed_discord_update_preserves_previous_state_and_allows_retry(self) -> None:
        with patch("src.presence.time.monotonic", return_value=100):
            await self.presence.update("Previous")
        previous_activity = self.bot.activity
        self.bot.change_presence.side_effect = RuntimeError("Secret wording should never appear in logs")
        with patch("src.presence.time.monotonic", return_value=120):
            self.assertFalse(await self.presence.update("New private wording"))
        self.assertIs(self.bot.activity, previous_activity)
        self.assertEqual(self.presence.current_text, "Previous")
        self.assertEqual(json.loads(self.path.read_text()), {"text": "Previous"})
        self.assertNotIn("private wording", " ".join(self.logs))
        self.assertNotIn("Secret wording", " ".join(self.logs))
        self.bot.change_presence.side_effect = None
        with patch("src.presence.time.monotonic", return_value=120):
            self.assertTrue(await self.presence.update("Retry"))

    async def test_invalid_input_never_reaches_discord(self) -> None:
        self.assertFalse(await self.presence.update("invalid\nline"))
        self.bot.change_presence.assert_not_awaited()
        self.assertFalse(self.path.exists())

    async def test_persistence_failure_keeps_live_status_without_raising(self) -> None:
        with patch("src.presence.os.replace", side_effect=OSError("private path")):
            self.assertTrue(await self.presence.update("Kettle on."))
        self.assertEqual(self.presence.current_text, "Kettle on.")
        self.assertEqual(self.bot.activity.state, "Kettle on.")
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.iterdir()), [])
        self.assertNotIn("private path", " ".join(self.logs))

    async def test_saved_status_restores_on_new_client_without_sending(self) -> None:
        await self.presence.update("Restored after restart")
        restarted_bot = SimpleNamespace(activity=None, status=discord.Status.online, change_presence=AsyncMock())
        restarted = StatusPresence(restarted_bot, self.path, log=self.logs.append)
        self.assertEqual(restarted.current_text, "Restored after restart")
        self.assertEqual(restarted_bot.activity.to_dict()["state"], "Restored after restart")
        restarted_bot.change_presence.assert_not_awaited()
        self.assertFalse(await restarted.update("Restored after restart"))

    async def test_saved_clear_restores_no_activity(self) -> None:
        self.path.parent.mkdir()
        self.path.write_text('{"text":""}')
        self.bot.activity = discord.Game("Previous activity")
        self.presence.setup()
        self.assertIsNone(self.bot.activity)

    async def test_corrupt_invalid_and_oversize_saved_status_are_ignored(self) -> None:
        self.path.parent.mkdir()
        previous_activity = discord.Game("Previous activity")
        for saved in ("{", "[]", '{"text":5}', '{"text":"bad\\nline"}', json.dumps({"text": "x" * 129}), " " * 4097):
            with self.subTest(saved=saved[:40]):
                self.path.write_text(saved)
                self.bot.activity = previous_activity
                restored = StatusPresence(self.bot, self.path, log=self.logs.append)
                self.assertEqual(restored.current_text, "")
                self.assertIs(self.bot.activity, previous_activity)
                self.bot.change_presence.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
