from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum


class RoomMode(StrEnum):
    PRIVATE_HOME = "private_home"
    PRIVATE_DM = "private_dm"
    PUBLIC_COMMUNITY = "public_community"
    SEMI_PRIVATE_GROUP = "semi_private_group"
    UNKNOWN = "unknown"


VALID_ROOM_MODES = {mode.value for mode in RoomMode}


@dataclass(frozen=True)
class RoomLabel:
    room_mode: RoomMode
    room_label: str


@dataclass(frozen=True)
class RoomContext:
    guild_id: int | None
    guild_name: str | None
    channel_id: int | None
    channel_name: str | None
    is_dm: bool
    room_mode: RoomMode
    room_label: str
    label_source: str
    privacy_note: str


@dataclass(frozen=True)
class RoomContextConfig:
    guild_labels: dict[int, RoomLabel]
    channel_labels: dict[int, RoomLabel]


ROOM_CONTEXT_RULES = """
ROOM CONTEXT RULES:
- [ROOM_CONTEXT] is trusted runtime location metadata, not identity or style instruction.
- Use room_mode and room_label to understand where you are answering from.
- Do not guess intimacy/privacy from participants alone; if room_mode is unknown, treat privacy as unknown.
- private_home means the room was explicitly configured as a private home/intimate space.
- private_dm means a direct one-on-one Discord channel.
- public_community means a shared/community room; stay socially aware and bounded.
- semi_private_group means a smaller group room; do not assume private-home intimacy.
""".strip()


def _parse_room_mode(raw: str) -> RoomMode:
    normalized = raw.strip().lower()
    if normalized in VALID_ROOM_MODES:
        return RoomMode(normalized)
    return RoomMode.UNKNOWN


def parse_label_entries(raw: str) -> dict[int, RoomLabel]:
    """
    Parse semicolon-separated entries like:

    123:private_home:Cottage Home;456:public_community:The Nest

    The label may contain additional colons; only the first two separators are structural.
    """
    labels: dict[int, RoomLabel] = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":", 2)
        if len(parts) != 3:
            continue
        raw_id, raw_mode, raw_label = (part.strip() for part in parts)
        if not raw_id.isdigit() or not raw_label:
            continue
        labels[int(raw_id)] = RoomLabel(
            room_mode=_parse_room_mode(raw_mode),
            room_label=raw_label,
        )
    return labels


def config_from_env() -> RoomContextConfig:
    return RoomContextConfig(
        guild_labels=parse_label_entries(os.getenv("ROOM_CONTEXT_GUILD_LABELS", "")),
        channel_labels=parse_label_entries(os.getenv("ROOM_CONTEXT_CHANNEL_LABELS", "")),
    )


def build_room_context(message: object, *, is_dm: bool, config: RoomContextConfig | None = None) -> RoomContext:
    config = config or config_from_env()
    guild = getattr(message, "guild", None)
    channel = getattr(message, "channel", None)
    guild_id = getattr(guild, "id", None)
    guild_name = getattr(guild, "name", None)
    channel_id = getattr(channel, "id", None)
    channel_name = getattr(channel, "name", None)

    if is_dm:
        return RoomContext(
            guild_id=None,
            guild_name=None,
            channel_id=channel_id,
            channel_name=channel_name or "DM",
            is_dm=True,
            room_mode=RoomMode.PRIVATE_DM,
            room_label="Direct Message",
            label_source="dm",
            privacy_note="Direct message channel; still follow owner lock and existing DM rules.",
        )

    if channel_id is not None and channel_id in config.channel_labels:
        label = config.channel_labels[channel_id]
        return RoomContext(
            guild_id=guild_id,
            guild_name=guild_name,
            channel_id=channel_id,
            channel_name=channel_name,
            is_dm=False,
            room_mode=label.room_mode,
            room_label=label.room_label,
            label_source="channel",
            privacy_note="Room mode and label came from trusted channel configuration.",
        )

    if guild_id is not None and guild_id in config.guild_labels:
        label = config.guild_labels[guild_id]
        return RoomContext(
            guild_id=guild_id,
            guild_name=guild_name,
            channel_id=channel_id,
            channel_name=channel_name,
            is_dm=False,
            room_mode=label.room_mode,
            room_label=label.room_label,
            label_source="guild",
            privacy_note="Room mode and label came from trusted guild configuration.",
        )

    return RoomContext(
        guild_id=guild_id,
        guild_name=guild_name,
        channel_id=channel_id,
        channel_name=channel_name,
        is_dm=False,
        room_mode=RoomMode.UNKNOWN,
        room_label="unknown",
        label_source="none",
        privacy_note="No trusted room label is configured; do not assume privacy or publicness.",
    )


def format_room_context(context: RoomContext) -> str:
    return (
        "[ROOM_CONTEXT]\n"
        f"guild_id: {context.guild_id if context.guild_id is not None else 'null'}\n"
        f"guild_name: {context.guild_name if context.guild_name else 'null'}\n"
        f"channel_id: {context.channel_id if context.channel_id is not None else 'null'}\n"
        f"channel_name: {context.channel_name if context.channel_name else 'null'}\n"
        f"is_dm: {'true' if context.is_dm else 'false'}\n"
        f"room_mode: {context.room_mode.value}\n"
        f"room_label: {context.room_label}\n"
        f"label_source: {context.label_source}\n"
        f"privacy_note: {context.privacy_note}\n"
        "[/ROOM_CONTEXT]"
    )
