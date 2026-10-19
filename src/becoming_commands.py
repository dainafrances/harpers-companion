"""Explicit, private Discord commands for the Room of Becoming.

This module deliberately has no ordinary chat, journal, recall, or scheduling
dependencies. Registering the commands does not open or initialize a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import discord
from discord import app_commands

from .becoming import RoomConfig, RoomError, RoomScope, RoomService


MAX_INPUT_LENGTH = 4000
MAX_ENTRY_ID_LENGTH = 64
PAGE_CONTENT_UNITS = 1800
_UNAVAILABLE = "This private Room command is unavailable here."
_UNCONFIRMED = (
    "The Room operation could not be confirmed. No success is being claimed. "
    "Inspect the entry before starting another operation."
)


def _scope(interaction: discord.Interaction, config: RoomConfig) -> RoomScope | None:
    """Authorize from trusted Discord context, including the actual DM type."""
    if not config.enabled or not config.valid:
        return None
    user_id = interaction.user.id
    guild_id = interaction.guild_id
    channel_id = interaction.channel_id
    if channel_id is None or not config.authorized(
        user_id=user_id,
        guild_id=guild_id,
        channel_id=channel_id,
        is_dm=isinstance(interaction.channel, discord.DMChannel),
    ):
        return None
    return RoomScope(
        owner_id=str(user_id),
        channel_id=str(channel_id),
        guild_id=str(guild_id) if guild_id is not None else None,
    )


async def _private_message(interaction: discord.Interaction, content: str) -> bool:
    """Keep every delivery private, including failures; never use channel.send."""
    try:
        kwargs = {"ephemeral": True, "allowed_mentions": discord.AllowedMentions.none()}
        if interaction.response.is_done():
            await interaction.followup.send(content, **kwargs)
        else:
            await interaction.response.send_message(content, **kwargs)
        return True
    except Exception:
        # Do not let Discord's command error handler log input or entry text.
        # A failed private delivery must never fall back to a public message.
        return False


def _pages(text: str) -> list[str]:
    """Split without dropping characters, with room for page labels and emoji."""
    pages: list[str] = []
    start = 0
    units = 0
    for index, character in enumerate(text):
        character_units = 2 if ord(character) > 0xFFFF else 1
        if units + character_units > PAGE_CONTENT_UNITS:
            pages.append(text[start:index])
            start = index
            units = 0
        units += character_units
    pages.append(text[start:])
    return pages


class _PrivatePages(discord.ui.View):
    """All pages remain in the originating, authorized private interaction."""

    def __init__(self, pages: list[str], *, config: RoomConfig, scope: RoomScope) -> None:
        super().__init__(timeout=840)
        self.pages = pages
        self.config = config
        self.scope = scope
        self.index = 0
        self._update_buttons()

    def content(self) -> str:
        return f"Room of Becoming · page {self.index + 1}/{len(self.pages)}\n\n{self.pages[self.index]}"

    def _update_buttons(self) -> None:
        self.first.disabled = self.previous.disabled = self.index == 0
        self.next_page.disabled = self.last.disabled = self.index == len(self.pages) - 1

    def _authorized(self, interaction: discord.Interaction) -> bool:
        return _scope(interaction, self.config) == self.scope

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self._authorized(interaction):
            return True
        await _private_message(interaction, _UNAVAILABLE)
        return False

    async def _move(self, interaction: discord.Interaction, index: int) -> None:
        # Recheck here as well as interaction_check so no component callback can
        # change pages when called independently or from a changed route.
        if not self._authorized(interaction):
            await _private_message(interaction, _UNAVAILABLE)
            return
        self.index = min(max(index, 0), len(self.pages) - 1)
        self._update_buttons()
        try:
            await interaction.response.edit_message(
                content=self.content(),
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            await _private_message(interaction, "This private page could not be updated.")

    @discord.ui.button(label="First", style=discord.ButtonStyle.secondary)
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._move(interaction, 0)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._move(interaction, self.index - 1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._move(interaction, self.index + 1)

    @discord.ui.button(label="Last", style=discord.ButtonStyle.secondary)
    async def last(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._move(interaction, len(self.pages) - 1)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        # The default View handler logs the exception. Keep private payloads out
        # of that path even if a component encounters an unexpected failure.
        await _private_message(interaction, "This private page could not be updated.")


async def _send_output(
    interaction: discord.Interaction, text: str, *, config: RoomConfig, scope: RoomScope
) -> None:
    pages = _pages(text)
    if len(pages) == 1:
        await _private_message(interaction, pages[0])
        return
    view = _PrivatePages(pages, config=config, scope=scope)
    try:
        kwargs = {
            "ephemeral": True,
            "allowed_mentions": discord.AllowedMentions.none(),
            "view": view,
        }
        if interaction.response.is_done():
            await interaction.followup.send(view.content(), **kwargs)
        else:
            await interaction.response.send_message(view.content(), **kwargs)
    except Exception:
        view.stop()
        # No public fallback and no exception/body logging.


def _metadata(value: Any) -> str:
    if value is None or value == "":
        return "UNKNOWN"
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text if len(text) <= 160 else text[:159] + "…"


def _provenance(row: dict[str, Any]) -> str:
    return (
        f"Invocation: {_metadata(row.get('invocation_id'))}\n"
        f"Requested model: {_metadata(row.get('requested_model'))}\n"
        f"Returned model: {_metadata(row.get('returned_model'))}\n"
        f"Returned provider: {_metadata(row.get('returned_provider'))}\n"
        f"Provider response: {_metadata(row.get('provider_response_id'))}"
    )


def _receipt(row: dict[str, Any]) -> str:
    outcome = row.get("outcome")
    if outcome in {"created", "revised"}:
        heading = (
            f"Want {outcome}.\nEntry: {_metadata(row.get('entry_id'))}\n"
            f"Revision: {_metadata(row.get('revision_number'))}"
        )
    elif outcome == "abstained":
        heading = f"No entry or revision was created.\nAbstention: {row.get('abstention_reason') or 'No reason supplied.'}"
    elif outcome == "denied":
        category = row.get("error_category")
        heading = (
            _error_text(category)
            if category in {"stale_revision", "revision_conflict", "not_found", "entry_not_found"}
            else _UNAVAILABLE
        )
    elif outcome == "failed":
        heading = _error_text(row.get("error_category"))
    else:
        heading = "This operation is already recorded and has not completed. It was not repeated."
    return (
        f"{heading}\n\nOutcome: {_metadata(outcome) if outcome is not None else 'pending'}\n"
        f"Started: {_metadata(row.get('started_at'))}\n"
        f"Completed: {_metadata(row.get('completed_at'))}\n{_provenance(row)}"
    )


def _entry(row: dict[str, Any], entry_id: str) -> str:
    return (
        f"Entry: {_metadata(row.get('entry_id') or entry_id)}\n"
        f"Status: {_metadata(row.get('status'))}\n"
        f"Revision: {_metadata(row.get('current_revision'))}\n"
        f"Author: {_metadata(row.get('author_classification'))}\n"
        f"Created: {_metadata(row.get('created_at'))}\n"
        f"Revision timestamp: {_metadata(row.get('timestamp'))}\n\n"
        f"{row.get('title', '')}\n\n{row.get('content', '')}\n\n{_provenance(row)}"
    )


def _history(rows: list[dict[str, Any]], entry_id: str) -> str:
    sections = [f"Entry: {entry_id} · complete revision history"]
    for row in sorted(rows, key=lambda item: item["revision_number"]):
        sections.append(
            f"Revision {_metadata(row.get('revision_number'))}\n"
            f"Parent revision: {_metadata(row.get('parent_revision_number'))}\n"
            f"Author: {_metadata(row.get('author_classification'))}\n"
            f"Timestamp: {_metadata(row.get('timestamp'))}\n"
            f"Reason: {row.get('revision_reason', '')}\n\n"
            f"{row.get('title', '')}\n\n{row.get('content', '')}\n\n{_provenance(row)}"
        )
    return "\n\n---\n\n".join(sections)


def _error_text(category: Any) -> str:
    # Display only application-owned wording, never raw exceptions, model
    # errors, SQL errors, or an untrusted category returned by a provider.
    if category in {"stale_revision", "revision_conflict"}:
        return "That revision is no longer current. Show the entry and use its current revision number."
    if category in {"not_found", "entry_not_found"}:
        return "No entry was found in this private workspace."
    if category in {"unauthorized", "denied"}:
        return _UNAVAILABLE
    return _UNCONFIRMED


def _valid_entry_id(entry_id: str) -> bool:
    return (
        0 < len(entry_id) <= MAX_ENTRY_ID_LENGTH
        and entry_id.isascii()
        and all(character.isalnum() or character in "-_" for character in entry_id)
    )


@dataclass(frozen=True)
class RoomCommands:
    becoming_want: Callable[..., Awaitable[None]]
    becoming_show: Callable[..., Awaitable[None]]
    becoming_revise: Callable[..., Awaitable[None]]
    becoming_history: Callable[..., Awaitable[None]]
    commands: tuple[app_commands.Command, ...]


async def sync_dm_commands(bot: Any, registered_commands: RoomCommands) -> list[Any]:
    """Sync Room globals without publishing legacy guild-only commands globally.

    Call during the existing startup sync sequence. The removed definitions are
    restored locally even when Discord rejects the sync; no storage is touched.
    """
    room_commands = set(registered_commands.commands)
    removed: list[Any] = []
    try:
        for command in bot.tree.get_commands():
            if command in room_commands:
                continue
            command_type = (
                command.type
                if isinstance(command, app_commands.ContextMenu)
                else discord.AppCommandType.chat_input
            )
            previous = bot.tree.remove_command(command.name, type=command_type)
            if previous is not None:
                removed.append(previous)
        return await bot.tree.sync()
    finally:
        for command in removed:
            bot.tree.add_command(command, override=True)


def register_commands(
    bot: Any, *, config: RoomConfig, service: RoomService | None
) -> RoomCommands:
    """Register private commands without reading or initializing any storage."""

    async def prepare(interaction: discord.Interaction) -> RoomScope | None:
        scope = _scope(interaction, config)
        if scope is None or service is None:
            await _private_message(interaction, _UNAVAILABLE)
            return None
        return scope

    async def execute(
        interaction: discord.Interaction,
        scope: RoomScope,
        operation: Callable[[], Awaitable[str]],
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except Exception:
            return
        try:
            result = await operation()
        except RoomError as error:
            result = _error_text(error.category)
        except Exception:
            result = _UNCONFIRMED
        await _send_output(interaction, result, config=config, scope=scope)

    async def becoming_want(interaction: discord.Interaction, seed: str | None = None) -> None:
        scope = await prepare(interaction)
        if scope is None:
            return
        if seed is not None and len(seed) > MAX_INPUT_LENGTH:
            await _private_message(interaction, f"Keep the seed to {MAX_INPUT_LENGTH} characters or fewer.")
            return

        async def operation() -> str:
            return _receipt(await service.create(scope, str(interaction.id), seed or ""))

        await execute(interaction, scope, operation)

    async def becoming_show(interaction: discord.Interaction, entry_id: str) -> None:
        scope = await prepare(interaction)
        if scope is None:
            return
        if not _valid_entry_id(entry_id):
            await _private_message(interaction, "Use the entry ID from its Room receipt.")
            return

        async def operation() -> str:
            row = await service.show(scope, entry_id)
            return _entry(row, entry_id) if row is not None else _error_text("not_found")

        await execute(interaction, scope, operation)

    async def becoming_revise(
        interaction: discord.Interaction, entry_id: str, expected_revision: int, instruction: str
    ) -> None:
        scope = await prepare(interaction)
        if scope is None:
            return
        if not _valid_entry_id(entry_id):
            await _private_message(interaction, "Use the entry ID from its Room receipt.")
            return
        if expected_revision < 1 or expected_revision > 2_147_483_647:
            await _private_message(interaction, "Use the current positive revision number shown on the entry.")
            return
        if not instruction.strip() or len(instruction) > MAX_INPUT_LENGTH:
            await _private_message(
                interaction, f"Provide a revision instruction between 1 and {MAX_INPUT_LENGTH} characters."
            )
            return

        async def operation() -> str:
            return _receipt(
                await service.revise(
                    scope, str(interaction.id), entry_id, expected_revision, instruction
                )
            )

        await execute(interaction, scope, operation)

    async def becoming_history(interaction: discord.Interaction, entry_id: str) -> None:
        scope = await prepare(interaction)
        if scope is None:
            return
        if not _valid_entry_id(entry_id):
            await _private_message(interaction, "Use the entry ID from its Room receipt.")
            return

        async def operation() -> str:
            rows = await service.history(scope, entry_id)
            return _history(rows, entry_id) if rows else _error_text("not_found")

        await execute(interaction, scope, operation)

    definitions = (
        (becoming_want, "Create one private want, or record an abstention."),
        (becoming_show, "Show a private Room entry and its provenance."),
        (becoming_revise, "Append a revision while preserving the earlier text."),
        (becoming_history, "Inspect every revision of a private Room entry."),
    )
    discord_commands = []
    for callback, description in definitions:
        private_contexts = app_commands.allowed_contexts(guilds=True, dms=True, private_channels=False)
        command = app_commands.command(name=callback.__name__, description=description)(
            private_contexts(callback)
        )
        bot.tree.add_command(command)
        discord_commands.append(command)

    return RoomCommands(
        becoming_want, becoming_show, becoming_revise, becoming_history, tuple(discord_commands)
    )
