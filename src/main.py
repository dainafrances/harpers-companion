from __future__ import annotations

import os
import random
import re
import time
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from . import memory
from .router import generate_companion_reply

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

MODEL_PRIMARY = os.getenv("MODEL_PRIMARY", "anthropic/claude-sonnet-4.5").strip()

# Optional channel restriction list. Leave blank to allow all channels
# inside the allowed guild(s).
COMPANION_CHANNEL_IDS_RAW = os.getenv("COMPANION_CHANNEL_IDS", "").strip()

# Comma-separated list of other companion bot names (display/global/name).
COMPANION_BOT_NAMES_RAW = os.getenv(
    "COMPANION_BOT_NAMES",
    "rafayel,elias william ashcombe,ben morgan,solace dante salvatore",
).strip()

# Bot IDs permitted to address Colin with @everyone. This is deliberately
# separate from companion names so an unrelated bot cannot trigger him by
# adopting a familiar display name.
BOT_EVERYONE_TRIGGER_IDS_RAW = os.getenv(
    "BOT_EVERYONE_TRIGGER_IDS",
    "1496237287825080390",
).strip()

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
bot_everyone_trigger_ids = _parse_channel_ids(BOT_EVERYONE_TRIGGER_IDS_RAW)

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
) -> None:
    chunks = split_for_discord(text)
    for index, chunk in enumerate(chunks):
        try:
            if index == 0 and reply_to is not None:
                await reply_to.reply(
                    chunk,
                    mention_author=True,
                    allowed_mentions=_safe_allowed_mentions(replied_user=True),
                )
            else:
                await channel.send(
                    chunk,
                    allowed_mentions=_safe_allowed_mentions(),
                )
        except discord.Forbidden:
            _debug_log("FORBIDDEN: Bot lacks permission to send messages in this channel.")
            raise
        except discord.HTTPException as e:
            _debug_log(f"HTTPException while sending message: {e}")
            raise


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


def save_observed_message(message: discord.Message, *, source: str) -> bool:
    """Save visible room context without causing Colin to answer."""
    if not memory.try_claim_discord_message(
        message_id=message.id,
        channel_id=message.channel.id,
        author_id=message.author.id,
        source=source,
    ):
        _debug_log(f"Skipping duplicate observed Discord message {message.id}.")
        return False

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
    guild_id = message.guild.id if message.guild else None
    channel_id = message.channel.id

    return (
        f"[SPEAKER] name={speaker_name} id={speaker_id} is_bot={is_bot}\n"
        f"[CONTEXT] dm={is_dm} guild_id={guild_id} channel_id={channel_id}\n"
        f"[OWNER] owner_id={owner_id}\n"
        "RULES:\n"
        "- Only owner_id is Goose / wife / Daina.\n"
        "- Never infer speaker identity. Use the SPEAKER header.\n"
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

        # Pull history BEFORE saving current message, so we don't double-send the same turn
        history = memory.get_recent_messages(channel_id=message.channel.id, limit=12)
        latest_journal = memory.get_latest_journal_entry()

        # Save current user message for future turns / memory
        stored_payload = payload
        if image_urls:
            stored_payload += f"\n[ATTACHMENTS: {len(image_urls)} image(s)/gif(s)]"

        memory.save_message(
            channel_id=message.channel.id,
            user_id=message.author.id,
            role="user",
            content=stored_payload,
            source=source,
        )

        async with message.channel.typing():
            speaker_name = (
                getattr(message.author, "display_name", None)
                or getattr(message.author, "name", "unknown")
            )
            reply = await generate_companion_reply(
                user_text=payload,
                history=history,
                latest_journal=latest_journal,
                is_dm=is_dm,
                speaker_name=speaker_name,
                speaker_is_owner=owner_id is not None and message.author.id == owner_id,
                image_urls=image_urls,
            )

        # Save assistant reply
        memory.save_message(
            channel_id=message.channel.id,
            user_id=bot.user.id if bot.user else 0,
            role="assistant",
            content=reply,
            source=source,
        )

        chunk_count = len(split_for_discord(reply))
        _debug_log(
            f"Sending reply for Discord message {message.id} "
            f"source={source} length={len(reply)} chunks={chunk_count}."
        )
        await send_long_message(
            message.channel,
            reply,
            reply_to=message if reply_to_trigger else None,
        )

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
    _debug_log(f"Owner ID: {owner_id}")
    _debug_log(f"Companion bot names: {sorted(companion_bot_names)}")
    _debug_log(f"Bot @everyone trigger IDs: {sorted(bot_everyone_trigger_ids)}")
    _debug_log(f"Self aliases: {sorted(self_name_aliases)}")
    _debug_log(f"Spontaneous chance: {SPONTANEOUS_REPLY_CHANCE}")
    _debug_log(f"Bot reply cooldown seconds: {BOT_REPLY_COOLDOWN_SECONDS}")


@bot.event
async def on_message(message: discord.Message) -> None:
    # Ignore ourselves. Colin's own replies are already saved by the normal response path.
    if bot.user and message.author.id == bot.user.id:
        return

    is_dm = isinstance(message.channel, discord.DMChannel)

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

    # COMPANION BOT messages: direct @mention, Discord reply to Colin, or an
    # @everyone from a specifically allowlisted companion bot.
    if _is_companion_bot(message.author):
        trusted_everyone_hit = (
            everyone_hit and message.author.id in bot_everyone_trigger_ids
        )
        companion_trigger = mention_hit or reply_to_self or trusted_everyone_hit
        if not companion_trigger:
            save_observed_message(message, source="observed-companion-bot")
            return

        if message.author.id in bot_to_bot_cooldowns:
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

        cleaned = (message.content or "").strip()
        if bot.user and mention_hit:
            cleaned = strip_bot_mention(cleaned, bot.user.id)
        if trusted_everyone_hit:
            cleaned = cleaned.replace("@everyone", "").replace("@here", "").strip()
        if not cleaned:
            cleaned = "I'm here."

        # Set both safety locks before awaiting the model call, preventing races.
        bot_to_bot_cooldowns.add(message.author.id)
        bot_reply_cooldown_by_channel[channel_id] = now_ts + BOT_REPLY_COOLDOWN_SECONDS
        _debug_log(
            f"Companion trigger accepted discord_message_id={message.id} "
            f"author_id={message.author.id} channel_id={channel_id} "
            f"trigger={'trusted-everyone' if trusted_everyone_hit else 'direct'}."
        )
        await handle_chat_message(
            message,
            cleaned,
            is_dm=False,
            source="companion-bot",
            reply_to_trigger=True,
        )
        return

    # Ignore unrelated application bots; they are not part of Colin's companion context.
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
    await interaction.response.send_message(
        f"Model: `{MODEL_PRIMARY}`\nOwner lock (DMs): {owner_text}\nSaved messages: {count}",
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


def main() -> None:
    memory.init_db()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
