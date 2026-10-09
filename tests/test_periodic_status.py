from __future__ import annotations

import contextlib
import asyncio
import importlib
import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.ext import tasks


os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
main = importlib.import_module("src.main")
router = importlib.import_module("src.router")
presence = importlib.import_module("src.presence")


def completion(content: str | None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def selection(text: str):
    return completion(json.dumps({"text": text}))


def audit(decision: str, *reason_codes: str):
    return completion(json.dumps({"decision": decision, "reason_codes": list(reason_codes)}))


class PeriodicStatusSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "ENABLE_DISCORD_STATUS": "true", "DISCORD_STATUS_AUTO_ENABLED": "true",
        })
        environment.start()
        self.addCleanup(environment.stop)

    async def choose(self, create, *, current="Previous public words"):
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with (
            patch.object(router, "_client", client),
            patch.object(router, "build_system_prompt", return_value="UNCHANGED IDENTITY STYLE") as identity,
            patch.object(router, "build_memory_note") as memory_note,
            patch.object(router, "generate_companion_reply") as chat,
        ):
            status = await router.generate_periodic_discord_status(current_status=current)
        memory_note.assert_not_called()
        chat.assert_not_called()
        return status, identity

    async def test_model_chooses_own_phrase_and_separate_public_audit_checks_it(self):
        phrase = "Cottage kettle on; leaving room for a little quiet."
        create = AsyncMock(side_effect=[selection(phrase), audit("ALLOW")])
        selected, identity = await self.choose(create)
        self.assertEqual(selected, phrase)
        identity.assert_called_once_with(
            is_dm=False, speaker_name="Discord public profile", speaker_is_owner=False,
        )
        writer, auditor = create.await_args_list
        self.assertEqual(writer.kwargs["tools"], [])
        self.assertEqual(writer.kwargs["response_format"], router.PERIODIC_STATUS_RESPONSE_FORMAT)
        self.assertEqual(writer.kwargs["messages"][0]["content"], "UNCHANGED IDENTITY STYLE")
        timer = json.loads(writer.kwargs["messages"][-1]["content"])
        self.assertEqual(timer, {
            "task": "automated_public_status_selection", "current_public_status": "Previous public words",
        })
        payload = json.loads(auditor.kwargs["messages"][-1]["content"])
        self.assertEqual(payload["audience"]["scope"], "global_public_profile")
        source = payload["source_evidence"]
        self.assertTrue(source["automated_timer"])
        self.assertEqual(source["identity_style_source"], "UNCHANGED IDENTITY STYLE")
        self.assertEqual(source["current_room_history"], [])
        self.assertEqual(source["current_user_message"], "")
        self.assertEqual(source["direct_owner_message_text"], "")
        self.assertFalse(source["current_speaker_is_owner"])

    async def test_denied_status_is_skipped(self):
        create = AsyncMock(side_effect=[selection("Private detail"), audit("REJECT", "EXPLICIT_CONFIDENCE")])
        self.assertIsNone((await self.choose(create))[0])

    async def test_audit_errors_skip_status(self):
        create = AsyncMock(side_effect=[selection("Fresh public words"), RuntimeError("down"), RuntimeError("down")])
        self.assertIsNone((await self.choose(create))[0])

    async def test_generation_errors_skip_status(self):
        self.assertIsNone((await self.choose(AsyncMock(side_effect=RuntimeError("down"))))[0])

    async def test_invalid_model_selection_is_skipped_without_audit(self):
        for payload in ([], {"text": 5}, {"text": "x" * 129}, {"text": "two\nlines"}, {"text": "OK", "extra": True}):
            with self.subTest(payload=payload):
                create = AsyncMock(return_value=completion(json.dumps(payload)))
                self.assertIsNone((await self.choose(create))[0])
                self.assertEqual(create.await_count, 1)

    async def test_empty_selection_clears_status_without_content_audit(self):
        create = AsyncMock(return_value=selection(""))
        self.assertEqual((await self.choose(create))[0], "")
        self.assertEqual(create.await_count, 1)

    async def test_disabled_auto_or_all_status_makes_no_model_calls(self):
        for setting in ("ENABLE_DISCORD_STATUS", "DISCORD_STATUS_AUTO_ENABLED"):
            with self.subTest(setting=setting), patch.dict(os.environ, {setting: "false"}):
                create = AsyncMock()
                selected, identity = await self.choose(create)
                self.assertIsNone(selected)
                create.assert_not_awaited()
                identity.assert_not_called()


