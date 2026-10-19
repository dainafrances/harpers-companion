from __future__ import annotations

from contextlib import redirect_stdout
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


class BecomingStartupTests(unittest.IsolatedAsyncioTestCase):
    async def startup(self, *, guilds, room_guild="", valid=True, fail_room=False):
        client = discord.Client(intents=discord.Intents.none())
        tree = app_commands.CommandTree(client)
        original_commands = main.bot.tree.get_commands()
        for command in original_commands:
            tree.add_command(command)
        seen = []

        async def sync(**kwargs):
            guild = kwargs.get("guild")
            names = {command.name for command in tree.get_commands(guild=guild)}
            seen.append((guild.id if guild else None, names))
            if fail_room and guild is None:
                raise RuntimeError("synthetic private failure must not be logged")
            return []

        tree.sync = AsyncMock(side_effect=sync)
        config = RoomConfig.from_env({
            "BECOMING_ENABLED": "true", "BOT_OWNER_DISCORD_ID": "123" if valid else "",
            "BECOMING_CHANNEL_ID": "456", "BECOMING_GUILD_ID": room_guild,
        })
        fake_bot = SimpleNamespace(tree=tree, user="synthetic bot")
        journal = SimpleNamespace(is_running=lambda: False, start=Mock())
        refresh = SimpleNamespace(is_running=lambda: False, start=Mock())
        with (
            patch.object(main, "bot", fake_bot),
            patch.object(main, "configured_guild_ids", set(guilds)),
            patch.object(main, "becoming_config", config),
            patch.object(main, "startup_synced", False),
            patch.object(main, "becoming_synced", False),
            patch.object(main, "DISCORD_STATUS_ENABLED", True),
            patch.object(main, "DISCORD_STATUS_AUTO_ENABLED", True),
            patch.object(main, "status_presence", SimpleNamespace()),
            patch.object(main, "nightly_journal", journal),
            patch.object(main, "periodic_status_refresh", refresh),
            patch.object(main.memory, "init_db") as legacy_init,
            redirect_stdout(io.StringIO()),
        ):
            await main.on_ready()
            legacy_init.assert_called_once_with()
            journal.start.assert_called_once_with()
            refresh.start.assert_called_once_with()
            if fail_room:
                self.assertFalse(main.becoming_synced)
        self.assertEqual({c.name for c in tree.get_commands()}, {c.name for c in original_commands})
        await client.close()
        return seen

    async def test_dm_sync_does_not_publish_existing_guild_commands_globally(self):
        seen = await self.startup(guilds={777})
        self.assertEqual([guild for guild, _ in seen], [777, None])
        self.assertEqual(seen[-1][1], {command.name for command in main.becoming_callbacks.commands})
        self.assertNotIn("journal_now", seen[-1][1])

    async def test_separate_explicit_room_guild_receives_only_room_commands(self):
        seen = await self.startup(guilds={777}, room_guild="999")
        self.assertEqual([guild for guild, _ in seen], [777, 999])
        self.assertEqual(seen[-1][1], {command.name for command in main.becoming_callbacks.commands})
        self.assertNotIn("voice", seen[-1][1])

    async def test_invalid_configuration_does_not_add_global_sync(self):
        seen = await self.startup(guilds={777}, valid=False)
        self.assertEqual([guild for guild, _ in seen], [777])

    async def test_existing_global_mode_keeps_existing_commands(self):
        seen = await self.startup(guilds=set())
        self.assertEqual(len(seen), 1)
        self.assertIsNone(seen[0][0])
        self.assertIn("voice", seen[0][1])
        self.assertIn("becoming_want", seen[0][1])

    async def test_room_sync_failure_keeps_existing_timers_and_allows_later_retry(self):
        seen = await self.startup(guilds={777}, fail_room=True)
        self.assertEqual([guild for guild, _ in seen], [777, None])

    def test_import_registers_feature_without_initializing_any_database(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "room.sqlite3"
            env = dict(os.environ, DISCORD_TOKEN="test-token", OPENROUTER_API_KEY="test-key",
                       BECOMING_ENABLED="true", BOT_OWNER_DISCORD_ID="123", BECOMING_CHANNEL_ID="456",
                       BECOMING_GUILD_ID="789", BECOMING_DB_PATH=str(path))
            code = (
                "import json;import src.main as m;"
                "print(json.dumps({'service':m.becoming_service is not None,"
                "'exists':m.becoming_config.db_path.exists()}))"
            )
            child = subprocess.run([sys.executable, "-B", "-c", code], env=env, text=True,
                                   capture_output=True, check=True)
            self.assertEqual(json.loads(child.stdout), {"service": True, "exists": False})

    def test_invalid_room_path_disables_feature_without_crashing_import(self):
        env = dict(os.environ, DISCORD_TOKEN="test-token", OPENROUTER_API_KEY="test-key",
                   BECOMING_ENABLED="true", BOT_OWNER_DISCORD_ID="123", BECOMING_CHANNEL_ID="456",
                   BECOMING_GUILD_ID="789", BECOMING_DB_PATH="~room_becoming_missing_user/room.sqlite3")
        code = (
            "import json;import src.main as m;"
            "print(json.dumps({'service':m.becoming_service is not None,'valid':m.becoming_config.valid}))"
        )
        child = subprocess.run([sys.executable, "-B", "-c", code], env=env, text=True,
                               capture_output=True, check=True)
        self.assertEqual(json.loads(child.stdout.splitlines()[-1]), {"service": False, "valid": False})


if __name__ == "__main__":
    unittest.main()
