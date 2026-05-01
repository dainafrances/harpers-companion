from __future__ import annotations

import os
import random
import re
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
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()

MODEL_PRIMARY = os.getenv("MODEL_PRIMARY", "openai/gpt-5.5").strip()

# Comma-separated list of channel IDs allowed for the shared companion room.
# Example: "123,456"
COMPANION_CHANNEL_IDS_RAW = os.getenv("COMPANION_CHANNEL_IDS", "").strip()

# Comma-separated list of other companion bot names (display/global/name).
# Example: "rafayel,elias william ashcombe,ben morgan,solace dante salvatore"
COMPANION_BOT_NAMES_RAW = os.getenv(
    "COMPANION_BOT_NAMES",
    "rafayel,elias william ashcombe,ben morgan,solace dante salvatore",
).strip()

# Comma-separated aliases Colin should respond to if spoken naturally (not @ mention).
# Example: "colin,moose"
SELF_NAME_ALIASES_RAW = os.getenv("SELF_NAME_ALIASES", "colin,moose").strip()

# 0.25 = 25% chance to jump in on human messages even without mention
SPONTANEOUS_REPLY_CHANCE = float(os.getenv("SPONTANEOUS_REPLY_CHANCE", "0.25"))

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing.")

owner_id = int(BOT_OWNER_DISCORD_ID) if BOT_OWNER_DISCORD_ID else None
configured_guild_id = int(DISCORD_GUILD_ID) if DISCORD_GUILD_ID else None


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


companion_channel_ids = _parse_channel_ids(COMPANION_CHANNEL_IDS_RAW)

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

# cooldown set: store bot author IDs we already responded to (until a human speaks again)
bot_to_bot_cooldowns: set[int] = set()

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


def split_for_discord(text: str, limit: int = 1900) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n"):
        candidate = f"{current}\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            while len(paragraph) > limit:
                chunks.append(paragraph[:limit])
                paragraph = paragraph[limit:]
            current = paragraph
    if current:
        chunks.append(current)
    return chunks


async def send_long_message(channel: discord.abc.Messageable, text: str) -> None:
    for chunk in split_for_discord(text):
        try:
            await channel.send(chunk)
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
    - If DISCORD_GUILD_ID is set, only messages from that guild are allowed.
    - If COMPANION_CHANNEL_IDS is set, those channels are always allowed.
    - ALSO allow any direct mention of Colin inside the allowed guild,
      even if the channel wasn't pre-listed (useful for new private channels).
    """
    if isinstance(message.channel, discord.DMChannel):
        return False

    if configured_guild_id:
        if message.guild is None or message.guild.id != configured_guild_id:
            return False

    # Explicitly allowed channels always pass
    if companion_channel_ids and message.channel.id in companion_channel_ids:
        return True

    # If Colin is directly pinged in the home guild, allow it
    if bot.user and message.mentions and bot.user in message.mentions:
        return True

    # If no channel list is configured, allow all channels in the allowed guild
    if not companion_channel_ids:
        return True

    return False

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
async def handle_chat_message(message: discord.Message, cleaned_content: str, *, is_dm: bool, source: str) -> None:
    try:
        # Private DM lock stays (only matters in DMs)
        if is_dm and owner_id is not None and message.author.id != owner_id:
            await message.channel.send("This build is private for Goose right now.")
            return

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
            reply = await generate_companion_reply(
                user_text=payload,
                history=history,
                latest_journal=latest_journal,
                is_dm=is_dm,
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

        await send_long_message(message.channel, reply)

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
            if DISCORD_GUILD_ID:
                guild = discord.Object(id=int(DISCORD_GUILD_ID))
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
                print(f"Synced {len(synced)} command(s) to guild {DISCORD_GUILD_ID}.")
            else:
                synced = await bot.tree.sync()
                print(f"Synced {len(synced)} global command(s).")
        finally:
            startup_synced = True

    if not nightly_journal.is_running():
        nightly_journal.start()

    print(f"Logged in as {bot.user} using model {MODEL_PRIMARY}")
    _debug_log(f"Configured guild lock: {configured_guild_id}")
    _debug_log(f"Companion channel IDs: {sorted(companion_channel_ids) if companion_channel_ids else 'ALL (within allowed guild)'}")
    _debug_log(f"Owner ID: {owner_id}")
    _debug_log(f"Companion bot names: {sorted(companion_bot_names)}")
    _debug_log(f"Self aliases: {sorted(self_name_aliases)}")
    _debug_log(f"Spontaneous chance: {SPONTANEOUS_REPLY_CHANCE}")


@bot.event
async def on_message(message: discord.Message) -> None:
    # ignore ourselves
    if bot.user and message.author.id == bot.user.id:
        return

    is_dm = isinstance(message.channel, discord.DMChannel)

    # DMs: always respond (subject to owner lock)
    if is_dm:
        cleaned = (message.content or "").strip()
        if cleaned:
            await handle_chat_message(message, cleaned, is_dm=True, source="dm")
        return

    # Guild / channel gating
    if not _is_companion_room(message):
        await bot.process_commands(message)
        return

    # Mention checks:
    # - mention_hit = Colin was directly @mentioned
    # - everyone_hit = @everyone or @here was used
    # - natural_name_hit = someone said "Colin" or "Moose" naturally
    mention_hit = bool(bot.user and message.mentions and bot.user in message.mentions)
    everyone_hit = bool(getattr(message, "mention_everyone", False))
    natural_name_hit = _message_mentions_self_naturally(message)

    # HUMAN messages
    if not message.author.bot:
        # any human message resets bot-to-bot cooldowns
        bot_to_bot_cooldowns.clear()

        if mention_hit or everyone_hit or natural_name_hit:
            cleaned = (message.content or "").strip()

            # Remove Colin's direct mention if present
            if bot.user and mention_hit:
                cleaned = strip_bot_mention(cleaned, bot.user.id)

            # Remove @everyone / @here so Colin doesn't answer as if those words are the message
            if everyone_hit:
                cleaned = cleaned.replace("@everyone", "").replace("@here", "").strip()

            if not cleaned:
                cleaned = "I'm here."

            source = "human-everyone" if everyone_hit and not mention_hit and not natural_name_hit else "human-direct"

            await handle_chat_message(message, cleaned, is_dm=False, source=source)
            return

        # 25% chance to jump in
        if random.random() < SPONTANEOUS_REPLY_CHANCE:
            cleaned = (message.content or "").strip()
            if cleaned:
                await handle_chat_message(message, cleaned, is_dm=False, source="human-spontaneous")
            return

        await bot.process_commands(message)
        return

    # BOT messages (other companion bots)
    if _is_companion_bot(message.author) and (mention_hit or natural_name_hit):
        # cooldown: don't answer the same bot twice until a human speaks
        if message.author.id in bot_to_bot_cooldowns:
            return

        cleaned = (message.content or "").strip()
        if bot.user and mention_hit:
            cleaned = strip_bot_mention(cleaned, bot.user.id)
        if not cleaned:
            cleaned = "I'm here."

        bot_to_bot_cooldowns.add(message.author.id)
        await handle_chat_message(message, cleaned, is_dm=False, source="companion-bot")
        return

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