class FakeLoop:
    def __init__(self):
        self.running = False
        self.start = Mock(side_effect=self._start)
        self.cancel = Mock()

    def is_running(self):
        return self.running

    def _start(self):
        self.running = True


class PeriodicStatusSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SimpleNamespace(
            user=SimpleNamespace(id=1, name="Colin"), is_closed=Mock(return_value=False),
            is_ready=Mock(return_value=True), wait_until_ready=AsyncMock(),
        )
        self.status = SimpleNamespace(current_text="", update=AsyncMock(side_effect=self.update))
        self.selector = AsyncMock(return_value="Cottage window open to the evening.")
        self.refresh = main.periodic_status_refresh.coro
        for target, attribute, value in (
            (main, "bot", self.client), (main, "status_presence", self.status),
            (main, "DISCORD_STATUS_ENABLED", True), (main, "DISCORD_STATUS_AUTO_ENABLED", True),
            (main, "status_refresh_started", False), (main, "generate_periodic_discord_status", self.selector),
        ):
            replacement = patch.object(target, attribute, value)
            replacement.start()
            self.addCleanup(replacement.stop)

    async def update(self, text):
        self.status.current_text = text
        return True

    def test_interval_defaults_and_invalid_values_cannot_create_runaway_timer(self):
        for invalid in (None, "bad", "nan", "inf", "-inf", "0", "-1", "0.249", "8761", "1e300", "1e-300"):
            with self.subTest(invalid=invalid):
                self.assertEqual(presence.status_interval_hours(invalid), 3.0)
        self.assertEqual(presence.status_interval_hours("1.5"), 1.5)
        self.assertEqual(presence.status_interval_hours("0.25"), 0.25)
        self.assertEqual(presence.status_interval_hours("8760"), 8760.0)
        self.assertEqual(main.periodic_status_refresh.hours, main.DISCORD_STATUS_INTERVAL_HOURS)

    async def test_before_loop_waits_for_readiness(self):
        await main.before_periodic_status_refresh()
        self.client.wait_until_ready.assert_awaited_once()

    async def test_native_loop_waits_then_runs_intervals_and_stops_cleanly(self):
        ready = asyncio.Event()
        refreshed_twice = asyncio.Event()
        self.client.wait_until_ready.side_effect = ready.wait

        async def choose(*, current_status):
            if self.selector.await_count == 2:
                refreshed_twice.set()
            return f"Public phrase {self.selector.await_count}"

        self.selector.side_effect = choose
        loop = tasks.loop(seconds=0.005)(self.refresh)
        loop.before_loop(main.before_periodic_status_refresh)
        task = loop.start()
        try:
            await asyncio.sleep(0)
            self.selector.assert_not_awaited()
            ready.set()
            await asyncio.wait_for(refreshed_twice.wait(), timeout=1.0)
        finally:
            loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.assertFalse(loop.is_running())
        self.assertEqual(self.selector.await_count, 2)
        self.client.wait_until_ready.assert_awaited_once()

    async def test_bot_shutdown_cancels_periodic_task_and_closes_discord(self):
        client = main.CompanionBot(command_prefix="!", intents=discord.Intents.none())
        loop = FakeLoop()
        with (
            patch.object(main, "periodic_status_refresh", loop),
            patch("discord.ext.commands.Bot.close", new=AsyncMock()) as close,
        ):
            await client.close()
        loop.cancel.assert_called_once()
        close.assert_awaited_once()

    async def test_ready_reconnect_starts_one_background_task_and_no_gateway_update(self):
        loop = FakeLoop()
        with (
            patch.object(main, "periodic_status_refresh", loop),
            patch.object(main, "startup_synced", True),
            patch.object(main.memory, "init_db"),
            patch.object(main.nightly_journal, "is_running", return_value=True),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            await main.on_ready()
            await main.on_ready()
        loop.start.assert_called_once()
        self.selector.assert_not_awaited()
        self.status.update.assert_not_awaited()

    async def test_disabled_auto_does_not_start_on_ready(self):
        loop = FakeLoop()
        with (
            patch.object(main, "DISCORD_STATUS_AUTO_ENABLED", False),
            patch.object(main, "periodic_status_refresh", loop),
            patch.object(main, "startup_synced", True),
            patch.object(main.memory, "init_db"),
            patch.object(main.nightly_journal, "is_running", return_value=True),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            await main.on_ready()
        loop.start.assert_not_called()

    async def test_empty_status_fills_initially_then_refreshes_on_next_tick(self):
        await self.refresh()
        self.selector.assert_awaited_once_with(current_status="")
        first = self.status.current_text
        await self.refresh()
        self.assertEqual(self.selector.await_count, 2)
        self.selector.assert_awaited_with(current_status=first)

    async def test_restored_status_waits_until_next_interval(self):
        self.status.current_text = "Restored public words"
        await self.refresh()
        self.selector.assert_not_awaited()
        self.status.update.assert_not_awaited()
        await self.refresh()
        self.selector.assert_awaited_once_with(current_status="Restored public words")

    async def test_disconnected_tick_skips_without_resetting_first_cycle(self):
        self.client.is_ready.return_value = False
        await self.refresh()
        self.selector.assert_not_awaited()
        self.assertFalse(main.status_refresh_started)
        self.client.is_ready.return_value = True
        await self.refresh()
        self.selector.assert_awaited_once()

    async def test_disabled_or_closed_task_stops_without_model_calls(self):
        for setting in ("disabled", "closed", "no_presence"):
            with self.subTest(setting=setting), contextlib.ExitStack() as stack:
                loop = FakeLoop()
                stack.enter_context(patch.object(main, "periodic_status_refresh", loop))
                if setting == "disabled":
                    stack.enter_context(patch.object(main, "DISCORD_STATUS_AUTO_ENABLED", False))
                elif setting == "closed":
                    self.client.is_closed.return_value = True
                else:
                    stack.enter_context(patch.object(main, "status_presence", None))
                await self.refresh()
                loop.cancel.assert_called_once()
                self.client.is_closed.return_value = False
        self.selector.assert_not_awaited()

    async def test_selection_failure_retains_previous_status(self):
        self.status.current_text = "Previous public words"
        with patch.object(main, "status_refresh_started", True):
            self.selector.side_effect = RuntimeError("selector unavailable")
            await self.refresh()
        self.assertEqual(self.status.current_text, "Previous public words")
        self.status.update.assert_not_awaited()

    async def test_denied_selection_retains_previous_status(self):
        self.status.current_text = "Previous public words"
        self.selector.return_value = None
        with patch.object(main, "status_refresh_started", True):
            await self.refresh()
        self.assertEqual(self.status.current_text, "Previous public words")
        self.status.update.assert_not_awaited()

    async def test_gateway_failure_retains_previous_status(self):
        self.status.current_text = "Previous public words"
        self.status.update.side_effect = RuntimeError("gateway unavailable")
        with patch.object(main, "status_refresh_started", True):
            await self.refresh()
        self.assertEqual(self.status.current_text, "Previous public words")

    async def test_timer_never_posts_chat_or_reads_or_writes_memory(self):
        with (
            patch.object(main, "send_long_message", new=AsyncMock()) as send,
            patch.object(main, "handle_chat_message", new=AsyncMock()) as chat,
            patch.object(main.memory, "save_message") as save_message,
            patch.object(main.memory, "save_journal_entry") as save_journal,
            patch.object(main.memory, "get_recent_messages") as recent,
            patch.object(main, "_latest_journal_for_prompt") as journal,
            patch.object(main, "_build_continuity_prompt_inputs") as continuity,
        ):
            await self.refresh()
        send.assert_not_awaited()
        chat.assert_not_awaited()
        for operation in (save_message, save_journal, recent, journal, continuity):
            operation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
