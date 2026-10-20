from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, time, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord import app_commands

from src.becoming import RoomConfig
from src.becoming_backup import BackupError
from src import becoming_backup_discord as operations


NOW = datetime(2026, 10, 19, 3, tzinfo=timezone.utc)
PRIVATE = "SYNTHETIC_PRIVATE_ROOM_CONTENT_NEVER_DISPLAY"
OWNER = {"id": "123", "username": "synthetic-owner", "discriminator": "0", "avatar": None}
BOT_USER = {"id": "987", "username": "synthetic-bot", "discriminator": "0", "avatar": None, "bot": True}


def room(*, guild="", enabled=True):
    return RoomConfig.from_env({
        "BECOMING_ENABLED": "true" if enabled else "false", "BOT_OWNER_DISCORD_ID": "123",
        "BECOMING_CHANNEL_ID": "456", "BECOMING_GUILD_ID": guild,
    })


def status(*, category="success", failures=0):
    return {
        "outcome_category": category, "last_attempt_time": NOW.isoformat(),
        "last_successful_time": NOW.isoformat() if category == "success" else None,
        "backup_id": "f61d28c7-3f19-42d6-86e5-663b1fda441c",
        "successful_tiers": ["daily"] if category == "success" else [],
        "failed_tiers": [] if category == "success" else ["daily"],
        "consecutive_failure_count": failures, "ciphertext_size": 1024,
        "schema_version": 1, "code_commit": "a" * 40,
        "retention_verified": category == "success", "download_verified": category == "success",
    }


class Interaction:
    def __init__(self, *, guild=None, user=123, channel=456, channel_type=discord.DMChannel):
        self.user = SimpleNamespace(id=user)
        self.guild_id, self.channel_id = guild, channel
        self.channel = Mock(spec=channel_type)
        self.channel.send = AsyncMock()
        self.done = False

        async def finish(*args, **kwargs):
            self.done = True

        self.response = SimpleNamespace(
            is_done=lambda: self.done, send_message=AsyncMock(side_effect=finish),
            defer=AsyncMock(side_effect=finish),
        )
        self.followup = SimpleNamespace(send=AsyncMock())

    def sent(self):
        return self.response.send_message.call_args_list + self.followup.send.call_args_list


class BackupCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = discord.Client(intents=discord.Intents.none())
        self.client._connection.application_id = 111
        self.tree = app_commands.CommandTree(self.client)
        self.bot = SimpleNamespace(tree=self.tree, application_id=111, http=self.client.http)
        self.manager = SimpleNamespace(
            config=SimpleNamespace(can_run=True), active=False,
            run=AsyncMock(return_value=status()), status=Mock(return_value=status()),
        )
        self.config = room()
        self.callbacks = operations.register_commands(self.bot, config=self.config, manager=self.manager)

    async def asyncTearDown(self):
        await self.client.close()

    def assert_private(self, interaction):
        for call in interaction.sent():
            self.assertTrue(call.kwargs["ephemeral"])
            mentions = call.kwargs["allowed_mentions"]
            self.assertFalse(mentions.everyone or mentions.users or mentions.roles or mentions.replied_user)
            self.assertNotIn(PRIVATE, call.args[0])
        interaction.channel.send.assert_not_awaited()

    async def test_commands_are_separate_private_definitions_and_registration_is_inert(self):
        self.assertEqual([command.name for command in self.callbacks.commands],
                         ["becoming_backup_now", "becoming_backup_status"])
        for command in self.callbacks.commands:
            self.assertTrue(command.allowed_contexts.dm_channel)
            self.assertFalse(command.allowed_contexts.guild or command.allowed_contexts.private_channel)
        self.assertIs(operations.register_commands(self.bot, config=self.config, manager=self.manager), self.callbacks)
        self.manager.run.assert_not_awaited()
        self.manager.status.assert_not_called()

    async def test_disabled_invalid_or_missing_manager_registers_nothing(self):
        for config, manager in (
            (room(enabled=False), self.manager), (replace(self.config, valid=False), self.manager),
            (self.config, None), (self.config, SimpleNamespace(config=SimpleNamespace(can_run=False))),
        ):
            with self.subTest(config=config, manager=bool(manager)):
                tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
                other = SimpleNamespace(tree=tree)
                self.assertIsNone(operations.register_commands(other, config=config, manager=manager))
                self.assertEqual(tree.get_commands(), [])

    async def test_owner_route_and_group_dm_checks_precede_all_work(self):
        for settings in (
            {"user": 999}, {"channel": 999}, {"channel": None}, {"guild": 789},
            {"channel_type": discord.GroupChannel}, {"channel_type": discord.TextChannel},
        ):
            for callback in (self.callbacks.becoming_backup_now, self.callbacks.becoming_backup_status):
                interaction = Interaction(**settings)
                await callback(interaction)
                self.assertIn("unavailable", interaction.sent()[0].args[0])
                interaction.response.defer.assert_not_awaited()
                self.assert_private(interaction)
        self.manager.run.assert_not_awaited()
        self.manager.status.assert_not_called()

    async def test_exact_guild_route_is_ephemeral_and_wrong_guild_is_denied(self):
        client = discord.Client(intents=discord.Intents.none())
        self.addAsyncCleanup(client.close)
        other = SimpleNamespace(tree=app_commands.CommandTree(client))
        callbacks = operations.register_commands(other, config=room(guild="789"), manager=self.manager)
        command = callbacks.commands[0]
        self.assertTrue(command.allowed_contexts.guild)
        self.assertFalse(command.allowed_contexts.dm_channel or command.allowed_contexts.private_channel)
        self.assertEqual(other.tree.get_commands(guild=discord.Object(id=789)), list(callbacks.commands))
        for guild, expected in ((789, True), (777, False)):
            interaction = Interaction(guild=guild, channel_type=discord.TextChannel)
            await callbacks.becoming_backup_status(interaction)
            self.assertEqual("unavailable here" not in interaction.sent()[0].args[0], expected)
            self.assert_private(interaction)

    async def test_manual_defers_before_backup_and_uses_manual_tier(self):
        interaction = Interaction()

        async def run(**kwargs):
            interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
            self.assertTrue(interaction.done)
            self.assertEqual(kwargs, {"tiers": ("manual",)})
            return status()

        self.manager.run.side_effect = run
        await self.callbacks.becoming_backup_now(interaction)
        self.manager.run.assert_awaited_once_with(tiers=("manual",))
        self.assertEqual(len(interaction.sent()), 1)
        self.assertIn("Outcome: success", interaction.sent()[0].args[0])
        self.assert_private(interaction)

    async def test_active_backup_and_racing_busy_error_are_private(self):
        for active in (True, False):
            self.manager.active = active
            self.manager.run.reset_mock()
            self.manager.run.side_effect = BackupError("busy")
            interaction = Interaction()
            await self.callbacks.becoming_backup_now(interaction)
            self.assertIn("Outcome: busy", interaction.sent()[0].args[0])
            if active:
                self.manager.run.assert_not_awaited()
            self.assert_private(interaction)

    async def test_status_whitelist_never_displays_unknown_fields_or_raw_values(self):
        poisoned = {key: PRIVATE for key in status()}
        poisoned.update(entry_content=PRIVATE, credentials=PRIVATE, endpoint=PRIVATE,
                        object_key=PRIVATE, age_identity=PRIVATE,
                        successful_tiers=[PRIVATE, "daily"], failed_tiers={PRIVATE: PRIVATE, "weekly": PRIVATE},
                        tier_results={"daily": {"outcome": PRIVATE, "reason": PRIVATE},
                                      "weekly": {"outcome": "remote_unverified", "reason": PRIVATE,
                                                 "object_key": PRIVATE}})
        self.manager.status.return_value = poisoned
        interaction = Interaction()
        await self.callbacks.becoming_backup_status(interaction)
        text = interaction.sent()[0].args[0]
        self.assertLess(len(text), 1000)
        self.assertIn("Successful tiers: daily", text)
        self.assertIn("Failed tiers: weekly", text)
        self.assert_private(interaction)

    async def test_status_privately_distinguishes_verified_unverified_and_unconfirmed_tiers(self):
        report = status(category="partial_success", failures=1)
        report.update(successful_tiers=["daily"], failed_tiers=["weekly", "monthly"], cleanup_failed=True,
                      tier_results={"daily": {"outcome": "verified"},
                                    "weekly": {"outcome": "remote_unverified", "reason": "hash_mismatch"},
                                    "monthly": {"outcome": "not_uploaded", "reason": "storage_unavailable"}})
        self.manager.status.return_value = report
        interaction = Interaction()
        await self.callbacks.becoming_backup_status(interaction)
        text = interaction.sent()[0].args[0]
        self.assertIn("daily: verified", text)
        self.assertIn("weekly: remote_unverified (hash_mismatch)", text)
        self.assertIn("monthly: not_uploaded (storage_unavailable)", text)
        self.assertIn("Cleanup failed: yes", text)
        self.assertLess(len(text), 1500)
        self.assert_private(interaction)

    async def test_errors_and_failed_defer_never_escape_or_send_public_fallback(self):
        self.manager.run.side_effect = RuntimeError(PRIVATE)
        interaction = Interaction()
        await self.callbacks.becoming_backup_now(interaction)
        self.assertIn("Outcome: failed", interaction.sent()[0].args[0])
        self.assert_private(interaction)
        self.manager.run.reset_mock()
        interaction = Interaction()
        interaction.response.defer.side_effect = RuntimeError(PRIVATE)
        await self.callbacks.becoming_backup_now(interaction)
        self.manager.run.assert_not_awaited()
        self.assertEqual(interaction.sent(), [])

    async def test_sync_uses_two_individual_upserts_and_never_bulk_or_delete(self):
        self.client.http.upsert_global_command = AsyncMock(return_value={})
        self.tree.sync = AsyncMock()
        await operations.sync_commands(self.bot, self.callbacks, guild_id=None)
        self.assertEqual(self.client.http.upsert_global_command.await_count, 2)
        self.assertEqual([call.kwargs["payload"]["name"] for call in
                          self.client.http.upsert_global_command.call_args_list],
                         ["becoming_backup_now", "becoming_backup_status"])
        self.tree.sync.assert_not_awaited()
        self.client.http.upsert_global_command.side_effect = RuntimeError(PRIVATE)
        with self.assertRaises(RuntimeError):
            await operations.sync_commands(self.bot, self.callbacks, guild_id=None)
        self.assertEqual(len(self.tree.get_commands()), 2)


