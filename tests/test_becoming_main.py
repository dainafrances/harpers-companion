from __future__ import annotations

import asyncio
from contextlib import ExitStack, redirect_stdout
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord import app_commands

from src.becoming import RoomConfig

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
main = importlib.import_module("src.main")

LEGACY_NAMES = {"ping", "status", "journal_now", "voice"}
ROOM_NAMES = {"becoming_want", "becoming_show", "becoming_revise", "becoming_history"}


class _Timer:
    def __init__(self, events, name):
        self.running = False
        self.events, self.name = events, name
        self.start = Mock(side_effect=self._start)

    def _start(self):
        self.running = True
        self.events.append(("timer_start", self.name))

    def is_running(self):
        return self.running


class StartupHarness:
    """Real command tree and serialization, with only HTTP/storage/timers fake."""

    def __init__(self, *, guilds=(777,), room_guild="", enabled=True, valid=True, remote_state=None):
        self.guilds, self.room_guild = set(guilds), room_guild
        self.config = RoomConfig.from_env({
            "BECOMING_ENABLED": "true" if enabled else "false",
            "BOT_OWNER_DISCORD_ID": "123" if valid else "",
            "BECOMING_CHANNEL_ID": "456", "BECOMING_GUILD_ID": room_guild,
        })
        self.events, self.bulk_attempts, self.upserts, self.logs = [], [], [], []
        # Remote definitions belong to Discord, not to a bot process. Reuse this
        # same state across separate harnesses to model a real restart.
        self.remote_state = remote_state if remote_state is not None else {
            "global": {"existing_global": {"name": "existing_global"}}, "guilds": {},
        }
        self.remote_global = self.remote_state["global"]
        self.remote_guilds = self.remote_state["guilds"]
        self.legacy_failures = self.room_failures = self.registration_failures = 0
        self.room_fail_name = None
        self.registration_attempts = 0
        self.output = io.StringIO()
        self.legacy_init = Mock()
        self.journal = _Timer(self.events, "journal")
        self.refresh = _Timer(self.events, "status")

    async def __aenter__(self):
        self.client = discord.Client(intents=discord.Intents.none())
        self.client._connection.application_id = 111
        self.client._connection.user = discord.ClientUser(state=self.client._connection, data={
            "id": "111", "username": "SyntheticBot", "discriminator": "0", "avatar": None, "bot": True,
        })
        self.tree = app_commands.CommandTree(self.client)
        # Copy the actual initial tree rather than inventing a legacy-only tree:
        # an import-time Room leak must make the primary-sync tests fail.
        original_commands = main.bot.tree.get_commands()
        assert {command.name for command in original_commands} == LEGACY_NAMES
        for command in original_commands:
            self.tree.add_command(command)
        self.fake_bot = SimpleNamespace(
            tree=self.tree, user="synthetic bot", application_id=111, http=self.client.http,
        )
        self.service = SimpleNamespace(
            create=AsyncMock(), show=AsyncMock(), revise=AsyncMock(), history=AsyncMock(),
        )
        actual_register = main.becoming_commands.register_commands

        def register(*args, **kwargs):
            self.registration_attempts += 1
            self.events.append(("room_registration",))
            if self.registration_failures:
                self.registration_failures -= 1
                raise RuntimeError("synthetic registration failure PRIVATE ERROR")
            return actual_register(*args, **kwargs)

        async def global_bulk(application_id, *, payload):
            return await self._bulk(None, payload)

        async def guild_bulk(application_id, guild_id, *, payload):
            return await self._bulk(guild_id, payload)

        async def global_upsert(application_id, *, payload):
            return await self._upsert(None, payload)

        async def guild_upsert(application_id, guild_id, *, payload):
            return await self._upsert(guild_id, payload)

        self.stack = ExitStack()
        for target, name, value in (
            (main, "bot", self.fake_bot),
            (main, "configured_guild_ids", self.guilds),
            (main, "becoming_config", self.config),
            (main, "becoming_service", self.service),
            (main, "becoming_callbacks", None),
            (main, "startup_synced", False),
            (main, "becoming_synced", False),
            (main, "becoming_backup_manager", None),
            (main, "becoming_backup_scheduler", None),
            (main, "becoming_backup_callbacks", None),
            (main, "becoming_backup_synced", False),
            (main, "DISCORD_STATUS_ENABLED", True),
            (main, "DISCORD_STATUS_AUTO_ENABLED", True),
            (main, "status_presence", SimpleNamespace()),
            (main, "nightly_journal", self.journal),
            (main, "periodic_status_refresh", self.refresh),
            (main, "_debug_log", self.logs.append),
            (main.memory, "init_db", self.legacy_init),
            (main.becoming_commands, "register_commands", register),
            (self.client.http, "bulk_upsert_global_commands", AsyncMock(side_effect=global_bulk)),
            (self.client.http, "bulk_upsert_guild_commands", AsyncMock(side_effect=guild_bulk)),
            (self.client.http, "upsert_global_command", AsyncMock(side_effect=global_upsert)),
            (self.client.http, "upsert_guild_command", AsyncMock(side_effect=guild_upsert)),
        ):
            self.stack.enter_context(patch.object(target, name, value))
        self.stack.enter_context(redirect_stdout(self.output))
        return self

    async def __aexit__(self, *exc):
        self.stack.close()
        await self.client.close()

    async def _bulk(self, guild_id, payload):
        names = {command["name"] for command in payload}
        self.bulk_attempts.append((guild_id, names))
        self.events.append(("legacy_sync", guild_id))
        # Simulate Discord rejecting only Room-bearing bulk payloads. This fails
        # the old implementation even if all other HTTP requests would succeed.
        if names & ROOM_NAMES:
            raise RuntimeError("synthetic primary payload rejects becoming_* definitions")
        if self.legacy_failures:
            self.legacy_failures -= 1
            raise RuntimeError("synthetic ordinary sync failure PRIVATE ERROR")
        remote = {command["name"]: dict(command) for command in payload}
        if guild_id is None:
            self.remote_global.clear()
            self.remote_global.update(remote)
        else:
            self.remote_guilds[guild_id] = remote
        return []

    async def _upsert(self, guild_id, payload):
        assert self.journal.running and self.refresh.running, "Room began before ordinary timers"
        self.upserts.append((guild_id, dict(payload)))
        self.events.append(("room_upsert", guild_id, payload["name"]))
        if self.room_failures and self.room_fail_name in (None, payload["name"]):
            self.room_failures -= 1
            raise RuntimeError("synthetic Room sync failure PRIVATE ERROR")
        remote = self.remote_global if guild_id is None else self.remote_guilds.setdefault(guild_id, {})
        remote[payload["name"]] = dict(payload)
        return dict(payload)

    async def ready(self):
        await main.on_ready()

    def assert_primary_payloads(self, test):
        for _, names in self.bulk_attempts:
            test.assertEqual(names, LEGACY_NAMES)
            test.assertFalse(names & ROOM_NAMES)

    def assert_timers_once(self, test):
        self.journal.start.assert_called_once_with()
        self.refresh.start.assert_called_once_with()
        if self.registration_attempts:
            registration = self.events.index(("room_registration",))
            test.assertLess(self.events.index(("timer_start", "journal")), registration)
            test.assertLess(self.events.index(("timer_start", "status")), registration)


class BecomingStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_and_invalid_room_do_not_register_or_sync(self):
        for settings in ({"enabled": False}, {"valid": False}):
            with self.subTest(settings=settings):
                async with StartupHarness(**settings) as harness:
                    await harness.ready()
                    await harness.ready()
                    self.assertTrue(main.startup_synced)
                    self.assertFalse(main.becoming_synced)
                    self.assertIsNone(main.becoming_callbacks)
                    self.assertEqual(harness.registration_attempts, 0)
                    self.assertEqual(harness.upserts, [])
                    self.assertEqual({command.name for command in harness.tree.get_commands()}, LEGACY_NAMES)
                    self.assertEqual(len(harness.bulk_attempts), 1)
                    harness.assert_primary_payloads(self)
                    harness.assert_timers_once(self)

    async def _enabled_to_disabled_restart(self, *, guilds, room_guild, remains):
        remote_state = {"global": {"existing_global": {"name": "existing_global"}}, "guilds": {}}
        scope = int(room_guild) if room_guild else None

        def remote_commands():
            return remote_state["global"] if scope is None else remote_state["guilds"].get(scope, {})

        async with StartupHarness(guilds=guilds, room_guild=room_guild, remote_state=remote_state) as enabled:
            await enabled.ready()
            self.assertTrue(main.startup_synced and main.becoming_synced)
            self.assertEqual({guild for guild, _ in enabled.upserts}, {scope})
            self.assertTrue(ROOM_NAMES <= set(remote_commands()))
            for name in ROOM_NAMES:
                # The actual serializer exposes guild or bot-DM context, not an
                # exact owner/channel discoverability restriction.
                self.assertEqual(remote_commands()[name]["contexts"], [1] if scope is None else [0])
            previous_room = {name: dict(remote_commands()[name]) for name in ROOM_NAMES}
            enabled.assert_primary_payloads(self)
            enabled.assert_timers_once(self)

        provider = AsyncMock(side_effect=AssertionError("Disabled Room invoked provider"))
        storage = {
            name: Mock(side_effect=AssertionError("Disabled Room accessed storage"))
            for name in ("initialize", "begin_invocation", "write_revision", "finish_without_entry", "get_entry", "get_history")
        }
        with ExitStack() as denied_access:
            denied_access.enter_context(patch.object(main, "generate_becoming_want", provider))
            for name, guard in storage.items():
                denied_access.enter_context(patch.object(main.becoming.RoomStore, name, guard))
            async with StartupHarness(
                guilds=guilds, room_guild=room_guild, enabled=False, remote_state=remote_state,
            ) as disabled:
                # This is a new client/tree/process model with the SAME remote
                # dictionaries, not a fresh empty Discord command inventory.
                self.assertIs(disabled.remote_state, remote_state)
                await disabled.ready()
                await disabled.ready()
                self.assertTrue(main.startup_synced)
                self.assertFalse(main.becoming_synced)
                self.assertIsNone(main.becoming_callbacks)
                self.assertEqual(disabled.registration_attempts, 0)
                self.assertEqual(disabled.upserts, [])
                disabled.client.http.upsert_global_command.assert_not_awaited()
                disabled.client.http.upsert_guild_command.assert_not_awaited()
                current_room = {name: remote_commands()[name] for name in ROOM_NAMES if name in remote_commands()}
                self.assertEqual(current_room, previous_room if remains else {})
                for name in ROOM_NAMES:
                    self.assertIsNone(disabled.tree.get_command(name))
                    if scope is not None:
                        self.assertIsNone(disabled.tree.get_command(name, guild=discord.Object(id=scope)))
                    data = {
                        "id": "100000000000000001", "application_id": "111", "type": 2,
                        "token": "synthetic-disabled-interaction", "version": 1, "attachment_size_limit": 10000000,
                        "channel": {"id": "456", "type": 1 if scope is None else 0,
                                    "name": "synthetic-room", "position": 0, "permission_overwrites": []},
                        "user": {"id": "123", "username": "Synthetic", "discriminator": "0", "avatar": None},
                        "app_permissions": "0", "data": {"id": "999", "name": name, "type": 1, "options": []},
                    }
                    if scope is not None:
                        data["guild_id"] = str(scope)
                        data["data"]["guild_id"] = str(scope)
                        data["member"] = {"user": data["user"], "roles": [],
                                          "joined_at": "2026-10-19T00:00:00Z", "deaf": False, "mute": False, "flags": 0}
                    interaction = discord.Interaction(data=data, state=disabled.client._connection)
                    # Actual discord.py dispatch fails before argument handling
                    # or a Room callback. It does not send a Room-specific reply.
                    with self.assertRaises(app_commands.CommandNotFound):
                        await disabled.tree._call(interaction)
                    self.assertFalse(interaction.response.is_done())
                for method in (disabled.service.create, disabled.service.show, disabled.service.revise, disabled.service.history):
                    method.assert_not_awaited()
                ordinary = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
                await disabled.tree.get_command("ping").callback(ordinary)
                ordinary.response.send_message.assert_awaited_once_with("Awake, steady, and listening.", ephemeral=True)
                self.assertTrue(disabled.journal.running and disabled.refresh.running)
                disabled.assert_primary_payloads(self)
                disabled.assert_timers_once(self)
        provider.assert_not_awaited()
        for guard in storage.values():
            guard.assert_not_called()

    async def test_disabled_restart_keeps_dm_globals_outside_ordinary_guild_sync(self):
        await self._enabled_to_disabled_restart(guilds=(777,), room_guild="", remains=True)

    async def test_disabled_restart_removes_dm_globals_in_ordinary_global_sync(self):
        await self._enabled_to_disabled_restart(guilds=(), room_guild="", remains=False)

    async def test_disabled_restart_removes_room_guild_in_ordinary_guild_sync(self):
        await self._enabled_to_disabled_restart(guilds=(777,), room_guild="777", remains=False)

    async def test_disabled_restart_keeps_separate_room_guild_outside_ordinary_sync(self):
        await self._enabled_to_disabled_restart(guilds=(777,), room_guild="999", remains=True)

    async def test_same_and_separate_guild_routes_never_publish_room_globally(self):
        for room_guild in ("777", "999"):
            with self.subTest(room_guild=room_guild):
                async with StartupHarness(room_guild=room_guild) as harness:
                    await harness.ready()
                    self.assertTrue(main.startup_synced)
                    self.assertTrue(main.becoming_synced)
                    self.assertEqual([guild for guild, _ in harness.bulk_attempts], [777])
                    self.assertEqual({guild for guild, _ in harness.upserts}, {int(room_guild)})
                    self.assertEqual({payload["name"] for _, payload in harness.upserts}, ROOM_NAMES)
                    self.assertEqual({command.name for command in harness.tree.get_commands()}, LEGACY_NAMES)
                    self.assertEqual(set(harness.remote_global), {"existing_global"})
                    self.assertTrue(LEGACY_NAMES <= set(harness.remote_guilds[777]))
                    self.assertTrue(ROOM_NAMES <= set(harness.remote_guilds[int(room_guild)]))
                    harness.assert_primary_payloads(self)
                    harness.assert_timers_once(self)

    async def test_dm_publication_preserves_existing_remote_global_commands(self):
        async with StartupHarness() as harness:
            await harness.ready()
            self.assertTrue(main.becoming_synced)
            self.assertEqual([guild for guild, _ in harness.bulk_attempts], [777])
            self.assertEqual(set(harness.remote_global), ROOM_NAMES | {"existing_global"})
            self.assertEqual({guild for guild, _ in harness.upserts}, {None})
            self.assertEqual(set(harness.remote_guilds[777]), LEGACY_NAMES)
            harness.assert_primary_payloads(self)
            harness.assert_timers_once(self)

    async def test_global_legacy_mode_syncs_ordinary_commands_before_room(self):
        async with StartupHarness(guilds=()) as harness:
            await harness.ready()
            self.assertEqual(harness.bulk_attempts, [(None, LEGACY_NAMES)])
            self.assertEqual(set(harness.remote_global), LEGACY_NAMES | ROOM_NAMES)
            self.assertTrue(main.startup_synced and main.becoming_synced)
            harness.assert_timers_once(self)

    async def test_rejected_initial_legacy_sync_starts_timers_then_retries_clean_payload(self):
        async with StartupHarness() as harness:
            harness.legacy_failures = 1
            await harness.ready()
            self.assertFalse(main.startup_synced)
            self.assertFalse(main.becoming_synced)
            self.assertIsNone(main.becoming_callbacks)
            self.assertEqual(harness.registration_attempts, 0)
            self.assertEqual(harness.upserts, [])
            harness.assert_timers_once(self)
            await harness.ready()
            self.assertTrue(main.startup_synced and main.becoming_synced)
            self.assertEqual(len(harness.bulk_attempts), 2)
            harness.assert_primary_payloads(self)
            harness.assert_timers_once(self)

    async def test_primary_sync_that_rejects_only_room_definitions_succeeds(self):
        async with StartupHarness() as harness:
            await harness.ready()
            # The harness rejects any becoming_* definition in its real bulk
            # serialization; passing means the original startup bug is absent.
            self.assertTrue(main.startup_synced and main.becoming_synced)
            self.assertEqual(harness.bulk_attempts, [(777, LEGACY_NAMES)])
            self.assertEqual(len(harness.upserts), 4)
            harness.assert_timers_once(self)

    async def test_partial_room_sync_failure_reconnect_retries_without_replacing_ordinary_commands(self):
        async with StartupHarness() as harness:
            harness.room_failures = 1
            harness.room_fail_name = "becoming_show"
            await harness.ready()
            self.assertTrue(main.startup_synced)
            self.assertFalse(main.becoming_synced)
            self.assertIsNotNone(main.becoming_callbacks)
            callbacks = main.becoming_callbacks
            self.assertEqual(set(harness.remote_global), {"existing_global", "becoming_want"})
            self.assertEqual(set(harness.remote_guilds[777]), LEGACY_NAMES)
            await harness.ready()
            self.assertTrue(main.becoming_synced)
            self.assertIs(main.becoming_callbacks, callbacks)
            self.assertEqual(harness.registration_attempts, 1)
            self.assertEqual(len(harness.bulk_attempts), 1)
            self.assertEqual(set(harness.remote_global), {"existing_global"} | ROOM_NAMES)
            self.assertEqual({command.name for command in harness.tree.get_commands()}, LEGACY_NAMES | ROOM_NAMES)
            harness.assert_timers_once(self)

    async def test_successful_reconnect_never_reregisters_resyncs_or_restarts_timers(self):
        async with StartupHarness(room_guild="777") as harness:
            await harness.ready()
            callbacks = main.becoming_callbacks
            await harness.ready()
            await harness.ready()
            self.assertIs(main.becoming_callbacks, callbacks)
            self.assertEqual(harness.registration_attempts, 1)
            self.assertEqual(len(harness.bulk_attempts), 1)
            self.assertEqual(len(harness.upserts), 4)
            self.assertEqual(len(harness.tree.get_commands(guild=discord.Object(id=777))), 8)
            harness.assert_timers_once(self)

    async def test_registration_failure_is_guarded_and_retried_after_timers_start(self):
        async with StartupHarness() as harness:
            harness.registration_failures = 1
            await harness.ready()
            self.assertTrue(main.startup_synced)
            self.assertFalse(main.becoming_synced)
            self.assertIsNone(main.becoming_callbacks)
            self.assertEqual(harness.upserts, [])
            harness.assert_timers_once(self)
            await harness.ready()
            self.assertTrue(main.becoming_synced)
            self.assertEqual(harness.registration_attempts, 2)
            self.assertEqual(len(harness.bulk_attempts), 1)
            self.assertEqual(len(harness.upserts), 4)
            harness.assert_timers_once(self)

    async def test_overlapping_ready_events_serialize_primary_sync_before_room_registration(self):
        async with StartupHarness(guilds=(777, 888)) as harness:
            first_primary = asyncio.Event()
            release_primary = asyncio.Event()
            bulk = harness._bulk

            async def paused_bulk(guild_id, payload):
                if not first_primary.is_set():
                    first_primary.set()
                    await release_primary.wait()
                return await bulk(guild_id, payload)

            harness._bulk = paused_bulk
            first = asyncio.create_task(harness.ready())
            await asyncio.wait_for(first_primary.wait(), timeout=1)
            second = asyncio.create_task(harness.ready())
            try:
                await asyncio.sleep(0)
                self.assertFalse(second.done())
                self.assertEqual(harness.legacy_init.call_count, 1)
                self.assertEqual(harness.registration_attempts, 0)
                self.assertEqual(harness.bulk_attempts, [])
            finally:
                release_primary.set()
                await asyncio.gather(first, second)
            self.assertTrue(main.startup_synced and main.becoming_synced)
            self.assertEqual(sorted(guild for guild, _ in harness.bulk_attempts), [777, 888])
            self.assertEqual(harness.registration_attempts, 1)
            self.assertEqual(len(harness.upserts), 4)
            harness.assert_primary_payloads(self)
            harness.assert_timers_once(self)

    async def test_ping_remains_operational_and_errors_are_bounded_after_optional_failure(self):
        async with StartupHarness() as harness:
            harness.room_failures = 1
            await harness.ready()
            interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
            await harness.tree.get_command("ping").callback(interaction)
            interaction.response.send_message.assert_awaited_once_with("Awake, steady, and listening.", ephemeral=True)
            self.assertTrue(main.startup_synced)
            self.assertFalse(main.becoming_synced)
            self.assertTrue(harness.journal.running and harness.refresh.running)
            self.assertNotIn("PRIVATE ERROR", "\n".join(harness.logs) + harness.output.getvalue())

    def test_import_leaves_room_commands_absent_and_opens_no_database(self):
        for enabled, owner, expected_service in (("true", "123", True), ("false", "123", False), ("true", "", False)):
            with self.subTest(enabled=enabled, owner=owner):
                with tempfile.TemporaryDirectory() as temp:
                    path = Path(temp) / "room.sqlite3"
                    env = dict(os.environ, DISCORD_TOKEN="test-token", OPENROUTER_API_KEY="test-key",
                               BECOMING_ENABLED=enabled, BOT_OWNER_DISCORD_ID=owner, BECOMING_CHANNEL_ID="456",
                               BECOMING_GUILD_ID="789", BECOMING_DB_PATH=str(path),
                               BECOMING_BACKUP_REQUIRED_FOR_ROOM="false", BECOMING_BACKUP_ENABLED="false")
                    code = (
                        "import json;import src.main as m;"
                        "print(json.dumps({'service':m.becoming_service is not None,"
                        "'exists':m.becoming_config.db_path.exists(),'callbacks':m.becoming_callbacks is None,"
                        "'rooms':[c.name for c in m.bot.tree.get_commands() if c.name.startswith('becoming_')]}))"
                    )
                    child = subprocess.run([sys.executable, "-B", "-c", code], env=env, text=True,
                                           capture_output=True, check=True)
                    self.assertEqual(json.loads(child.stdout.splitlines()[-1]), {
                        "service": expected_service, "exists": False, "callbacks": True, "rooms": [],
                    })

    def test_required_missing_backup_prevents_store_construction_before_import(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "room.sqlite3"
            env = {key: value for key, value in os.environ.items() if not key.startswith("BECOMING_BACKUP_")}
            env.update(DISCORD_TOKEN="test-token", OPENROUTER_API_KEY="test-key", BECOMING_ENABLED="true",
                       BOT_OWNER_DISCORD_ID="123", BECOMING_CHANNEL_ID="456", BECOMING_GUILD_ID="789",
                       BECOMING_DB_PATH=str(path))
            code = (
                "import json;from unittest.mock import patch;import src.becoming as b;"
                "guard=patch.object(b,'RoomStore',side_effect=AssertionError('store construction denied'));"
                "guard.start();import src.main as m;"
                "print(json.dumps({'service':m.becoming_service is not None,'valid':m.becoming_config.valid,"
                "'exists':m.becoming_config.db_path.exists(),'backup':m.becoming_backup_manager is not None}))"
            )
            child = subprocess.run([sys.executable, "-B", "-c", code], env=env, text=True,
                                   capture_output=True, check=True)
            self.assertEqual(json.loads(child.stdout.splitlines()[-1]), {
                "service": False, "valid": False, "exists": False, "backup": False,
            })

    def test_backup_constructor_failure_preserves_ordinary_import_and_required_room_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, DISCORD_TOKEN="test-token", OPENROUTER_API_KEY="test-key",
                       BECOMING_ENABLED="true", BOT_OWNER_DISCORD_ID="123", BECOMING_CHANNEL_ID="456",
                       BECOMING_GUILD_ID="789", BECOMING_DB_PATH=str(Path(temp) / "room.sqlite3"))
            code = (
                "import json;from unittest.mock import patch;from types import SimpleNamespace;"
                "import src.becoming_backup as b;"
                "c=SimpleNamespace(can_run=True,required_for_room=True,room_allowed=lambda room: True);"
                "patch.object(b.BackupConfig,'from_env',return_value=c).start();"
                "patch.object(b,'BackupManager',side_effect=RuntimeError('PRIVATE ERROR')).start();"
                "import src.main as m;print(json.dumps({'service':m.becoming_service is not None,"
                "'valid':m.becoming_config.valid,'exists':m.becoming_config.db_path.exists(),"
                "'commands':sorted(c.name for c in m.bot.tree.get_commands())}))"
            )
            child = subprocess.run([sys.executable, "-B", "-c", code], env=env, text=True,
                                   capture_output=True, check=True)
            self.assertEqual(json.loads(child.stdout.splitlines()[-1]), {
                "service": False, "valid": False, "exists": False, "commands": sorted(LEGACY_NAMES),
            })
            self.assertNotIn("PRIVATE ERROR", child.stdout + child.stderr)

    async def test_backup_sync_failure_preserves_four_room_commands_and_timers(self):
        async with StartupHarness() as harness:
            manager = SimpleNamespace(config=SimpleNamespace(can_run=True), active=False,
                                      run=AsyncMock(), status=Mock(return_value={}))
            scheduler = SimpleNamespace(start=Mock())
            with patch.object(main, "becoming_backup_manager", manager), patch.object(
                main, "becoming_backup_scheduler", scheduler,
            ):
                harness.room_fail_name = "becoming_backup_now"
                harness.room_failures = 1
                await harness.ready()
                self.assertTrue(main.startup_synced and main.becoming_synced)
                self.assertFalse(main.becoming_backup_synced)
                self.assertIsNotNone(main.becoming_backup_callbacks)
                self.assertTrue(ROOM_NAMES <= set(harness.remote_global))
                self.assertEqual([payload["name"] for _, payload in harness.upserts[:4]],
                                 ["becoming_want", "becoming_show", "becoming_revise", "becoming_history"])
                await harness.ready()
                self.assertTrue(main.becoming_backup_synced)
                self.assertEqual(set(harness.remote_global) & {
                    "becoming_backup_now", "becoming_backup_status",
                }, {"becoming_backup_now", "becoming_backup_status"})
                scheduler.start.assert_called()
                harness.assert_timers_once(self)
                harness.assert_primary_payloads(self)
                manager.run.assert_not_awaited()
                self.assertNotIn("PRIVATE ERROR", " ".join(harness.logs))

    async def test_scheduler_starts_even_when_optional_room_publication_fails(self):
        async with StartupHarness() as harness:
            harness.room_failures = 1
            scheduler = SimpleNamespace(start=Mock(side_effect=lambda: harness.events.append(("backup_start",))))
            manager = SimpleNamespace(config=SimpleNamespace(can_run=True))
            with patch.object(main, "becoming_backup_manager", manager), patch.object(
                main, "becoming_backup_scheduler", scheduler,
            ):
                await harness.ready()
                self.assertFalse(main.becoming_synced)
                self.assertFalse(main.becoming_backup_synced)
                scheduler.start.assert_called_once_with()
                self.assertLess(harness.events.index(("timer_start", "journal")),
                                harness.events.index(("backup_start",)))
                self.assertLess(harness.events.index(("timer_start", "status")),
                                harness.events.index(("backup_start",)))

    async def test_backup_scheduler_failure_cannot_interrupt_existing_startup(self):
        async with StartupHarness() as harness:
            scheduler = SimpleNamespace(start=Mock(side_effect=RuntimeError("PRIVATE BACKUP ERROR")))
            with patch.object(main, "becoming_backup_scheduler", scheduler):
                await harness.ready()
                self.assertTrue(main.startup_synced and main.becoming_synced)
                harness.assert_timers_once(self)
                self.assertNotIn("PRIVATE BACKUP ERROR", " ".join(harness.logs))

    def test_invalid_room_path_disables_feature_without_crashing_import(self):
        env = dict(os.environ, DISCORD_TOKEN="test-token", OPENROUTER_API_KEY="test-key",
                   BECOMING_ENABLED="true", BOT_OWNER_DISCORD_ID="123", BECOMING_CHANNEL_ID="456",
                   BECOMING_GUILD_ID="789", BECOMING_DB_PATH="~room_becoming_missing_user/room.sqlite3",
                   BECOMING_BACKUP_REQUIRED_FOR_ROOM="false", BECOMING_BACKUP_ENABLED="false")
        code = (
            "import json;import src.main as m;"
            "print(json.dumps({'service':m.becoming_service is not None,'valid':m.becoming_config.valid,"
            "'rooms':[c.name for c in m.bot.tree.get_commands() if c.name.startswith('becoming_')]}))"
        )
        child = subprocess.run([sys.executable, "-B", "-c", code], env=env, text=True,
                               capture_output=True, check=True)
        self.assertEqual(json.loads(child.stdout.splitlines()[-1]), {"service": False, "valid": False, "rooms": []})


if __name__ == "__main__":
    unittest.main()
