"""Synthetic Room acceptance through real Discord and provider SDK objects.

No login or socket is used: Discord's HTTP/webhook boundaries and the provider
HTTP transport are intercepted. All databases live in a disposable directory.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import importlib
import io
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord import app_commands
from discord.ext import commands as discord_commands
from discord.webhook.async_ import async_context

try:
    import httpx2 as httpx
except ImportError:
    import httpx

os.environ.setdefault("DISCORD_TOKEN", "synthetic-import-token")
os.environ.setdefault("OPENROUTER_API_KEY", "synthetic-import-key")
from src import becoming, becoming_commands, memory, router

main = importlib.import_module("src.main")

OWNER = 10
CHANNEL = 20
GUILD = 30
APPLICATION = 333
TITLE = "Paper lantern workshop"
ORIGINAL = "I want to build a tiny paper lantern for the imaginary workshop."
REVISED = "I want to build a tiny paper lantern with a blue handle for the imaginary workshop."
IDENTITY = "SYNTHETIC_PRIVATE_IDENTITY_FIXTURE"
PRIVATE_ERROR = "SYNTHETIC_PRIVATE_ERROR_BODY_DO_NOT_LOG"
REQUESTED_MODEL = "synthetic/room-test-model"
USER = {"id": str(OWNER), "username": "synthetic-owner", "discriminator": "0", "avatar": None}
BOT_USER = {"id": "444", "username": "synthetic-bot", "discriminator": "0", "avatar": None, "bot": True}


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


class _Timer:
    """Expose task-loop lifecycle without executing unrelated background jobs."""

    def __init__(self):
        self.running = False
        self.start = Mock(side_effect=self._start)

    def _start(self):
        self.running = True

    def is_running(self):
        return self.running


class _WebhookAdapter:
    """Intercept the real SDK's serialized private response payloads."""

    def __init__(self, case):
        self.case = case
        self.deliveries = []
        self.current_operation = None
        self.before_success = None

    def _capture(self, kind, payload):
        content = payload.get("content", "")
        if self.before_success and ("Want created." in content or "Want revised." in content):
            self.before_success(self.current_operation)
        self.deliveries.append((self.current_operation, kind, payload))

    async def create_interaction_response(self, interaction_id, token, **kwargs):
        payload = kwargs["params"].payload
        self._capture("initial", payload)
        return {"interaction": {"id": str(interaction_id), "response_message_loading": True,
                                "response_message_ephemeral": True}}

    async def execute_webhook(self, webhook_id, token, **kwargs):
        payload = kwargs["payload"]
        self._capture("followup", payload)
        return {
            "id": str(500 + len(self.deliveries)), "channel_id": str(CHANNEL), "author": BOT_USER,
            "content": payload.get("content", ""), "tts": False, "mention_everyone": False,
            "mentions": [], "mention_roles": [], "attachments": [], "embeds": [],
            "pinned": False, "type": 0, "flags": payload.get("flags", 0),
            "timestamp": "2026-10-19T00:00:00Z", "components": payload.get("components", []),
        }


class BecomingSyntheticEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="room-synthetic-e2e-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "private-room.sqlite3"
        self.legacy_path = Path(self.temp.name) / "ordinary-memory.sqlite3"
        self.store = becoming.RoomStore(self.path, protected_paths=(self.legacy_path,))
        self.scope = becoming.RoomScope(str(OWNER), str(CHANNEL), str(GUILD))
        self.requests = []
        self.provider_outputs = []
        self.bots = []
        self.adapter = _WebhookAdapter(self)
        self.context_token = async_context.set(self.adapter)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(memory, "DB_PATH", self.legacy_path))
        self.patches.enter_context(patch.object(router, "build_system_prompt", return_value=IDENTITY))
        self.patches.enter_context(patch.dict(os.environ, {"MODEL_PRIMARY": REQUESTED_MODEL}))
        self.client = router.AsyncOpenAI(
            api_key="synthetic-key-no-production-access", base_url="https://synthetic.invalid/api/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self._provider)),
        )
        self.patches.enter_context(patch.object(router, "_client", self.client))
        self.service = becoming.RoomService(self.store, router.generate_becoming_want, REQUESTED_MODEL)
        self.boundary_spies = []
        for module, name, asynchronous in (
            (memory, "save_message", False), (memory, "save_journal_entry", False),
            (memory, "save_recall_message", False), (memory, "save_continuity_event", False),
            (router, "build_memory_note", False), (router, "_prepare_history", False),
            (router, "generate_companion_reply", True), (router, "generate_periodic_discord_status", True),
            (router.paintbox, "run_create_calls", True), (main, "handle_chat_message", True),
            (main, "generate_companion_reply", True), (main, "generate_periodic_discord_status", True),
        ):
            spy = AsyncMock() if asynchronous else Mock()
            self.patches.enter_context(patch.object(module, name, spy))
            self.boundary_spies.append(spy)
        self.config = self._config()
        self.next_id = 1000
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.patches.enter_context(redirect_stdout(self.stdout))
        self.patches.enter_context(redirect_stderr(self.stderr))
        self.logs = _LogCapture()
        self.patches.enter_context(patch.object(logging.getLogger(), "level", logging.DEBUG))
        logging.getLogger().addHandler(self.logs)
        self.addCleanup(logging.getLogger().removeHandler, self.logs)

    async def asyncTearDown(self):
        for bot in self.bots:
            items = [item for group in bot._connection._view_store._views.values() for item in group.values()]
            for item in items:
                item.view.stop()
            await bot.close()
        await self.client.close()
        async_context.reset(self.context_token)

    def _config(self, *, enabled=True, guild=GUILD):
        return becoming.RoomConfig.from_env({
            "BECOMING_ENABLED": "true" if enabled else "false", "BOT_OWNER_DISCORD_ID": str(OWNER),
            "BECOMING_CHANNEL_ID": str(CHANNEL), "BECOMING_GUILD_ID": str(guild) if guild else "",
            "BECOMING_DB_PATH": str(self.path),
        })

    def _provider(self, request):
        self.assertEqual(request.url.host, "synthetic.invalid")
        self.assertEqual(request.headers["authorization"], "Bearer synthetic-key-no-production-access")
        payload = json.loads(request.content)
        self.requests.append(payload)
        self.assertEqual(payload["tools"], [])
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertEqual(payload["messages"][0]["content"], IDENTITY)
        self.assertEqual(len(payload["messages"]), 3)
        explicit = json.loads(payload["messages"][-1]["content"])
        if explicit["task"] == "create_one_want":
            self.assertEqual(set(explicit), {"task", "prompt_version", "explicit_command_seed"})
        else:
            self.assertEqual(set(explicit), {"task", "prompt_version", "current_stored_revision",
                                             "explicit_revision_instruction"})
            self.assertEqual(explicit["current_stored_revision"]["content"], ORIGINAL)
            self.assertNotIn("owner_id", explicit["current_stored_revision"])
            self.assertNotIn("guild_id", explicit["current_stored_revision"])
        operation = self.adapter.current_operation
        initial = [p for op, kind, p in self.adapter.deliveries if op == operation and kind == "initial"]
        self.assertEqual(initial, [{"type": 5, "data": {"flags": 64}}])
        self.assertTrue(self.provider_outputs, "An unexpected provider request was attempted")
        status, output, metadata = self.provider_outputs.pop(0)
        if status != 200:
            return httpx.Response(status, json=output, headers={"x-request-id": "synthetic-http-request"})
        return httpx.Response(200, json={
            "id": "synthetic-completion-" + str(len(self.requests)), "object": "chat.completion",
            "created": 1792368000, "model": REQUESTED_MODEL, "provider": "Synthetic Provider",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(output) if isinstance(output, dict) else output,
            }}], **metadata,
        })

    def _queue(self, action="create", *, content=ORIGINAL, reason="", title=TITLE):
        self.provider_outputs.append((200, {"action": action, "title": title, "content": content, "reason": reason}, {}))

    async def _bot(self, *, config, fail_room=False):
        bot = discord_commands.Bot(command_prefix="!", intents=discord.Intents.none(), application_id=APPLICATION)
        await bot._async_setup_hook()
        bot._connection.user = discord.ClientUser(state=bot._connection, data=BOT_USER)
        self.bots.append(bot)
        for command in main.bot.tree.get_commands():
            bot.tree.add_command(command)
        async def ordinary_health(interaction: discord.Interaction):
            await interaction.response.send_message("Synthetic ordinary companion command is operational.",
                                                    ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        bot.tree.add_command(app_commands.command(name="ordinary_health", description="Synthetic startup check")(ordinary_health))
        primary = []
        optional = []
        journal, status = _Timer(), _Timer()

        def command_result(payload, *, guild=None):
            return {**payload, "id": str(800 + len(primary) + len(optional)), "application_id": str(APPLICATION),
                    **({"guild_id": str(guild)} if guild is not None else {})}

        async def bulk_global(application_id, *, payload):
            self.assertFalse(any(p["name"].startswith("becoming_") for p in payload))
            primary.append((None, payload))
            return [command_result(p) for p in payload]

        async def bulk_guild(application_id, guild_id, *, payload):
            self.assertFalse(any(p["name"].startswith("becoming_") for p in payload))
            primary.append((guild_id, payload))
            return [command_result(p, guild=guild_id) for p in payload]

        async def upsert_global(application_id, *, payload):
            return await upsert(payload, None)

        async def upsert_guild(application_id, guild_id, *, payload):
            return await upsert(payload, guild_id)

        async def upsert(payload, guild):
            self.assertTrue(journal.running and status.running, "Optional sync ran before ordinary timers")
            self.assertTrue(payload["name"].startswith("becoming_"))
            optional.append((guild, payload))
            if fail_room:
                raise RuntimeError("SYNTHETIC_PRIVATE_SYNC_PAYLOAD")
            return command_result(payload, guild=guild)

        bot.http.request = AsyncMock(side_effect=AssertionError("Unintercepted Discord HTTP request"))
        bot.http.bulk_upsert_global_commands = AsyncMock(side_effect=bulk_global)
        bot.http.bulk_upsert_guild_commands = AsyncMock(side_effect=bulk_guild)
        bot.http.upsert_global_command = AsyncMock(side_effect=upsert_global)
        bot.http.upsert_guild_command = AsyncMock(side_effect=upsert_guild)
        with ExitStack() as startup:
            for name, value in {
                "bot": bot, "configured_guild_ids": {GUILD}, "becoming_config": config,
                "becoming_service": self.service, "becoming_callbacks": None, "startup_synced": False,
                "becoming_synced": False, "nightly_journal": journal, "periodic_status_refresh": status,
                "DISCORD_STATUS_ENABLED": True, "DISCORD_STATUS_AUTO_ENABLED": True,
                "status_presence": SimpleNamespace(),
            }.items():
                startup.enter_context(patch.object(main, name, value))
            await main.on_ready()
            first_success = main.becoming_synced
            await main.on_ready()  # A reconnect must not duplicate registrations or timer starts.
            callbacks = main.becoming_callbacks
            synced = main.becoming_synced
        journal.start.assert_called_once_with()
        status.start.assert_called_once_with()
        self.assertEqual(len(primary), 1)
        self.assertEqual(first_success, enabled_and_valid := bool(config.enabled and config.valid and not fail_room))
        self.assertEqual(synced, enabled_and_valid)
        if config.enabled and config.valid:
            self.assertIsNotNone(callbacks)
        else:
            self.assertIsNone(callbacks)
            self.assertEqual(optional, [])
        return bot, callbacks, primary, optional

    def _interaction(self, bot, name, *, operation=None, owner=OWNER, channel=CHANNEL, guild=GUILD,
                     command_guild=GUILD, group_dm=False, component=False, **options):
        operation = operation or str(self.next_id)
        self.next_id += 1
        data = {
            "id": operation, "application_id": str(APPLICATION), "type": 3 if component else 2,
            "token": "synthetic-interaction-token", "version": 1, "attachment_size_limit": 10000000,
            "channel": {"id": str(channel), "type": 0 if guild is not None else 3 if group_dm else 1,
                        "name": "synthetic-private-room", "position": 0, "permission_overwrites": []},
            "user": {**USER, "id": str(owner)}, "app_permissions": "0",
            "data": {"id": "999", "name": name, "type": 1,
                     "options": [{"name": key, "type": 4 if isinstance(value, int) else 3, "value": value}
                                 for key, value in options.items()]},
        }
        if command_guild is not None:
            data["data"]["guild_id"] = str(command_guild)
        if guild is not None:
            data["guild_id"] = str(guild)
            data["member"] = {"user": {**USER, "id": str(owner)}, "roles": [],
                              "joined_at": "2026-10-19T00:00:00Z", "deaf": False, "mute": False, "flags": 0}
        return discord.Interaction(data=data, state=bot._connection)

    async def _invoke(self, bot, name, **kwargs):
        interaction = self._interaction(bot, name, **kwargs)
        self.adapter.current_operation = str(interaction.id)
        before = len(self.adapter.deliveries)
        await bot.tree._call(interaction)
        self.assertFalse(interaction.command_failed)
        deliveries = self.adapter.deliveries[before:]
        self.assertTrue(deliveries)
        self._assert_private(deliveries)
        return str(interaction.id), "\n".join(
            payload.get("content", payload.get("data", {}).get("content", ""))
            for _, _, payload in deliveries
        )

    def _assert_private(self, deliveries):
        for _, kind, payload in deliveries:
            message = payload if kind == "followup" else payload.get("data", {})
            if kind == "followup" or payload.get("type") in {4, 5}:
                self.assertEqual(message.get("flags", 0) & 64, 64)
            if "content" in message:
                self.assertEqual(message["allowed_mentions"]["parse"], [])
                self.assertFalse(message["allowed_mentions"].get("replied_user", False))
                self.assertLessEqual(len(message["content"].encode("utf-16-le")) // 2, 2000)

    def _rows(self, table):
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute("SELECT * FROM " + table)]

    def _assert_durable_before_success(self, operation):
        rows = [row for row in self._rows("invocations") if row["operation_key"] == operation]
        self.assertEqual(len(rows), 1)
        receipt = rows[0]
        self.assertIn(receipt["outcome"], {"created", "revised"})
        self.assertIsNotNone(receipt["completed_at"])
        self.assertIsNotNone(self.store.get_entry(receipt["entry_id"], self.scope))
        self.assertTrue(any(row["invocation_id"] == receipt["invocation_id"] for row in self._rows("revisions")))

    async def test_complete_synthetic_private_lifecycle(self):
        # 1. Disabled Room: actual legacy startup succeeds, without Room definitions.
        disabled_bot, callbacks, _, optional = await self._bot(config=self._config(enabled=False))
        self.assertIsNone(callbacks)
        self.assertEqual(optional, [])
        self.assertFalse(any(c.name.startswith("becoming_") for c in disabled_bot.tree.get_commands()))
        self.assertFalse(self.path.exists())

        # 2. A fresh bot enables only its exact guild commands after ordinary startup.
        bot, callbacks, _, optional = await self._bot(config=self.config)
        self.assertEqual(len(optional), 4)
        self.assertTrue(all(guild == GUILD for guild, _ in optional))
        self.assertFalse(any(c.name.startswith("becoming_") for c in bot.tree.get_commands()))
        self.assertTrue(all(isinstance(c, app_commands.Command) for c in callbacks.commands))
        legacy_snapshot = self.legacy_path.read_bytes()
        protected_sources = {path: path.read_bytes() for path in (
            Path(main.__file__).parent / "identity.py", Path(main.__file__).parent / "presence.py",
            Path(main.__file__).parent / "paintbox.py", Path(main.__file__).parent / "continuity.py",
        )}
        self.adapter.before_success = self._assert_durable_before_success

        # 3. Actual command dispatch -> SDK HTTP transport -> durable private receipt.
        self._queue()
        created_operation, text = await self._invoke(bot, "becoming_want", seed="A synthetic paper craft experiment.")
        self.assertIn("Want created.", text)
        entry = self._rows("entries")[0]
        entry_id = entry["entry_id"]
        first_revision = self.store.get_history(entry_id, self.scope)[0]
        self.assertEqual(first_revision["content"], ORIGINAL)
        self.assertEqual([len(self._rows(t)) for t in ("entries", "revisions", "invocations")], [1, 1, 1])

        # 4. Read back exact content, status, timestamp and returned provenance.
        _, text = await self._invoke(bot, "becoming_show", entry_id=entry_id)
        for expected in (ORIGINAL, TITLE, "Status: active", "Revision: 1", first_revision["timestamp"],
                         REQUESTED_MODEL, "Synthetic Provider", "synthetic-completion-1"):
            self.assertIn(expected, text)

        # 5-6. Append revision 2, then privately inspect both unchanged ordered versions.
        self._queue("revise", content=REVISED, reason="Added a blue handle.")
        await self._invoke(bot, "becoming_revise", entry_id=entry_id, expected_revision=1,
                           instruction="Give this invented lantern a blue handle.")
        history = self.store.get_history(entry_id, self.scope)
        self.assertEqual(history[0], first_revision)
        self.assertEqual(history[1]["content"], REVISED)
        _, text = await self._invoke(bot, "becoming_history", entry_id=entry_id)
        self.assertLess(text.index("Revision 1"), text.index("Revision 2"))
        self.assertIn(ORIGINAL, text)
        self.assertIn(REVISED, text)

        # 7. A separate process opens the same database and retrieves the full history.
        code = (
            "import json,sys;from src.becoming import RoomStore,RoomScope;"
            "s=RoomStore(sys.argv[1]);s.initialize();scope=RoomScope('10','20','30');"
            "print(json.dumps({'entry':s.get_entry(sys.argv[2],scope),'history':s.get_history(sys.argv[2],scope)}))"
        )
        child = await asyncio.to_thread(subprocess.run, [sys.executable, "-B", "-c", code, str(self.path), entry_id],
                                        env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(Path(main.__file__).parent.parent)},
                                        capture_output=True, text=True, check=True)
        restarted = json.loads(child.stdout)
        self.assertEqual(restarted["entry"]["current_revision"], 2)
        self.assertEqual(restarted["history"], history)

        # 8-9. Duplicate replay and stale revision invoke no provider and add no revision.
        request_count = len(self.requests)
        await self._invoke(bot, "becoming_want", operation=created_operation, seed="Changed duplicate seed must be ignored.")
        _, stale = await self._invoke(bot, "becoming_revise", entry_id=entry_id, expected_revision=1, instruction="Stale attempt.")
        self.assertIn("no longer current", stale)
        self.assertEqual(len(self.requests), request_count)
        self.assertEqual(len(self._rows("entries")), 1)
        self.assertEqual(len(self._rows("revisions")), 2)

        # 10. Abstention persists a real receipt without an entry.
        self._queue("abstain", title="", content="", reason="No additional invented want during this invocation.")
        abstained_operation, text = await self._invoke(bot, "becoming_want", seed="Synthetic abstention case.")
        self.assertIn("No entry or revision was created.", text)
        self.assertEqual(next(r for r in self._rows("invocations") if r["operation_key"] == abstained_operation)["outcome"], "abstained")

        # 11. Real SDK failure shapes retain only allowlisted metadata; malformed output fails.
        self.provider_outputs.append((503, {"error": {"message": PRIVATE_ERROR, "metadata": {
            "model": "synthetic/failed-model", "provider_name": "Synthetic Error Provider",
            "response_id": "synthetic-failed-response", "raw": PRIVATE_ERROR,
        }}}, {}))
        failed_operation, text = await self._invoke(bot, "becoming_want", seed="SYNTHETIC_PRIVATE_FAILURE_SEED")
        self.assertIn("No success is being claimed", text)
        failed = next(r for r in self._rows("invocations") if r["operation_key"] == failed_operation)
        self.assertEqual((failed["outcome"], failed["returned_model"], failed["returned_provider"], failed["provider_response_id"]),
                         ("failed", "synthetic/failed-model", "Synthetic Error Provider", "synthetic-failed-response"))
        self.assertEqual(failed["error_category"], "provider_failure")
        self.provider_outputs.append((200, "SYNTHETIC_PRIVATE_MALFORMED_OUTPUT", {}))
        malformed_operation, text = await self._invoke(bot, "becoming_want", seed="Synthetic malformed case.")
        self.assertIn("No success is being claimed", text)
        self.assertEqual(next(r for r in self._rows("invocations") if r["operation_key"] == malformed_operation)["error_category"], "malformed_output")
        self.assertEqual(len(self._rows("entries")), 1)
        self.assertEqual(len(self._rows("revisions")), 2)
        self.assertEqual(len(self.requests), 5)  # Failed 503 gets one request, not SDK retries.

        # 12. Every denied caller/route is rejected before provider or storage access.
        with patch.object(self.store, "initialize", side_effect=AssertionError("Unauthorized storage access")) as initialize:
            denied_commands = (
                ("becoming_want", {"seed": "Synthetic denied seed."}),
                ("becoming_show", {"entry_id": entry_id}),
                ("becoming_revise", {"entry_id": entry_id, "expected_revision": 2,
                                     "instruction": "Synthetic denied revision."}),
                ("becoming_history", {"entry_id": entry_id}),
            )
            for route in ({"owner": 99}, {"channel": 99}, {"guild": 99}):
                for name, options in denied_commands:
                    _, denied = await self._invoke(bot, name, **options, **route)
                    self.assertIn("unavailable", denied)
            dm_bot, _, _, _ = await self._bot(config=self._config(guild=None))
            for name, options in denied_commands:
                _, denied = await self._invoke(dm_bot, name, **options, guild=None,
                                               command_guild=None, group_dm=True)
                self.assertIn("unavailable", denied)
            initialize.assert_not_called()
        self.assertEqual(len(self.requests), 5)

        # 13. Invalid custom repository path is rejected without creating a file.
        invalid_path = Path(main.__file__).parent.parent / "private-room.sqlite3"
        self.assertFalse(invalid_path.exists())
        with self.assertRaises(becoming.RoomError):
            becoming.RoomStore(invalid_path)
        self.assertFalse(invalid_path.exists())

        # 14. Optional Room sync failures preserve operational ordinary commands/timers.
        failed_bot, _, _, failed_syncs = await self._bot(config=self._config(guild=None), fail_room=True)
        self.assertTrue(failed_syncs)
        _, health = await self._invoke(failed_bot, "ordinary_health", command_guild=None)
        self.assertIn("ordinary companion command is operational", health)

        # 15. Database/source boundaries and logs remain clean after the complete flow.
        self.assertEqual(self.legacy_path.read_bytes(), legacy_snapshot)
        with sqlite3.connect(self.legacy_path) as conn:
            for table in ("messages", "journal_entries", "discord_recall_messages", "continuity_events"):
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)
        for path, initial in protected_sources.items():
            self.assertEqual(path.read_bytes(), initial)
        for spy in self.boundary_spies:
            spy.assert_not_called()
        logs = "\n".join(self.logs.lines) + self.stdout.getvalue() + self.stderr.getvalue()
        for private in (TITLE, ORIGINAL, REVISED, IDENTITY, PRIVATE_ERROR, "SYNTHETIC_PRIVATE",
                        "A synthetic paper craft experiment.", "Give this invented lantern a blue handle."):
            self.assertNotIn(private, logs)
        self.assertEqual(len(self._rows("invocations")), 6)
        receipts = json.dumps(self._rows("invocations"))
        for private in (PRIVATE_ERROR, "SYNTHETIC_PRIVATE_FAILURE_SEED", "SYNTHETIC_PRIVATE_MALFORMED_OUTPUT"):
            self.assertNotIn(private, receipts)
        self.assertEqual(self.provider_outputs, [])
        self.assertFalse(invalid_path.exists())
        self.assertTrue(all(Path(self.temp.name) in p.parents for p in (self.path, self.legacy_path)))

    async def test_real_discord_pagination_stays_private_and_checks_components(self):
        bot, _, _, _ = await self._bot(config=self.config)
        long_content = "Synthetic Unicode pagination fixture: " + "🫎" * 2000 + " @everyone <@10> END"
        self._queue(content=long_content)
        await self._invoke(bot, "becoming_want", seed="Synthetic pagination only.")
        entry_id = self._rows("entries")[0]["entry_id"]
        await self._invoke(bot, "becoming_show", entry_id=entry_id)
        items = [item for group in bot._connection._view_store._views.values() for item in group.values()]
        view = items[0].view
        self.assertIn(long_content, "".join(view.pages))
        self.assertGreater(len(view.pages), 2)
        for index in range(len(view.pages)):
            component = self._interaction(bot, "becoming_show", component=True)
            self.adapter.current_operation = str(component.id)
            before = len(self.adapter.deliveries)
            self.assertTrue(await view.interaction_check(component))
            await view._move(component, index)
            deliveries = self.adapter.deliveries[before:]
            self._assert_private(deliveries)
            self.assertEqual(deliveries[-1][2]["type"], 7)  # Edit the existing ephemeral message.
            self.assertIn(f"page {index + 1}/", deliveries[-1][2]["data"]["content"])
        for route in ({"owner": 99}, {"channel": 99}, {"guild": 99}):
            component = self._interaction(bot, "becoming_show", component=True, **route)
            self.adapter.current_operation = str(component.id)
            before = len(self.adapter.deliveries)
            self.assertFalse(await view.interaction_check(component))
            self._assert_private(self.adapter.deliveries[before:])
            self.assertNotIn("Synthetic Unicode", str(self.adapter.deliveries[before:]))


if __name__ == "__main__":
    unittest.main()
