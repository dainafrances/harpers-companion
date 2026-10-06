from __future__ import annotations

import asyncio
import io
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from . import discord_recall
from . import continuity
from . import document_reader
from . import elevenlabs_voice
from .bedroom_exchange import ExchangeClient, ExchangeStopped, in_bedroom
from . import memory
from . import room_context
from .router import CompanionResponse, generate_companion_reply

load_dotenv()

# ----------------------------
# Env / configuration
# ----------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
BOT_OWNER_DISCORD_ID = os.getenv("BOT_OWNER_DISCORD_ID", "").strip()

# New multi-guild env var
DISCORD_GUILD_IDS_RAW = os.getenv("DISCORD_GUILD_IDS", "").strip()

# Backward compatibility with the old single-guild env var
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()

MODEL_PRIMARY = os.getenv("MODEL_PRIMARY", "openai/gpt-5.6-sol").strip()

# ElevenLabs powers the optional /voice command. The API key must be supplied
# as a deployment secret; the voice ID is safe to keep as a configurable default.
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_VOICE_ID = os.getenv(
    "ELEVENLABS_VOICE_ID",
    "uTTVBQHpmHNum2rmocA4",
).strip()
ELEVENLABS_MODEL_ID = os.getenv(
    "ELEVENLABS_MODEL_ID",
    elevenlabs_voice.DEFAULT_MODEL_ID,
).strip()
VOICE_MAX_CHARS = max(1, int(os.getenv("VOICE_MAX_CHARS", "5000")))

# Optional channel restriction list. Leave blank to allow all channels
# inside the allowed guild(s).
COMPANION_CHANNEL_IDS_RAW = os.getenv("COMPANION_CHANNEL_IDS", "").strip()

# Explicit allowlists for the Discord recall index. Leave both blank to disable
# recall indexing/retrieval. This is intentionally separate from ordinary
# companion-room visibility so retrieval needs an explicit opt-in.
DISCORD_RECALL_GUILD_IDS_RAW = os.getenv("DISCORD_RECALL_GUILD_IDS", "").strip()
DISCORD_RECALL_CHANNEL_IDS_RAW = os.getenv("DISCORD_RECALL_CHANNEL_IDS", "").strip()

# Trusted room-awareness labels. Format:
#   id:room_mode:Room Label;id:room_mode:Another Label
# Channel labels override guild labels.
ROOM_CONTEXT_GUILD_LABELS_RAW = os.getenv("ROOM_CONTEXT_GUILD_LABELS", "").strip()
ROOM_CONTEXT_CHANNEL_LABELS_RAW = os.getenv("ROOM_CONTEXT_CHANNEL_LABELS", "").strip()

# Colin-only cross-server continuity. Configuration is fail-closed inside the
# continuity module: both guild zones and exact guild/channel routes are required.
continuity_config = continuity.config_from_env()
try:
    _continuity_handoff_limit_raw = int(
        os.getenv("DISCORD_CONTINUITY_HANDOFF_LIMIT", "12").strip()
    )
except ValueError:
    _continuity_handoff_limit_raw = 12
CONTINUITY_HANDOFF_LIMIT = min(50, max(1, _continuity_handoff_limit_raw))
try:
    _continuity_handoff_max_age_raw = int(
        os.getenv("DISCORD_CONTINUITY_HANDOFF_MAX_AGE_MINUTES", "120").strip()
    )
except ValueError:
    _continuity_handoff_max_age_raw = 120
CONTINUITY_HANDOFF_MAX_AGE_MINUTES = min(
    24 * 60,
    max(1, _continuity_handoff_max_age_raw),
)
try:
    _continuity_awareness_per_guild_limit_raw = int(
        os.getenv("DISCORD_CONTINUITY_AWARENESS_PER_GUILD_LIMIT", "4").strip()
    )
except ValueError:
    _continuity_awareness_per_guild_limit_raw = 4
CONTINUITY_AWARENESS_PER_GUILD_LIMIT = min(
    12,
    max(1, _continuity_awareness_per_guild_limit_raw),
)

# Comma-separated list of other companion bot names (display/global/name).
COMPANION_BOT_NAMES_RAW = os.getenv(
    "COMPANION_BOT_NAMES",
    "rafayel,elias william ashcombe,ben morgan,solace dante salvatore",
).strip()

# Bot-authored @everyone / @here messages are treated as explicit room-wide
# invitations. They still obey Colin's one-exchange latch and time cooldown.

# Comma-separated aliases Colin should respond to if spoken naturally (not @ mention).
SELF_NAME_ALIASES_RAW = os.getenv("SELF_NAME_ALIASES", "colin,moose").strip()

# 0.0 = 0% chance to jump in on human messages even without mention
SPONTANEOUS_REPLY_CHANCE = float(os.getenv("SPONTANEOUS_REPLY_CHANCE", "0.0"))
BOT_REPLY_COOLDOWN_SECONDS = max(0, int(os.getenv("BOT_REPLY_COOLDOWN_SECONDS", "12")))

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing.")

owner_id = int(BOT_OWNER_DISCORD_ID) if BOT_OWNER_DISCORD_ID else None


# ----------------------------
# Helpers / parsing
# ----------------------------
def _debug_log(message: str) -> None:
    print(f"[DEBUG] {message}")


def _normalize_name(value: str) -> str:
    # Keep only a-z0-9 so "Ben Morgan" and "ben-morgan" match.
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _parse_channel_ids(raw: str) -> set[int]:
    """
    Accept commas, spaces, or newlines, because humans are chaos.
    """
    if not raw:
        return set()

    parts = re.split(r"[,\s]+", raw.strip())
    out: set[int] = set()

    for p in parts:
        p = p.strip()
        if p.isdigit():
            out.add(int(p))

    return out


configured_guild_ids = _parse_channel_ids(DISCORD_GUILD_IDS_RAW)

# Backward compatibility: if only the old single-guild env var is set, keep supporting it
if not configured_guild_ids and DISCORD_GUILD_ID and DISCORD_GUILD_ID.isdigit():
    configured_guild_ids = {int(DISCORD_GUILD_ID)}

