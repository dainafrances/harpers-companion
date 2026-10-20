"""Private backup operations and a UTC scheduler, isolated from model context.

Scheduled messages can only use the configured one-to-one owner DM. Guild
interactions remain ephemeral; a scheduler cannot make a guild message
ephemeral and therefore never sends a channel message to a guild route.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import re
from typing import Any, Awaitable, Callable
from uuid import UUID

import discord
from discord import app_commands

from .becoming import RoomConfig
from .becoming_backup import BACKUP_CATEGORIES, VERIFICATION_REASONS, BackupError
from .becoming_commands import _private_message, _scope, sync_room_commands


_UNAVAILABLE = "This private Room backup command is unavailable here."
_CATEGORIES = BACKUP_CATEGORIES | {"unavailable", "failed"}
_TIERS = ("daily", "weekly", "monthly", "manual")


def _category(value: object) -> str:
    return value if isinstance(value, str) and value in _CATEGORIES else "unavailable"


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 40:
        return "unavailable"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return "unavailable"
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return "unavailable"


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000 else 0


def format_status(status: object) -> str:
    """Project a status onto a fixed bounded display; never stringify unknowns."""
    status = status if isinstance(status, dict) else {}
    successful = status.get("successful_tiers")
    failed = status.get("failed_tiers")
    successful = successful if isinstance(successful, (list, tuple)) else ()
    failed = failed if isinstance(failed, (list, tuple, dict)) else ()
    identifier = "unavailable"
    if isinstance(status.get("backup_id"), str) and len(status["backup_id"]) <= 36:
        try:
            identifier = str(UUID(status["backup_id"]))
        except ValueError:
            pass
    commit = status.get("code_commit")
    commit = commit if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) else "unavailable"
    size = status.get("ciphertext_size")
    size = size if isinstance(size, int) and not isinstance(size, bool) and 0 <= size <= 2**40 else 0
    tiers = status.get("tier_results")
    tiers = tiers if isinstance(tiers, dict) else {}
    tier_lines = []
    for tier in _TIERS:
        detail = tiers.get(tier)
        if not isinstance(detail, dict):
            continue
        outcome = detail.get("outcome")
        if not isinstance(outcome, str) or outcome not in {"verified", "not_uploaded", "remote_unverified"}:
            continue
        reason = detail.get("reason")
        reason = reason if isinstance(reason, str) and reason in BACKUP_CATEGORIES | VERIFICATION_REASONS else None
        tier_lines.append(f"{tier}: {outcome}" + (f" ({reason})" if reason else ""))
    return "\n".join((
        "Room backup operations",
        f"Outcome: {_category(status.get('outcome_category'))}",
        f"Last attempt: {_timestamp(status.get('last_attempt_time'))}",
        f"Last success: {_timestamp(status.get('last_successful_time'))}",
        f"Backup ID: {identifier}",
        "Successful tiers: " + (", ".join(tier for tier in _TIERS if tier in successful) or "none"),
        "Failed tiers: " + (", ".join(tier for tier in _TIERS if tier in failed) or "none"),
        "Tier results: " + ("; ".join(tier_lines) or "none"),
        "Cleanup failed: " + ("yes" if status.get("cleanup_failed") is True else "no"),
        f"Ciphertext bytes: {size}",
        f"Schema version: {_count(status.get('schema_version'))}",
        f"Code commit: {commit}",
        f"Consecutive failures: {_count(status.get('consecutive_failure_count'))}",
        "Retention verified: " + ("yes" if status.get("retention_verified") is True else "no"),
        "Download verified: " + ("yes" if status.get("download_verified") is True else "no"),
    ))


def next_backup_time(now: datetime, scheduled_time: time) -> datetime:
    """Return the next strictly future daily boundary, always in UTC."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("aware_utc_time_required")
    now = now.astimezone(timezone.utc)
    candidate = datetime.combine(now.date(), scheduled_time, tzinfo=timezone.utc)
    return candidate if candidate > now else candidate + timedelta(days=1)


def tiers_for_date(now: datetime) -> tuple[str, ...]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("aware_utc_time_required")
    utc = now.astimezone(timezone.utc)
    return ("daily",) + (("weekly",) if utc.weekday() == 0 else ()) + (("monthly",) if utc.day == 1 else ())