class BackupScheduleBoundaryTests(unittest.TestCase):
    def test_next_three_utc_boundary_across_month_and_year(self):
        scheduled = time(3, tzinfo=timezone.utc)
        cases = (
            ("2026-12-31T02:59:59+00:00", "2026-12-31T03:00:00+00:00"),
            ("2026-12-31T03:00:00+00:00", "2027-01-01T03:00:00+00:00"),
            ("2026-10-31T23:00:00+00:00", "2026-11-01T03:00:00+00:00"),
            ("2028-02-28T04:00:00+00:00", "2028-02-29T03:00:00+00:00"),
            ("2026-12-31T18:00:00-08:00", "2027-01-01T03:00:00+00:00"),
        )
        for source, expected in cases:
            self.assertEqual(operations.next_backup_time(datetime.fromisoformat(source), scheduled),
                             datetime.fromisoformat(expected))
        with self.assertRaises(ValueError):
            operations.next_backup_time(datetime(2026, 1, 1), scheduled)

    def test_deterministic_monday_and_first_day_tiers_use_utc(self):
        cases = {
            "2026-06-01T03:00:00+00:00": ("daily", "weekly", "monthly"),
            "2026-10-19T03:00:00+00:00": ("daily", "weekly"),
            "2026-11-01T03:00:00+00:00": ("daily", "monthly"),
            "2026-10-20T03:00:00+00:00": ("daily",),
            "2026-05-31T20:00:00-08:00": ("daily", "weekly", "monthly"),
        }
        for source, expected in cases.items():
            self.assertEqual(operations.tiers_for_date(datetime.fromisoformat(source)), expected)
        with self.assertRaises(ValueError):
            operations.tiers_for_date(datetime(2026, 1, 1))


class BackupSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = discord.Client(intents=discord.Intents.none())
        self.user = discord.ClientUser(state=self.client._connection, data=BOT_USER)
        self.channel = discord.DMChannel(me=self.user, state=self.client._connection,
                                         data={"id": "456", "type": 1, "recipients": [OWNER]})
        self.bot = SimpleNamespace(user=self.user, get_channel=Mock(return_value=self.channel),
                                   fetch_channel=AsyncMock(return_value=self.channel))
        self.manager = SimpleNamespace(
            config=SimpleNamespace(can_run=True, time_utc=time(3, tzinfo=timezone.utc)),
            active=False, run=AsyncMock(return_value=status()),
        )
        self.send = AsyncMock()
        self.send_patch = patch.object(discord.DMChannel, "send", self.send)
        self.send_patch.start()
        self.addCleanup(self.send_patch.stop)
        self.scheduler = operations.BackupScheduler(self.bot, room_config=room(), manager=self.manager,
                                                    now=lambda: NOW)

    async def asyncTearDown(self):
        await self.scheduler.close()
        await self.client.close()

    async def test_disabled_or_invalid_scheduler_is_inert(self):
        for config, enabled in ((room(enabled=False), True), (replace(room(), valid=False), True), (room(), False)):
            self.manager.config.can_run = enabled
            scheduler = operations.BackupScheduler(self.bot, room_config=config, manager=self.manager)
            self.assertFalse(scheduler.start())
            self.assertIsNone(await scheduler.run_once(now=NOW))
        self.manager.run.assert_not_awaited()
        self.send.assert_not_awaited()

    async def test_exactly_one_task_across_reconnects_and_overlapping_start_calls(self):
        blocked = asyncio.Event()

        async def wait(seconds):
            await blocked.wait()

        self.scheduler._sleep = AsyncMock(side_effect=wait)
        self.assertTrue(self.scheduler.start())
        original = self.scheduler._task
        for _ in range(5):
            self.assertFalse(self.scheduler.start())
            self.assertIs(self.scheduler._task, original)
        await asyncio.sleep(0)
        self.manager.run.assert_not_awaited()

    async def test_failure_then_success_attempts_continue_and_success_is_announced_once(self):
        self.manager.run.side_effect = [status(category="timeout", failures=2), status(), status()]
        for _ in range(3):
            await self.scheduler.run_once(now=NOW)
        self.assertEqual(self.manager.run.await_count, 3)
        self.assertEqual(self.send.await_count, 2)
        failure, success = [call.args[0] for call in self.send.call_args_list]
        self.assertIn("Category: timeout", failure)
        self.assertIn("Consecutive failures: 2", failure)
        self.assertIn("verified off-volume", success)
        for call in self.send.call_args_list:
            self.assertLess(len(call.args[0]), 240)
            self.assertFalse(call.kwargs["allowed_mentions"].everyone)
        self.assertEqual(self.scheduler.notification_state, "delivered")

    async def test_partial_success_is_a_failure_alert_not_universal_success(self):
        partial = status(category="partial_success", failures=1)
        partial.update(successful_tiers=["daily"], failed_tiers=["weekly"],
                       retention_verified=True, download_verified=True)
        self.manager.run.return_value = partial
        await self.scheduler.run_once(now=NOW)
        self.assertFalse(self.scheduler._success_confirmed)
        self.assertIn("attempt failed", self.send.call_args.args[0])

    async def test_incomplete_verification_cannot_claim_first_success(self):
        incomplete = status()
        incomplete["download_verified"] = False
        self.manager.run.return_value = incomplete
        await self.scheduler.run_once(now=NOW)
        self.assertFalse(self.scheduler._success_confirmed)
        self.assertNotIn("verified off-volume", self.send.call_args.args[0])

    async def test_overlapping_scheduler_attempts_do_not_overlap_backup(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def run(**kwargs):
            entered.set()
            await release.wait()
            return status()

        self.manager.run.side_effect = run
        first = asyncio.create_task(self.scheduler.run_once(now=NOW))
        await entered.wait()
        self.assertIsNone(await self.scheduler.run_once(now=NOW))
        release.set()
        await first
        self.manager.run.assert_awaited_once()

    async def test_bounded_errors_never_disclose_exception_or_status_content(self):
        self.manager.run.side_effect = RuntimeError(PRIVATE)
        await self.scheduler.run_once(now=NOW)
        self.assertNotIn(PRIVATE, self.send.call_args.args[0])
        self.assertIn("Category: failed", self.send.call_args.args[0])
        self.manager.run.side_effect = BackupError("busy")
        await self.scheduler.run_once(now=NOW)
        self.assertIn("Category: busy", self.send.call_args.args[0])

    async def test_only_exact_owner_dm_route_can_receive_scheduled_alerts(self):
        original = self.channel
        for config, channel in (
            (room(guild="789"), original),
            (room(), Mock(spec=discord.GroupChannel)),
            (room(), Mock(spec=discord.TextChannel)),
            (room(), discord.DMChannel(me=self.user, state=self.client._connection,
                                      data={"id": "456", "type": 1, "recipients": [{**OWNER, "id": "999"}]})),
            (room(), discord.DMChannel(me=self.user, state=self.client._connection,
                                      data={"id": "999", "type": 1, "recipients": [OWNER]})),
        ):
            self.scheduler.room_config = config
            self.bot.get_channel.return_value = channel
            self.scheduler._success_confirmed = False
            await self.scheduler.run_once(now=NOW)
            self.assertEqual(self.scheduler.notification_state, "unavailable")
        self.send.assert_not_awaited()
        self.bot.fetch_channel.assert_not_awaited()

    async def test_fetches_only_exact_configured_channel_and_delivery_failure_has_no_fallback(self):
        self.bot.get_channel.return_value = None
        self.send.side_effect = RuntimeError(PRIVATE)
        await self.scheduler.run_once(now=NOW)
        self.bot.fetch_channel.assert_awaited_once_with(456)
        self.assertEqual(self.scheduler.notification_state, "delivery_failed")
        self.send.assert_awaited_once()

    async def test_daily_loop_waits_until_boundary_then_survives_a_failed_run(self):
        clock = [datetime(2026, 10, 18, 2, tzinfo=timezone.utc)]
        waits = []

        async def sleep(seconds):
            waits.append(seconds)
            if len(waits) == 3:
                raise asyncio.CancelledError
            clock[0] = operations.next_backup_time(clock[0], time(3, tzinfo=timezone.utc))

        scheduler = operations.BackupScheduler(self.bot, room_config=room(), manager=self.manager,
                                                now=lambda: clock[0], sleep=sleep)
        self.manager.run.side_effect = [RuntimeError(PRIVATE), status()]
        with self.assertRaises(asyncio.CancelledError):
            await scheduler._loop()
        self.assertEqual(waits, [3600, 86400, 86400])
        self.assertEqual(self.manager.run.await_count, 2)
        calls = self.manager.run.call_args_list
        self.assertEqual(calls[0].kwargs["tiers"], ("daily",))
        self.assertEqual(calls[1].kwargs["tiers"], ("daily", "weekly"))


if __name__ == "__main__":
    unittest.main()