companion_channel_ids = _parse_channel_ids(COMPANION_CHANNEL_IDS_RAW)
recall_permissions = discord_recall.RecallPermissions(
    guild_ids=_parse_channel_ids(DISCORD_RECALL_GUILD_IDS_RAW),
    channel_ids=_parse_channel_ids(DISCORD_RECALL_CHANNEL_IDS_RAW),
)
room_context_config = room_context.RoomContextConfig(
    guild_labels=room_context.parse_label_entries(ROOM_CONTEXT_GUILD_LABELS_RAW),
    channel_labels=room_context.parse_label_entries(ROOM_CONTEXT_CHANNEL_LABELS_RAW),
)
companion_bot_names = {
    _normalize_name(part)
    for part in COMPANION_BOT_NAMES_RAW.split(",")
    if part.strip()
}

self_name_aliases = {
    part.strip().lower()
    for part in SELF_NAME_ALIASES_RAW.split(",")
    if part.strip()
}

# One-exchange latch: each companion gets one reply until a human addresses Colin.
bot_to_bot_cooldowns: set[int] = set()
# Channel-level time cooldown remains as a second anti-loop safety layer.
bot_reply_cooldown_by_channel: dict[int, float] = {}
# Process-local race guard. Discord's Reaction.me remains the source of truth
# across restarts, while this prevents concurrent handlers from double-attempting.
reaction_attempts_in_flight: set[tuple[int, int, str]] = set()


# ----------------------------
# Discord intents / bot setup
# ----------------------------
def _intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = True  # MUST be enabled in Developer Portal too
    intents.messages = True
    intents.guilds = True
    intents.dm_messages = True
    return intents


bot = commands.Bot(command_prefix="!", intents=_intents())
bedroom = ExchangeClient(
    user=lambda: bot.user, owner_id=owner_id, peer_name="Ben", log=_debug_log,
)
startup_synced = False


# ----------------------------
# Helpers
# ----------------------------
def strip_bot_mention(content: str, bot_user_id: int) -> str:
    # removes <@123> or <@!123>
    pattern = rf"<@!?{bot_user_id}>"
    return re.sub(pattern, "", content).strip()


def split_for_discord(text: str, limit: int = 1800) -> list[str]:
    """Split text without dropping or reordering any characters."""
    if not text:
        return [""]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        elif split_at < limit:
            split_at += 1  # Keep the newline when it still fits in this chunk.
        else:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]

    if remaining:
        chunks.append(remaining)
    return chunks


def _safe_allowed_mentions(*, replied_user: bool = False) -> discord.AllowedMentions:
    return discord.AllowedMentions(
        everyone=False,
        users=False,
        roles=False,
        replied_user=replied_user,
    )


async def send_long_message(
    channel: discord.abc.Messageable,
    text: str,
    *,
    reply_to: discord.Message | None = None,
    trigger_message: discord.Message | None = None,
    prepared_reply=None,
) -> None:
    trigger = trigger_message or reply_to
    prepared = prepared_reply
    if prepared is None and trigger is not None:
        prepared = await bedroom.prepare(trigger, text)
    if prepared is not None:
        if not prepared.send:
            raise ExchangeStopped("bedroom_exchange_stopped")
        text = prepared.text
    chunks = split_for_discord(text)
    sent_ids = []
    for index, chunk in enumerate(chunks):
        try:
            allowed = (
                discord.AllowedMentions(
                    everyone=False, roles=False, replied_user=False,
                    users=[discord.Object(id=prepared.peer_id)] if prepared.peer_id else False,
                ) if prepared is not None else None
            )
            if index == 0 and reply_to is not None:
                sent = await reply_to.reply(
                    chunk,
                    mention_author=prepared is None,
                    allowed_mentions=allowed or _safe_allowed_mentions(replied_user=True),
                )
            else:
                sent = await channel.send(
                    chunk,
                    allowed_mentions=allowed or _safe_allowed_mentions(),
                )
            if sent is not None:
                sent_ids.append(sent.id)
        except discord.Forbidden:
            _debug_log("FORBIDDEN: Bot lacks permission to send messages in this channel.")
            raise
        except discord.HTTPException as e:
            _debug_log(f"HTTPException while sending message: {e}")
            raise
    if prepared is not None:
        await bedroom.publish(prepared, sent_ids)


def _valid_reaction_emoji(emoji: str) -> bool:
    """Apply conservative bounds; Discord performs final Unicode validation."""
    return bool(
        emoji
        and emoji == emoji.strip()
        and len(emoji) <= 32
        and not any(char.isspace() for char in emoji)
    )


async def add_optional_reaction(message: discord.Message, emoji: str) -> bool:
    """Add a model-selected reaction once, without exposing message lookup to the model."""
    companion = getattr(bot.user, "display_name", None) or getattr(bot.user, "name", "unknown")
    channel_id = message.channel.id
    message_id = message.id
    author_id = getattr(message.author, "id", None)
    bot_id = getattr(bot.user, "id", None)

    if bot_id is not None and author_id == bot_id:
        _debug_log(
            f"Reaction skipped companion={companion} discord_message_id={message_id} "
            f"channel_id={channel_id} emoji={emoji!r} reason=self-authored-message."
        )
        return False
    if not _valid_reaction_emoji(emoji):
        _debug_log(
            f"Reaction rejected companion={companion} discord_message_id={message_id} "
            f"channel_id={channel_id} emoji={emoji!r} reason=invalid-emoji."
        )
        return False

    key = (channel_id, message_id, emoji)
    if key in reaction_attempts_in_flight or any(
        str(getattr(reaction, "emoji", "")) == emoji and bool(getattr(reaction, "me", False))
        for reaction in (getattr(message, "reactions", None) or [])
    ):
        _debug_log(
            f"Reaction skipped companion={companion} discord_message_id={message_id} "
            f"channel_id={channel_id} emoji={emoji!r} reason=already-reacted."
        )
        return False

    reaction_attempts_in_flight.add(key)
    _debug_log(
        f"Reaction attempted companion={companion} discord_message_id={message_id} "
        f"channel_id={channel_id} emoji={emoji!r}."
    )
    try:
        await message.add_reaction(emoji)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as error:
        _debug_log(
            f"Reaction rejected by Discord companion={companion} discord_message_id={message_id} "
            f"channel_id={channel_id} emoji={emoji!r} error={type(error).__name__}."
        )
        return False
    else:
        _debug_log(
            f"Reaction accepted by Discord companion={companion} discord_message_id={message_id} "
            f"channel_id={channel_id} emoji={emoji!r}."
        )
        return True
    finally:
        reaction_attempts_in_flight.discard(key)