class BackupScheduler:
    def __init__(
        self, bot: Any, *, room_config: RoomConfig, manager: Any,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.bot, self.room_config, self.manager = bot, room_config, manager
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._task: asyncio.Task | None = None
        self._attempt_lock = asyncio.Lock()
        self._success_confirmed = False
        self.notification_state = "not_attempted"

    def start(self) -> bool:
        if (
            not self.room_config.enabled or not self.room_config.valid
            or not self.manager.config.can_run
            or (self._task is not None and not self._task.done())
        ):
            return False
        self._task = asyncio.create_task(self._loop(), name="room-backup-scheduler")
        return True

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            now = self._now()
            due = next_backup_time(now, self.manager.config.time_utc)
            await self._sleep(max(0.0, (due - now.astimezone(timezone.utc)).total_seconds()))
            # Per-run errors are bounded and cannot terminate later attempts.
            try:
                await self.run_once(now=due)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.notification_state = "unavailable"

    async def _notify(self, text: str) -> None:
        config = self.room_config
        self.notification_state = "unavailable"
        if not config.enabled or not config.valid or config.guild_id is not None:
            return
        try:
            channel = self.bot.get_channel(int(config.channel_id))
            if channel is None:
                channel = await asyncio.wait_for(self.bot.fetch_channel(int(config.channel_id)), timeout=10)
            if (
                not isinstance(channel, discord.DMChannel)
                or str(channel.id) != config.channel_id
                or channel.recipient is None
                or str(channel.recipient.id) != config.owner_id
                or self.bot.user is None
                or channel.me.id != self.bot.user.id
            ):
                return
            await asyncio.wait_for(
                channel.send(text, allowed_mentions=discord.AllowedMentions.none()), timeout=10,
            )
        except Exception:
            self.notification_state = "delivery_failed"
        else:
            self.notification_state = "delivered"

    async def run_once(self, *, now: datetime | None = None) -> dict[str, Any] | None:
        if (
            not self.room_config.enabled or not self.room_config.valid
            or not self.manager.config.can_run or self._attempt_lock.locked()
        ):
            return None
        async with self._attempt_lock:
            now = now or self._now()
            try:
                result = await self.manager.run(tiers=tiers_for_date(now), now=now)
            except BackupError as error:
                result = {"outcome_category": _category(error.category), "consecutive_failure_count": 0}
            except Exception:
                result = {"outcome_category": "failed", "consecutive_failure_count": 0}
            if not isinstance(result, dict):
                result = {"outcome_category": "failed", "consecutive_failure_count": 0}
            category = _category(result.get("outcome_category"))
            verified = (
                category == "success" and bool(result.get("successful_tiers"))
                and not result.get("failed_tiers")
                and result.get("retention_verified") is True
                and result.get("download_verified") is True
            )
            if not verified or not self._success_confirmed:
                heading = "Room backup verified off-volume." if verified else "Room backup attempt failed."
                await self._notify(
                    f"{heading}\nTime: {now.astimezone(timezone.utc).isoformat(timespec='seconds')}\n"
                    f"Category: {category}\n"
                    f"Consecutive failures: {_count(result.get('consecutive_failure_count'))}"
                )
            if verified:
                # Confirm at most once per process, even when Discord delivery
                # was unavailable. No success-message backlog on reconnect.
                self._success_confirmed = True
            return result


@dataclass(frozen=True)
class BackupCommands:
    becoming_backup_now: Callable[..., Awaitable[None]]
    becoming_backup_status: Callable[..., Awaitable[None]]
    commands: tuple[app_commands.Command, ...]


def register_commands(bot: Any, *, config: RoomConfig, manager: Any) -> BackupCommands | None:
    if not config.enabled or not config.valid or manager is None or not manager.config.can_run:
        return None
    previous = getattr(bot, "_room_backup_registration", None)
    if previous is not None:
        previous_config, previous_manager, registered = previous
        if previous_config != config or previous_manager is not manager:
            raise BackupError("registration_conflict")
        return registered

    async def becoming_backup_now(interaction: discord.Interaction) -> None:
        if _scope(interaction, config) is None:
            await _private_message(interaction, _UNAVAILABLE)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except Exception:
            return
        try:
            if manager.active:
                result = {"outcome_category": "busy"}
            else:
                result = await manager.run(tiers=("manual",))
        except BackupError as error:
            result = {"outcome_category": _category(error.category)}
        except Exception:
            result = {"outcome_category": "failed"}
        await _private_message(interaction, format_status(result))

    async def becoming_backup_status(interaction: discord.Interaction) -> None:
        if _scope(interaction, config) is None:
            await _private_message(interaction, _UNAVAILABLE)
            return
        try:
            result = manager.status()
        except Exception:
            result = {"outcome_category": "unavailable"}
        await _private_message(interaction, format_status(result))

    commands = tuple(
        app_commands.command(name=callback.__name__, description=description)(
            app_commands.allowed_contexts(
                guilds=config.guild_id is not None, dms=config.guild_id is None, private_channels=False,
            )(callback)
        )
        for callback, description in (
            (becoming_backup_now, "Create and verify one encrypted off-volume Room backup."),
            (becoming_backup_status, "Show bounded private backup operation metadata."),
        )
    )
    registered = BackupCommands(becoming_backup_now, becoming_backup_status, commands)
    guild = discord.Object(id=int(config.guild_id)) if config.guild_id is not None else None
    added = []
    try:
        for command in commands:
            bot.tree.add_command(command, guild=guild)
            added.append(command)
    except Exception:
        for command in added:
            bot.tree.remove_command(command.name, guild=guild)
        raise
    bot._room_backup_registration = (config, manager, registered)
    return registered


async def sync_commands(bot: Any, registered: BackupCommands, *, guild_id: str | None) -> list[Any]:
    # Uses the existing name-only upserts, never a bulk sync or deletion.
    return await sync_room_commands(bot, registered, guild_id=guild_id)
