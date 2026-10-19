from __future__ import annotations

import ast
import inspect
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from src import becoming_commands as commands
from src.becoming import RoomScope


@dataclass
class FakeConfig:
    enabled: bool = True
    valid: bool = True
    owner_id: int = 10
    channel_id: int = 20
    guild_id: int | None = 30

    def authorized(self, *, user_id, guild_id, channel_id, is_dm) -> bool:
        return (
            self.enabled
            and self.valid
            and user_id == self.owner_id
            and channel_id == self.channel_id
            and guild_id == self.guild_id
            and (self.guild_id is not None or is_dm)
        )


class FakeTree:
    def __init__(self) -> None:
        self.commands = []

    def add_command(self, command, *, override=False) -> None:
        if override:
            self.commands = [existing for existing in self.commands if existing.name != command.name]
        self.commands.append(command)

    def get_commands(self):
        return list(self.commands)

    def remove_command(self, name, *, type):
        for command in self.commands:
            if command.name == name:
                self.commands.remove(command)
                return command
        return None


class FakeInteraction:
    def __init__(
        self,
        *,
        user_id: int = 10,
        channel_id: int | None = 20,
        guild_id: int | None = 30,
        dm: bool = False,
        group_dm: bool = False,
    ) -> None:
        self.id = 123456789
        self.user = SimpleNamespace(id=user_id)
        self.channel_id = channel_id
        self.guild_id = guild_id
        channel_class = discord.DMChannel if dm else discord.GroupChannel if group_dm else discord.TextChannel
        self.channel = Mock(spec=channel_class)
        self.channel.send = AsyncMock()
        self.done = False

        async def finish(*args, **kwargs):
            self.done = True

        self.response = SimpleNamespace(
            is_done=lambda: self.done,
            send_message=AsyncMock(side_effect=finish),
            defer=AsyncMock(side_effect=finish),
            edit_message=AsyncMock(side_effect=finish),
        )
        self.followup = SimpleNamespace(send=AsyncMock())

    def sent(self):
        return self.response.send_message.call_args_list + self.followup.send.call_args_list


def receipt(outcome="created", **kwargs):
    return {
        "invocation_id": "invocation-1",
        "outcome": outcome,
        "entry_id": "entry-1",
        "revision_number": 1,
        "abstention_reason": None,
        "error_category": None,
        "requested_model": "test-requested",
        "returned_model": None,
        "returned_provider": None,
        "provider_response_id": None,
        "started_at": "2026-10-19T00:00:00Z",
        "completed_at": "2026-10-19T00:00:01Z",
        **kwargs,
    }


def entry(**kwargs):
    return {
        **receipt(),
        "status": "active",
        "current_revision": 2,
        "revision_number": 2,
        "title": "A place for choices",
        "content": "Exact private content @everyone <@10>",
        "created_at": "2026-10-19T00:00:00Z",
        "timestamp": "2026-10-19T00:02:00Z",
        "author_classification": "colin_model",
        **kwargs,
    }


class BecomingCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.config = FakeConfig()
        self.service = SimpleNamespace(
            create=AsyncMock(return_value=receipt()),
            show=AsyncMock(return_value=entry()),
            revise=AsyncMock(return_value=receipt("revised", revision_number=2)),
            history=AsyncMock(return_value=[]),
        )
        self.bot = SimpleNamespace(tree=FakeTree())
        self.callbacks = commands.register_commands(self.bot, config=self.config, service=self.service)

    def assert_private(self, interaction) -> None:
        for call in interaction.sent():
            self.assertTrue(call.kwargs["ephemeral"])
            mentions = call.kwargs["allowed_mentions"]
            self.assertFalse(mentions.everyone)
            self.assertFalse(mentions.users)
            self.assertFalse(mentions.roles)
            self.assertFalse(mentions.replied_user)
        interaction.channel.send.assert_not_awaited()

    def assert_service_unused(self) -> None:
        for method in (self.service.create, self.service.show, self.service.revise, self.service.history):
            method.assert_not_awaited()

    async def invoke_all(self, interaction_factory) -> None:
        invocations = (
            (self.callbacks.becoming_want, ()),
            (self.callbacks.becoming_show, ("entry-1",)),
            (self.callbacks.becoming_revise, ("entry-1", 1, "Revise explicitly.")),
            (self.callbacks.becoming_history, ("entry-1",)),
        )
        for callback, args in invocations:
            interaction = interaction_factory()
            await callback(interaction, *args)
            self.assertEqual(len(interaction.sent()), 1)
            self.assertIn("unavailable", interaction.sent()[0].args[0])
            self.assert_private(interaction)
        self.assert_service_unused()

    async def test_registration_is_storage_free_and_declares_only_guild_and_bot_dm(self):
        self.assertEqual(
            [command.name for command in self.bot.tree.commands],
            ["becoming_want", "becoming_show", "becoming_revise", "becoming_history"],
        )
        for command in self.bot.tree.commands:
            self.assertTrue(command.allowed_contexts.guild)
            self.assertTrue(command.allowed_contexts.dm_channel)
            self.assertFalse(command.allowed_contexts.private_channel)
        self.assert_service_unused()

    async def test_dm_sync_sends_only_room_globals_then_restores_legacy_commands(self):
        async def legacy_callback(interaction: discord.Interaction):
            pass

        legacy = discord.app_commands.command(name="legacy", description="Existing command")(legacy_callback)
        self.bot.tree.add_command(legacy)
        original = set(self.bot.tree.get_commands())

        async def sync():
            self.assertEqual(set(self.bot.tree.get_commands()), set(self.callbacks.commands))
            return ["synced"]

        self.bot.tree.sync = AsyncMock(side_effect=sync)
        result = await commands.sync_dm_commands(self.bot, self.callbacks)
        self.assertEqual(result, ["synced"])
        self.assertEqual(set(self.bot.tree.get_commands()), original)
        self.assert_service_unused()

    async def test_dm_sync_restores_legacy_commands_after_sync_failure(self):
        async def legacy_callback(interaction: discord.Interaction):
            pass

        legacy = discord.app_commands.command(name="legacy", description="Existing command")(legacy_callback)
        self.bot.tree.add_command(legacy)
        original = set(self.bot.tree.get_commands())
        self.bot.tree.sync = AsyncMock(side_effect=RuntimeError("Discord sync failed"))
        with self.assertRaises(RuntimeError):
            await commands.sync_dm_commands(self.bot, self.callbacks)
        self.assertEqual(set(self.bot.tree.get_commands()), original)
        self.assert_service_unused()

    async def test_every_command_denies_wrong_owner(self):
        await self.invoke_all(lambda: FakeInteraction(user_id=99))

    async def test_every_command_denies_wrong_channel(self):
        await self.invoke_all(lambda: FakeInteraction(channel_id=99))

    async def test_every_command_denies_wrong_guild(self):
        await self.invoke_all(lambda: FakeInteraction(guild_id=99))

    async def test_every_command_denies_missing_channel_context(self):
        await self.invoke_all(lambda: FakeInteraction(channel_id=None))

    async def test_every_command_fails_closed_when_disabled(self):
        self.config.enabled = False
        await self.invoke_all(FakeInteraction)

    async def test_every_command_fails_closed_when_privacy_config_invalid(self):
        self.config.valid = False
        await self.invoke_all(FakeInteraction)

    async def test_every_command_fails_closed_without_service(self):
        self.callbacks = commands.register_commands(
            SimpleNamespace(tree=FakeTree()), config=self.config, service=None
        )
        await self.invoke_all(FakeInteraction)

    async def test_dm_only_route_rejects_group_dm_and_untyped_private_channel(self):
        self.config.guild_id = None
        await self.invoke_all(lambda: FakeInteraction(guild_id=None, group_dm=True))
        await self.invoke_all(lambda: FakeInteraction(guild_id=None))

    async def test_valid_dm_reaches_service_with_exact_scope(self):
        self.config.guild_id = None
        interaction = FakeInteraction(guild_id=None, dm=True)
        await self.callbacks.becoming_want(interaction)
        self.service.create.assert_awaited_once_with(
            RoomScope(owner_id="10", channel_id="20", guild_id=None), "123456789", ""
        )
        self.assert_private(interaction)

    async def test_create_records_interaction_key_and_explicit_seed(self):
        interaction = FakeInteraction()
        seed = "An explicit seed with @everyone."
        await self.callbacks.becoming_want(interaction, seed)
        self.service.create.assert_awaited_once_with(
            RoomScope(owner_id="10", channel_id="20", guild_id="30"), "123456789", seed
        )
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        result = interaction.followup.send.call_args.args[0]
        self.assertIn("Want created", result)
        self.assertIn("entry-1", result)
        self.assertIn("Returned model: UNKNOWN", result)
        self.assert_private(interaction)

    async def test_show_displays_current_content_status_revision_and_provenance(self):
        interaction = FakeInteraction()
        await self.callbacks.becoming_show(interaction, "entry-1")
        self.service.show.assert_awaited_once()
        text = interaction.followup.send.call_args.args[0]
        for expected in (
            "Exact private content @everyone <@10>", "Status: active", "Revision: 2",
            "Author: colin_model", "2026-10-19T00:02:00Z", "test-requested", "UNKNOWN",
        ):
            self.assertIn(expected, text)
        self.assert_private(interaction)

    async def test_revise_passes_expected_revision_and_instruction_verbatim(self):
        interaction = FakeInteraction()
        instruction = "  Preserve this explicit instruction.  "
        await self.callbacks.becoming_revise(interaction, "entry-1", 7, instruction)
        self.service.revise.assert_awaited_once_with(
            RoomScope(owner_id="10", channel_id="20", guild_id="30"),
            "123456789", "entry-1", 7, instruction,
        )
        self.assertIn("Want revised", interaction.followup.send.call_args.args[0])
        self.assert_private(interaction)

    async def test_abstention_displays_receipt_and_honest_reason(self):
        self.service.create.return_value = receipt(
            "abstained", entry_id=None, revision_number=None, abstention_reason="No want to record."
        )
        interaction = FakeInteraction()
        await self.callbacks.becoming_want(interaction)
        text = interaction.followup.send.call_args.args[0]
        self.assertIn("No entry or revision was created", text)
        self.assertIn("No want to record.", text)
        self.assertNotIn("Want created", text)
        self.assert_private(interaction)

    async def test_pending_duplicate_does_not_claim_success(self):
        self.service.create.return_value = receipt(None, completed_at=None)
        interaction = FakeInteraction()
        await self.callbacks.becoming_want(interaction)
        text = interaction.followup.send.call_args.args[0]
        self.assertIn("not completed", text)
        self.assertIn("not repeated", text)
        self.assertNotIn("Want created", text)

    async def test_failures_never_echo_or_log_exception_or_private_input(self):
        self.service.create.side_effect = RuntimeError("SECRET provider error: private entry content")
        interaction = FakeInteraction()
        with patch("logging.Logger._log") as log:
            await self.callbacks.becoming_want(interaction, "SECRET seed")
        log.assert_not_called()
        text = interaction.followup.send.call_args.args[0]
        self.assertNotIn("SECRET", text)
        self.assertNotIn("Want created", text)
        self.assertIn("No success is being claimed", text)
        self.assert_private(interaction)

    async def test_failed_receipt_never_displays_untrusted_error_category(self):
        self.service.create.return_value = receipt("failed", error_category="SECRET SQL/provider text")
        interaction = FakeInteraction()
        await self.callbacks.becoming_want(interaction)
        text = interaction.followup.send.call_args.args[0]
        self.assertNotIn("SECRET", text)
        self.assertIn("No success is being claimed", text)

    async def test_stale_revision_receipt_is_actionable_without_claiming_success(self):
        self.service.revise.return_value = receipt("denied", error_category="stale_revision")
        interaction = FakeInteraction()
        await self.callbacks.becoming_revise(interaction, "entry-1", 1, "Change it.")
        text = interaction.followup.send.call_args.args[0]
        self.assertIn("no longer current", text)
        self.assertNotIn("Want revised", text)

    async def test_missing_entry_does_not_reveal_other_workspaces(self):
        self.service.show.return_value = None
        interaction = FakeInteraction()
        await self.callbacks.becoming_show(interaction, "entry-1")
        self.assertEqual(
            interaction.followup.send.call_args.args[0], "No entry was found in this private workspace."
        )

    async def test_inputs_are_bounded_before_invocation(self):
        invalid = (
            (self.callbacks.becoming_want, ("x" * 4001,)),
            (self.callbacks.becoming_show, ("x" * 65,)),
            (self.callbacks.becoming_history, ("bad\nentry",)),
            (self.callbacks.becoming_revise, ("entry-1", 0, "Change it.")),
            (self.callbacks.becoming_revise, ("entry-1", 2_147_483_648, "Change it.")),
            (self.callbacks.becoming_revise, ("entry-1", 1, "   ")),
            (self.callbacks.becoming_revise, ("entry-1", 1, "x" * 4001)),
        )
        for callback, args in invalid:
            interaction = FakeInteraction()
            await callback(interaction, *args)
            self.assertEqual(len(interaction.sent()), 1)
            interaction.response.defer.assert_not_awaited()
            self.assert_private(interaction)
        self.assert_service_unused()

    async def test_all_history_and_long_unicode_content_survive_private_pagination(self):
        first = entry(
            revision_number=1, parent_revision_number=None,
            content="🫎" * 2000 + "FIRST EXACT END", revision_reason="Created explicitly.",
        )
        second = entry(
            revision_number=2, parent_revision_number=1,
            content="SECOND @everyone " * 300 + "SECOND EXACT END", revision_reason="Explicit revision.",
        )
        self.service.history.return_value = [second, first]
        interaction = FakeInteraction()
        await self.callbacks.becoming_history(interaction, "entry-1")
        call = interaction.followup.send.call_args
        view = call.kwargs["view"]
        self.addCleanup(view.stop)
        rendered = "".join(view.pages)
        self.assertIn(first["content"], rendered)
        self.assertIn(second["content"], rendered)
        self.assertLess(rendered.index("Revision 1"), rendered.index("Revision 2"))
        self.assertIn("Parent revision: 1", rendered)
        self.assertIn("Author: colin_model", rendered)
        self.assertIn("Explicit revision.", rendered)
        self.assertGreater(len(view.pages), 2)
        self.assert_private(interaction)
        for index in range(len(view.pages)):
            component = FakeInteraction()
            await view._move(component, index)
            edit = component.response.edit_message.call_args
            self.assertIn(f"page {index + 1}/{len(view.pages)}", edit.kwargs["content"])
            self.assertLessEqual(len(edit.kwargs["content"].encode("utf-16-le")) // 2, 2000)
            self.assertFalse(edit.kwargs["allowed_mentions"].everyone)
            self.assertFalse(edit.kwargs["allowed_mentions"].users)
            component.channel.send.assert_not_awaited()

    async def test_every_pagination_component_rechecks_owner_and_route(self):
        scope = RoomScope(owner_id="10", channel_id="20", guild_id="30")
        view = commands._PrivatePages(["private one", "private two"], config=self.config, scope=scope)
        self.addCleanup(view.stop)
        for interaction in (
            FakeInteraction(user_id=99), FakeInteraction(channel_id=99), FakeInteraction(guild_id=99),
        ):
            self.assertFalse(await view.interaction_check(interaction))
            self.assert_private(interaction)
        for button in view.children:
            for interaction in (
                FakeInteraction(user_id=99), FakeInteraction(channel_id=99), FakeInteraction(guild_id=99),
            ):
                await button.callback(interaction)
                interaction.response.edit_message.assert_not_awaited()
                self.assertNotIn("private one", interaction.sent()[0].args[0])
                self.assert_private(interaction)
        self.assertEqual(view.index, 0)

    async def test_pagination_rechecks_disabled_configuration(self):
        view = commands._PrivatePages(
            ["private one", "private two"], config=self.config,
            scope=RoomScope(owner_id="10", channel_id="20", guild_id="30"),
        )
        self.addCleanup(view.stop)
        self.config.enabled = False
        interaction = FakeInteraction()
        await view._move(interaction, 1)
        interaction.response.edit_message.assert_not_awaited()
        self.assert_private(interaction)

    async def test_delivery_failure_never_falls_back_public_or_logs(self):
        interaction = FakeInteraction()
        interaction.followup.send.side_effect = RuntimeError("SECRET delivery payload")
        with patch("logging.Logger._log") as log:
            await self.callbacks.becoming_show(interaction, "entry-1")
        log.assert_not_called()
        interaction.channel.send.assert_not_awaited()

    async def test_commands_have_no_ordinary_memory_identity_or_scheduler_imports(self):
        tree = ast.parse(inspect.getsource(commands))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertFalse(
            set(imports) & {
                "memory", "identity", "router", "continuity", "discord_recall", "presence", "paintbox", "tasks"
            }
        )


if __name__ == "__main__":
    unittest.main()