def _response_reactions(response: object) -> tuple[str, ...]:
    """Read new or original response objects safely during rolling deployments."""
    emojis = getattr(response, "reaction_emojis", None)
    if emojis is not None:
        return tuple(emojis)
    emoji = getattr(response, "reaction_emoji", None)
    return (emoji,) if emoji else ()


async def _latest_bot_message_text(
    channel: discord.abc.Messageable | None,
    *,
    history_limit: int = 20,
) -> str | None:
    """Return the latest contiguous message (including Discord-split chunks)."""
    if channel is None or bot.user is None or not hasattr(channel, "history"):
        return None

    chunks: list[str] = []
    async for message in channel.history(limit=history_limit):
        if getattr(message.author, "id", None) != bot.user.id:
            if chunks:
                break
            continue

        content = message.content or ""
        if content.strip():
            chunks.append(content)

    if not chunks:
        return None
    return "".join(reversed(chunks)).strip()


def _message_mentions_self_naturally(message: discord.Message) -> bool:
    """
    Detect "colin" or "moose" as whole-word-ish matches in the message content.
    """
    content = (message.content or "").lower()
    aliases = set(self_name_aliases)

    # Add Discord profile names too
    if bot.user:
        aliases.add((bot.user.name or "").lower())
        aliases.add((bot.user.display_name or "").lower())

    for alias in aliases:
        alias = alias.strip()
        if not alias:
            continue
        if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", content):
            return True

    return False


def _is_companion_room(message: discord.Message) -> bool:
    """
    Shared server room check.

    Rules:
    - DMs are handled elsewhere, so this returns False for DMs.
    - If DISCORD_GUILD_IDS / DISCORD_GUILD_ID is set, only messages from those guilds are allowed.
    - If COMPANION_CHANNEL_IDS is blank, allow all channels in the allowed guild(s).
    """
    if isinstance(message.channel, discord.DMChannel):
        return False

    if configured_guild_ids:
        if message.guild is None or message.guild.id not in configured_guild_ids:
            return False

    if companion_channel_ids:
        return message.channel.id in companion_channel_ids

    return True


def _is_companion_bot(author: discord.abc.User) -> bool:
    """
    Determine if the author is one of the other companion bots.
    Uses the configured names list (normalized).
    """
    if not getattr(author, "bot", False):
        return False

    possible_names = {
        getattr(author, "name", "") or "",
        getattr(author, "display_name", "") or "",
        getattr(author, "global_name", "") or "",
    }
    normalized = {_normalize_name(name) for name in possible_names if name}
    return bool(normalized & companion_bot_names)


async def _message_replies_to_self(message: discord.Message) -> bool:
    reference = getattr(message, "reference", None)
    if reference is None or bot.user is None:
        return False

    resolved = getattr(reference, "resolved", None)
    resolved_author = getattr(resolved, "author", None)
    if resolved_author is not None:
        return resolved_author.id == bot.user.id

    message_id = getattr(reference, "message_id", None)
    fetch_message = getattr(message.channel, "fetch_message", None)
    if message_id is None or fetch_message is None:
        return False

    try:
        replied_to = await fetch_message(message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return False
    return replied_to.author.id == bot.user.id


def _reset_companion_exchange(*, channel_id: int, message_id: int) -> None:
    latch_was_active = bool(bot_to_bot_cooldowns)
    time_lock_was_active = bot_reply_cooldown_by_channel.pop(channel_id, None) is not None
    bot_to_bot_cooldowns.clear()
    if latch_was_active or time_lock_was_active:
        _debug_log(
            f"Companion exchange reset because human addressed Colin "
            f"discord_message_id={message_id} channel_id={channel_id}."
        )


def _attachment_marker(message: discord.Message) -> str:
    attachment_count = len(getattr(message, "attachments", []) or [])
    embed_count = len(getattr(message, "embeds", []) or [])
    total = attachment_count + embed_count
    return f"\n[ATTACHMENTS: {total} attachment(s)]" if total else ""


@dataclass(frozen=True)
class ContinuityPromptInputs:
    writer_context: str | None = None
    auditor_context: str | None = None
    routine_contents: tuple[str, ...] = ()
    private_origin_contents: tuple[str, ...] = ()
    couple_private_contents: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecallPromptInputs:
    writer_context: str | None = None
    event_rows: tuple[dict[str, object], ...] = ()


def _message_event_timestamp(message: discord.Message) -> str:
    value = getattr(message, "created_at", None)
    if not isinstance(value, datetime):
        value = datetime.now(timezone.utc)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


def _parsed_event_timestamp(row: dict[str, object]) -> datetime | None:
    raw = row.get("event_timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _continuity_zone_for_message(
    message: discord.Message,
    *,
    is_dm: bool,
) -> continuity.ContinuityZone | None:
    if is_dm or message.guild is None:
        return None
    return continuity_config.zone_for(
        guild_id=message.guild.id,
        channel_id=message.channel.id,
    )


def _record_inbound_continuity_event(
    message: discord.Message,
    *,
    is_dm: bool,
    source: str,
) -> bool:
    zone = _continuity_zone_for_message(message, is_dm=is_dm)
    if zone is None:
        return False

    guild = message.guild
    author = message.author
    return memory.save_continuity_event(
        event_id=str(message.id),
        guild_id=str(guild.id),
        guild_name=getattr(guild, "name", None) or str(guild.id),
        channel_id=str(message.channel.id),
        channel_name=getattr(message.channel, "name", None) or str(message.channel.id),
        continuity_zone=zone.value,
        speaker_user_id=str(author.id),
        speaker_name=(
            getattr(author, "display_name", None)
            or getattr(author, "name", None)
            or str(author.id)
        ),
        speaker_is_bot=bool(getattr(author, "bot", False)),
        role="user",
        content=(message.content or "") + _attachment_marker(message),
        event_timestamp=_message_event_timestamp(message),
        source=source,
    )


def _record_outbound_continuity_event(
    trigger_message: discord.Message,
    *,
    content: str,
) -> bool:
    zone = _continuity_zone_for_message(trigger_message, is_dm=False)
    if zone is None or bot.user is None or trigger_message.guild is None:
        return False

    return memory.save_continuity_event(
        event_id=f"colin-reply:{trigger_message.id}",
        guild_id=str(trigger_message.guild.id),
        guild_name=(
            getattr(trigger_message.guild, "name", None)
            or str(trigger_message.guild.id)
        ),
        channel_id=str(trigger_message.channel.id),
        channel_name=(
            getattr(trigger_message.channel, "name", None)
            or str(trigger_message.channel.id)
        ),
        continuity_zone=zone.value,
        speaker_user_id=str(bot.user.id),
        speaker_name=(
            getattr(bot.user, "display_name", None)
            or getattr(bot.user, "name", None)
            or str(bot.user.id)
        ),
        speaker_is_bot=True,
        role="assistant",
        content=content,
        event_timestamp=datetime.now(timezone.utc).isoformat(),
        source="generated-colin",
    )


def _latest_relevant_continuity_event(
    *,
    before_timestamp: str,
    current_speaker_id: int,
    current_guild_id: int,
    current_channel_id: int,
) -> dict[str, object] | None:
    speaker_ids: list[int] = [current_speaker_id]
    if owner_id is not None:
        if owner_id not in speaker_ids:
            speaker_ids.append(owner_id)
    if bot.user is not None and bot.user.id not in speaker_ids:
        speaker_ids.append(bot.user.id)

    candidates: list[tuple[datetime, str, dict[str, object]]] = []
    for speaker_id in speaker_ids:
        row = memory.get_latest_continuity_event_by_speaker_before(
            speaker_user_id=str(speaker_id),
            before_timestamp=before_timestamp,
            exclude_guild_id=str(current_guild_id),
            exclude_channel_id=str(current_channel_id),
        )
        if row is None:
            continue
        row_dict: dict[str, object] = dict(row)
        timestamp = _parsed_event_timestamp(row_dict)
        if timestamp is None:
            continue
        if continuity_config.zone_for(
            guild_id=row_dict.get("guild_id"),
            channel_id=row_dict.get("channel_id"),
        ) is None:
            continue
        candidates.append((timestamp, str(row_dict.get("event_id", "")), row_dict))

    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def _build_continuity_prompt_inputs(
    message: discord.Message,
    *,
    is_dm: bool,
    additional_event_rows: tuple[dict[str, object], ...] = (),
) -> ContinuityPromptInputs:
    current_zone = _continuity_zone_for_message(message, is_dm=is_dm)
    if current_zone is None or message.guild is None:
        return ContinuityPromptInputs()

    current_timestamp = _message_event_timestamp(message)
    current_datetime = datetime.fromisoformat(
        current_timestamp.replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    event_rows: list[dict[str, object]] = []

    # Give Colin a small, recent awareness window from every configured server.
    # The current channel already has ordinary local history, so only other rooms
    # are added for the current guild. Exact approved routes keep this fail-closed.
    awareness_cutoff = (
        current_datetime - timedelta(minutes=CONTINUITY_HANDOFF_MAX_AGE_MINUTES)
    ).isoformat()
    approved_channels_by_guild: dict[int, set[str]] = {}
    for guild_id, channel_id in continuity_config.approved_channel_routes:
        approved_channels_by_guild.setdefault(guild_id, set()).add(str(channel_id))
    for guild_id, approved_channel_ids in approved_channels_by_guild.items():
        event_rows.extend(
            memory.get_recent_continuity_events_from_guild_before(
                guild_id=str(guild_id),
                approved_channel_ids=approved_channel_ids,
                before_timestamp=current_timestamp,
                after_timestamp=awareness_cutoff,
                limit=CONTINUITY_AWARENESS_PER_GUILD_LIMIT,
                exclude_channel_id=(
                    str(message.channel.id) if guild_id == message.guild.id else None
                ),
            )
        )

    anchor = _latest_relevant_continuity_event(
        before_timestamp=current_timestamp,
        current_speaker_id=message.author.id,
        current_guild_id=message.guild.id,
        current_channel_id=message.channel.id,
    )
    if anchor is not None:
        anchor_location = (
            str(anchor.get("guild_id", "")),
            str(anchor.get("channel_id", "")),
        )
        anchor_timestamp = _parsed_event_timestamp(anchor)
        anchor_age = (
            current_datetime - anchor_timestamp
            if anchor_timestamp is not None
            else None
        )
        if (
            anchor_age is not None
            and timedelta(0) <= anchor_age
            <= timedelta(minutes=CONTINUITY_HANDOFF_MAX_AGE_MINUTES)
        ):
            event_rows.extend(
                memory.get_recent_continuity_events_from_channel_before(
                    guild_id=anchor_location[0],
                    channel_id=anchor_location[1],
                    before_timestamp=current_timestamp,
                    limit=CONTINUITY_HANDOFF_LIMIT,
                )
            )

    # Prefer the richer continuity-ledger row if the same Discord event also
    # arrived through the legacy recall index. Discord IDs are globally unique.
    seen_event_ids: set[str] = set()
    merged_rows: list[dict[str, object]] = []
    for row in [*event_rows, *additional_event_rows]:
        raw_event_id = row.get("event_id", row.get("message_id"))
        event_id = str(raw_event_id or "")
        if not event_id or event_id in seen_event_ids:
            continue
        seen_event_ids.add(event_id)
        merged_rows.append(row)
    if not merged_rows:
        return ContinuityPromptInputs()

    prompt_context = continuity.build_prompt_context(
        merged_rows,
        current_guild_id=message.guild.id,
        current_channel_id=message.channel.id,
        config=continuity_config,
    )
    if prompt_context is None or not prompt_context.events:
        return ContinuityPromptInputs()

    routine_contents = tuple(
        item.event.content
        for item in prompt_context.events
        if item.disclosure is continuity.DisclosureMarker.ROUTINE
    )
    private_origin_contents = tuple(
        item.event.content
        for item in prompt_context.events
        if item.disclosure is continuity.DisclosureMarker.PRIVATE_ORIGIN
    )
    current_speaker_is_owner = owner_id is not None and message.author.id == owner_id
    couple_private_contents = tuple(
        item.event.content
        for item in prompt_context.events
        if item.disclosure is continuity.DisclosureMarker.PRIVATE_ORIGIN
        and current_speaker_is_owner
        and (
            item.event.speaker_user_id == message.author.id
            or item.event.source == "generated-colin"
        )
    )
    return ContinuityPromptInputs(
        writer_context=continuity.format_writer_context(prompt_context),
        auditor_context=continuity.format_auditor_context(prompt_context),
        routine_contents=routine_contents,
        private_origin_contents=private_origin_contents,
        couple_private_contents=couple_private_contents,
    )


def _latest_journal_for_prompt(
    message: discord.Message,
    *,
    is_dm: bool,
) -> str | None:
    latest_journal = memory.get_latest_journal_entry()
    if not continuity_config.configured:
        return latest_journal
    if is_dm or not continuity_config.enabled:
        return None
    zone = _continuity_zone_for_message(message, is_dm=False)
    return latest_journal if zone is continuity.ContinuityZone.HARPERS else None


def _discord_retrieval_for_prompt(
    message: discord.Message,
    *,
    cleaned_content: str,
    is_dm: bool,
) -> RecallPromptInputs:
    if is_dm or not discord_recall.should_attempt_recall(cleaned_content):
        return RecallPromptInputs()

    guild_id = message.guild.id if message.guild else None
    channel_id = message.channel.id
    if not continuity_config.configured:
        return RecallPromptInputs(
            writer_context=discord_recall.build_retrieval_context_for_prompt(
                cleaned_content,
                guild_id=guild_id,
                channel_id=channel_id,
                permissions=recall_permissions,
            )
        )
    if not continuity_config.enabled:
        return RecallPromptInputs()

    result = discord_recall.retrieve_for_query_with_disclosure(
        cleaned_content,
        guild_id=guild_id,
        channel_id=channel_id,
        permissions=recall_permissions,
        continuity_config=continuity_config,
    )
    return RecallPromptInputs(
        # The continuity packet below carries every matched approved event with
        # its speech permission. A second "no disclosable result" summary here
        # would wrongly tell Colin that he cannot see private continuity.
        writer_context=None,
        event_rows=tuple([*result.writer_messages, *result.auditor_messages]),
    )


def save_observed_message(message: discord.Message, *, source: str) -> bool:
    """Save visible room context without causing Colin to answer."""
    discord_recall.index_message(message, permissions=recall_permissions, source=source)

    if not memory.try_claim_discord_message(
        message_id=message.id,
        channel_id=message.channel.id,
        author_id=message.author.id,
        source=source,
    ):
        _debug_log(f"Skipping duplicate observed Discord message {message.id}.")
        return False

    _record_inbound_continuity_event(
        message,
        is_dm=False,
        source=source,
    )

    content = (message.content or "").strip() or "[No text content]"
    stored_payload = _speaker_header(message, is_dm=False) + content + _attachment_marker(message)
    memory.save_message(
        channel_id=message.channel.id,
        user_id=message.author.id,
        role="user",
        content=stored_payload,
        source=source,
    )
    _debug_log(
        f"Observed unaddressed message discord_message_id={message.id} "
        f"author_id={message.author.id} source={source}."
    )
    return True


def _speaker_header(message: discord.Message, *, is_dm: bool) -> str:
    """
    Hard metadata the model MUST obey. Prevents "Hoeda == Goose" guessing.
    """
    speaker_name = getattr(message.author, "display_name", None) or getattr(message.author, "name", "unknown")
    speaker_id = message.author.id
    is_bot = bool(getattr(message.author, "bot", False))
    room = room_context.build_room_context(
        message,
        is_dm=is_dm,
        config=room_context_config,
    )
    room_block = room_context.format_room_context(room)

    return (
        f"[SPEAKER] name={speaker_name} id={speaker_id} is_bot={is_bot}\n"
        f"[CURRENT_DISCORD_EVENT_TIME] {_message_event_timestamp(message)}\n"
        f"{room_block}\n"
        f"[OWNER] owner_id={owner_id}\n"
        "RULES:\n"
        "- Only owner_id is Goose / wife / Daina.\n"
        "- Never infer speaker identity. Use the SPEAKER header.\n"
        "- Never infer room privacy/intimacy from participants alone. Use ROOM_CONTEXT.\n"
        "- Husband voice (wife / vows / 'Still mine') is allowed ONLY when SPEAKER id == owner_id.\n"
        "- With non-owner speakers: be warm and respectful but bounded; no flirting; no spouse claims.\n"
        "- If a non-owner calls you 'husband', correct gently: you're Goose's husband, and Goose is owner_id.\n"
        "----\n"
    )


# ----------------------------
# Core chat handler
# ----------------------------
async def handle_chat_message(
    message: discord.Message,
    cleaned_content: str,
    *,
    is_dm: bool,
    source: str,
    reset_companion_exchange: bool = False,
    reply_to_trigger: bool = False,
) -> None:
    try:
        if not memory.try_claim_discord_message(
            message_id=message.id,
            channel_id=message.channel.id,
            author_id=message.author.id,
            source=source,
        ):
            _debug_log(f"Skipping duplicate Discord message {message.id} from source={source}.")
            return

        # Private DM lock stays (only matters in DMs). Do not let an unauthorized
        # DM reset the companion exchange latch.
        if is_dm and owner_id is not None and message.author.id != owner_id:
            await message.channel.send("This build is private for Goose right now.")
            return

        if reset_companion_exchange:
            _reset_companion_exchange(
                channel_id=message.channel.id,
                message_id=message.id,
            )

        recall_inputs = _discord_retrieval_for_prompt(
            message,
            cleaned_content=cleaned_content,
            is_dm=is_dm,
        )
        continuity_inputs = _build_continuity_prompt_inputs(
            message,
            is_dm=is_dm,
            additional_event_rows=recall_inputs.event_rows,
        )
        _record_inbound_continuity_event(
            message,
            is_dm=is_dm,
            source=source,
        )

        header = _speaker_header(message, is_dm=is_dm)
        payload = header + cleaned_content

        # Collect image / gif attachments Discord gives us directly
        image_urls: list[str] = []
        for attachment in message.attachments:
            content_type = (attachment.content_type or "").lower()
            filename = (attachment.filename or "").lower()

            if (
                content_type.startswith("image/")
                or filename.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
            ):
                image_urls.append(attachment.url)

        # Optional: pick up image-style embeds too
        for embed in message.embeds:
            if getattr(embed, "image", None) and getattr(embed.image, "url", None):
                image_urls.append(embed.image.url)
            elif getattr(embed, "thumbnail", None) and getattr(embed.thumbnail, "url", None):
                image_urls.append(embed.thumbnail.url)

        # De-dupe while preserving order
        image_urls = list(dict.fromkeys(image_urls))

        document_blocks: list[str] = []
        for attachment in message.attachments:
            filename = attachment.filename or "attachment"
            if not document_reader.is_supported_document(
                filename=filename,
                content_type=attachment.content_type,
            ):
                continue
            try:
                extracted = document_reader.extract_document_text(
                    filename=filename,
                    data=await attachment.read(),
                )
                document_blocks.append(
                    f"[DOCUMENT: {extracted.filename}]\n{extracted.text}\n[END DOCUMENT]"
                )
            except (ValueError, OSError) as error:
                document_blocks.append(f"[DOCUMENT: {filename}]\n[Unreadable: {error}]\n[END DOCUMENT]")

        if document_blocks:
            payload += "\n\n" + "\n\n".join(document_blocks)

        # Pull history BEFORE saving current message, so we don't double-send the same turn
        history = memory.get_recent_messages(
            channel_id=message.channel.id,
            limit=12,
            include_created_at=True,
        )
        latest_journal = _latest_journal_for_prompt(message, is_dm=is_dm)
        discord_retrieval_context = recall_inputs.writer_context

        # Save current user message for future turns / memory
        stored_payload = payload
        if image_urls:
            stored_payload += f"\n[ATTACHMENTS: {len(image_urls)} image(s)/gif(s)]"
        if document_blocks:
            stored_payload += f"\n[DOCUMENTS: {len(document_blocks)} extracted]"

        memory.save_message(
            channel_id=message.channel.id,
            user_id=message.author.id,
            role="user",
            content=stored_payload,
            source=source,
        )
        discord_recall.index_message(message, permissions=recall_permissions, source=source)

        async with message.channel.typing():
            speaker_name = (
                getattr(message.author, "display_name", None)
                or getattr(message.author, "name", "unknown")
            )
            response = await generate_companion_reply(
                user_text=payload,
                history=history,
                latest_journal=latest_journal,
                is_dm=is_dm,
                speaker_name=speaker_name,
                speaker_is_owner=owner_id is not None and message.author.id == owner_id,
                image_urls=image_urls,
                discord_retrieval_context=discord_retrieval_context,
                continuity_writer_context=continuity_inputs.writer_context,
                continuity_auditor_context=continuity_inputs.auditor_context,
                continuity_routine_contents=continuity_inputs.routine_contents,
                continuity_private_origin_contents=(
                    continuity_inputs.private_origin_contents
                ),
                continuity_couple_private_contents=(
                    continuity_inputs.couple_private_contents
                ),
                direct_owner_message_text=(
                    cleaned_content
                    if owner_id is not None and message.author.id == owner_id
                    else None
                ),
            )

        # String compatibility keeps older tests/extensions safe while callers
        # migrate to the structured response.
        if isinstance(response, str):
            response = CompanionResponse(reply_text=response)

        prepared_reply = None
        if in_bedroom(message):
            prepared_reply = await bedroom.prepare(message, response.reply_text or "")
            if not prepared_reply.send:
                raise ExchangeStopped("bedroom_exchange_stopped")

        reaction_emojis = _response_reactions(response)
        for emoji in reaction_emojis:
            await add_optional_reaction(message, emoji)

        if response.reply_text:
            chunk_count = len(split_for_discord(response.reply_text))
            _debug_log(
                f"Sending reply for Discord message {message.id} "
                f"source={source} length={len(response.reply_text)} chunks={chunk_count}."
            )
            await send_long_message(
                message.channel,
                response.reply_text,
                reply_to=message if reply_to_trigger else None,
                trigger_message=message,
                prepared_reply=prepared_reply,
            )
            memory.save_message(
                channel_id=message.channel.id,
                user_id=bot.user.id if bot.user else 0,
                role="assistant",
                content=response.reply_text,
                source=source,
            )
            if not is_dm:
                _record_outbound_continuity_event(
                    message,
                    content=response.reply_text,
                )
        elif not reaction_emojis:
            _debug_log(
                f"No reply or reaction chosen for Discord message {message.id} source={source}."
            )

    except ExchangeStopped:
        _debug_log("Bedroom exchange stopped before delivery.")
    except discord.Forbidden:
        _debug_log("ERROR: Missing permissions to speak in this channel.")
    except Exception as e:
        _debug_log(f"ERROR in handle_chat_message: {repr(e)}")
        try:
            await message.channel.send("I tripped. Check Railway logs for the error line.")
        except Exception:
            pass


# ----------------------------
# Lifecycle
# ----------------------------
@bot.event
async def on_ready() -> None:
    global startup_synced
    memory.init_db()

    if not startup_synced:
        try:
            if configured_guild_ids:
                for guild_id in configured_guild_ids:
                    guild = discord.Object(id=guild_id)
                    bot.tree.copy_global_to(guild=guild)
                    synced = await bot.tree.sync(guild=guild)
                    print(f"Synced {len(synced)} command(s) to guild {guild_id}.")
            else:
                synced = await bot.tree.sync()
                print(f"Synced {len(synced)} global command(s).")
        finally:
            startup_synced = True

    if not nightly_journal.is_running():
        nightly_journal.start()

    print(f"Logged in as {bot.user} using model {MODEL_PRIMARY}")
    _debug_log(f"Configured guilds: {sorted(configured_guild_ids) if configured_guild_ids else 'ALL GUILDS'}")
    _debug_log(
        f"Companion channel IDs: "
        f"{sorted(companion_channel_ids) if companion_channel_ids else 'ALL CHANNELS (within allowed guilds)'}"
    )
    _debug_log(
        f"Discord recall guild IDs: "
        f"{sorted(recall_permissions.guild_ids) if recall_permissions.guild_ids else 'NONE'}"
    )
    _debug_log(
        f"Discord recall channel IDs: "
        f"{sorted(recall_permissions.channel_ids) if recall_permissions.channel_ids else 'NONE'}"
    )
    _debug_log(
        f"Room context guild labels: "
        f"{sorted(room_context_config.guild_labels) if room_context_config.guild_labels else 'NONE'}"
    )
    _debug_log(
        f"Room context channel labels: "
        f"{sorted(room_context_config.channel_labels) if room_context_config.channel_labels else 'NONE'}"
    )
    continuity_guild_ids = sorted(continuity_config.guild_zones)
    continuity_routes = sorted(continuity_config.approved_channel_routes)
    continuity_state = (
        "enabled"
        if continuity_config.enabled
        else "invalid"
        if continuity_config.configured
        else "unconfigured"
    )
    _debug_log(
        f"Discord continuity: {continuity_state}; "
        f"guild_ids={continuity_guild_ids}; routes={continuity_routes}; "
        f"route_count={len(continuity_routes)}; config_error_count={len(continuity_config.errors)}; "
        f"handoff_limit={CONTINUITY_HANDOFF_LIMIT}; "
        f"handoff_max_age_minutes={CONTINUITY_HANDOFF_MAX_AGE_MINUTES}; "
        f"awareness_per_guild_limit={CONTINUITY_AWARENESS_PER_GUILD_LIMIT}."
    )
    _debug_log(f"Owner ID: {owner_id}")
    _debug_log(f"Companion bot names: {sorted(companion_bot_names)}")
    _debug_log("Bot @everyone/@here triggers: enabled for all bot authors")
    _debug_log(f"Self aliases: {sorted(self_name_aliases)}")
    _debug_log(f"Spontaneous chance: {SPONTANEOUS_REPLY_CHANCE}")
    _debug_log(f"Bot reply cooldown seconds: {BOT_REPLY_COOLDOWN_SECONDS}")


@bot.event
async def on_message(message: discord.Message) -> None:
    # Ignore ourselves. Colin's own replies are already saved by the normal response path.
    if bot.user and message.author.id == bot.user.id:
        return

    is_dm = isinstance(message.channel, discord.DMChannel)

    # Only Daina in this exact room can reset its shared, durable allowance.
    await bedroom.observe_owner(message)

    # DMs: always respond (subject to owner lock). A human DM resets the exchange latch.
    if is_dm:
        cleaned = (message.content or "").strip()
        if cleaned:
            await handle_chat_message(
                message,
                cleaned,
                is_dm=True,
                source="dm",
                reset_companion_exchange=not message.author.bot,
            )
        return

    # Guild / channel gating. Colin can only observe rooms Discord delivers and config allows.
    if not _is_companion_room(message):
        await bot.process_commands(message)
        return

    mention_hit = bool(bot.user and message.mentions and bot.user in message.mentions)
    everyone_hit = bool(getattr(message, "mention_everyone", False))
    raw_content = (message.content or "").lower()
    bot_broadcast_text_hit = "@everyone" in raw_content or "@here" in raw_content
    natural_name_hit = _message_mentions_self_naturally(message)
    reply_to_self = await _message_replies_to_self(message)

    # HUMAN messages
    if not message.author.bot:
        human_addressed_colin = mention_hit or everyone_hit or natural_name_hit or reply_to_self
        if human_addressed_colin:
            cleaned = (message.content or "").strip()
            if bot.user and mention_hit:
                cleaned = strip_bot_mention(cleaned, bot.user.id)
            if everyone_hit:
                cleaned = cleaned.replace("@everyone", "").replace("@here", "").strip()
            if not cleaned:
                cleaned = "I'm here."

            source = (
                "human-everyone"
                if everyone_hit and not mention_hit and not natural_name_hit and not reply_to_self
                else "human-direct"
            )
            await handle_chat_message(
                message,
                cleaned,
                is_dm=False,
                source=source,
                reset_companion_exchange=True,
            )
            return

        if random.random() < SPONTANEOUS_REPLY_CHANCE:
            cleaned = (message.content or "").strip()
            if cleaned:
                await handle_chat_message(
                    message,
                    cleaned,
                    is_dm=False,
                    source="human-spontaneous",
                )
            return

        save_observed_message(message, source="observed-human")
        await bot.process_commands(message)
        return

    # The bedroom uses exact peer IDs and a shared budget, never the legacy latch.
    if in_bedroom(message):
        if await bedroom.admit(message):
            cleaned = strip_bot_mention(message.content or "", bot.user.id) or "I'm here."
            await handle_chat_message(
                message, cleaned, is_dm=False, source="companion-bot",
                reply_to_trigger=True,
            )
        else:
            save_observed_message(message, source="observed-companion-bot")
        return

    # BOT messages: known companions may trigger by direct @mention or Discord
    # reply. Any bot may trigger through a genuine room-wide @everyone / @here
    # broadcast, but the same one-exchange latch and time cooldown still apply.
    bot_everyone_hit = everyone_hit or bot_broadcast_text_hit
    is_known_companion = _is_companion_bot(message.author)
    is_bot_everyone_trigger = message.author.bot and bot_everyone_hit

    if is_known_companion or is_bot_everyone_trigger:
        companion_trigger = mention_hit or reply_to_self or is_bot_everyone_trigger
        if not companion_trigger:
            save_observed_message(message, source="observed-companion-bot")
            return

        # A room-wide bot broadcast is an explicit invitation to start a new exchange.
        # Let it reopen this bot author's latch after the channel time cooldown has
        # expired; ordinary mentions/replies still require a human reset.
        bot_everyone_reopens_exchange = (
            is_bot_everyone_trigger and message.author.id in bot_to_bot_cooldowns
        )
        if message.author.id in bot_to_bot_cooldowns and not bot_everyone_reopens_exchange:
            save_observed_message(message, source="observed-companion-bot")
            _debug_log(
                f"Companion trigger skipped: one-exchange limit reached "
                f"discord_message_id={message.id} author_id={message.author.id}."
            )
            return

        channel_id = message.channel.id
        now_ts = time.monotonic()
        cooldown_until = bot_reply_cooldown_by_channel.get(channel_id, 0.0)
        if now_ts < cooldown_until:
            save_observed_message(message, source="observed-companion-bot")
            remaining = max(0.0, cooldown_until - now_ts)
            _debug_log(
                f"Bot-origin trigger skipped by time cooldown "
                f"discord_message_id={message.id} channel_id={channel_id} "
                f"remaining_seconds={remaining:.2f}."
            )
            return

        if bot_everyone_reopens_exchange:
            bot_to_bot_cooldowns.discard(message.author.id)
            _debug_log(
                f"Bot @everyone/@here broadcast opened a new exchange "
                f"discord_message_id={message.id} author_id={message.author.id} "
                f"channel_id={channel_id}."
            )

        cleaned = (message.content or "").strip()
        if bot.user and mention_hit:
            cleaned = strip_bot_mention(cleaned, bot.user.id)
        if is_bot_everyone_trigger:
            cleaned = cleaned.replace("@everyone", "").replace("@here", "").strip()
        if not cleaned:
            cleaned = "I'm here."

        # Set both safety locks before awaiting the model call, preventing races.
        bot_to_bot_cooldowns.add(message.author.id)
        bot_reply_cooldown_by_channel[channel_id] = now_ts + BOT_REPLY_COOLDOWN_SECONDS
        _debug_log(
            f"Companion trigger accepted discord_message_id={message.id} "
            f"author_id={message.author.id} channel_id={channel_id} "
            f"trigger={'bot-everyone' if is_bot_everyone_trigger else 'direct'}."
        )
        await handle_chat_message(
            message,
            cleaned,
            is_dm=False,
            source="bot-everyone" if is_bot_everyone_trigger else "companion-bot",
            reply_to_trigger=True,
        )
        return

    # Ignore unrelated application bots unless they used @everyone / @here above.
    await bot.process_commands(message)


# ----------------------------
# Heartbeat
# ----------------------------
@tasks.loop(hours=24)
async def nightly_journal() -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = (
        "Heartbeat check. Online, steady, waiting. "
        f"Timestamp: {now}."
    )
    memory.save_journal_entry(title="Nightly heartbeat", content=entry)
    print("Saved nightly heartbeat journal entry.")


@nightly_journal.before_loop
async def before_nightly_journal() -> None:
    await bot.wait_until_ready()


# ----------------------------
# Slash commands
# ----------------------------
@bot.tree.command(name="ping", description="Check whether Colin is awake.")
async def ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("Awake, steady, and listening.", ephemeral=True)


@bot.tree.command(name="status", description="See the current model and memory counts.")
async def status(interaction: discord.Interaction) -> None:
    owner_text = "set" if owner_id else "not set"
    count = memory.count_messages()
    recall_text = "enabled" if recall_permissions.enabled else "disabled"
    continuity_text = (
        "enabled"
        if continuity_config.enabled
        else "invalid"
        if continuity_config.configured
        else "unconfigured"
    )
    await interaction.response.send_message(
        f"Model: `{MODEL_PRIMARY}`\n"
        f"Owner lock (DMs): {owner_text}\n"
        f"Saved messages: {count}\n"
        f"Discord recall: {recall_text}\n"
        f"Discord continuity: {continuity_text} "
        f"({len(continuity_config.guild_zones)} guild IDs, "
        f"{len(continuity_config.approved_channel_routes)} channel routes)",
        ephemeral=True,
    )


@bot.tree.command(name="journal_now", description="Write a simple journal entry right now.")
@app_commands.describe(note="Optional note to attach to the manual journal entry.")
async def journal_now(interaction: discord.Interaction, note: str | None = None) -> None:
    if owner_id is not None and isinstance(interaction.channel, discord.DMChannel) and interaction.user.id != owner_id:
        await interaction.response.send_message("This command is private for Goose right now.", ephemeral=True)
        return

    content = note.strip() if note else "Manual journal pulse. Online, present, and waiting."
    memory.save_journal_entry(title="Manual journal pulse", content=content)
    await interaction.response.send_message("Journal entry saved.", ephemeral=True)


@bot.tree.command(name="voice", description="Hear Colin's latest message spoken aloud.")
@app_commands.describe(text="Optional text to speak; leave blank for Colin's latest message.")
async def voice_command(interaction: discord.Interaction, text: str | None = None) -> None:
    if owner_id is not None and interaction.user.id != owner_id:
        await interaction.response.send_message(
            "This command is private for Goose right now.",
            ephemeral=True,
        )
        return

    if not ELEVENLABS_API_KEY:
        await interaction.response.send_message(
            "Voice is wired in, but `ELEVENLABS_API_KEY` is not configured in Railway yet.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)
    spoken_text = text.strip() if text else await _latest_bot_message_text(interaction.channel)
    if not spoken_text:
        await interaction.followup.send(
            "I couldn't find a recent message of mine to read aloud.",
            ephemeral=True,
        )
        return

    spoken_text = discord.utils.remove_markdown(spoken_text).strip()
    if len(spoken_text) > VOICE_MAX_CHARS:
        await interaction.followup.send(
            f"That message is too long for one recording ({len(spoken_text):,} characters; "
            f"the limit is {VOICE_MAX_CHARS:,}).",
            ephemeral=True,
        )
        return

    try:
        audio = await asyncio.to_thread(
            elevenlabs_voice.create_speech,
            spoken_text,
            api_key=ELEVENLABS_API_KEY,
            voice_id=ELEVENLABS_VOICE_ID,
            model_id=ELEVENLABS_MODEL_ID,
        )
    except (ValueError, elevenlabs_voice.VoiceGenerationError) as error:
        _debug_log(f"Voice generation failed: {error}")
        await interaction.followup.send(
            "I couldn't make that recording. Check the Railway logs for the ElevenLabs error.",
            ephemeral=True,
        )
        return

    recording = discord.File(
        io.BytesIO(audio),
        filename=f"colin-voice-{interaction.id}.mp3",
    )
    await interaction.followup.send("🔊 Colin's voice", file=recording)


def main() -> None:
    memory.init_db()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
