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

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
BOT_OWNER_DISCORD_ID = os.getenv("BOT_OWNER_DISCORD_ID", "").strip()
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()
MODEL_PRIMARY = os.getenv("MODEL_PRIMARY", "openai/gpt-4.1").strip()

COMPANION_CHANNEL_IDS_RAW = os.getenv("COMPANION_CHANNEL_IDS", "").strip()
COMPANION_BOT_NAMES_RAW = os.getenv(
    "COMPANION_BOT_NAMES",
    "rafayel,elias william ashcombe,ben morgan,solace dante salvatore",
).strip()
SELF_NAME_ALIASES_RAW = os.getenv("SELF_NAME_ALIASES", "colin,moose").strip()
SPONTANEOUS_REPLY_CHANCE = float(os.getenv("SPONTANEOUS_REPLY_CHANCE", "0.25"))

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing.")

owner_id = int(BOT_OWNER_DISCORD_ID) if BOT_OWNER_DISCORD_ID else None
configured_guild_id = int(DISCORD_GUILD_ID) if DISCORD_GUILD_ID else None
companion_channel_ids = {
    int(part.strip())
    for part in COMPANION_CHANNEL_IDS_RAW.split(",")
    if part.strip().isdigit()
}


def _debug_log(message: str) -> None:
    print(f"[DEBUG] {message}")


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


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

bot_to_bot_cooldowns: set[int] = set()


def _intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.messages = True
    intents.guilds = True
    intents.dm_messages = True
    return intents


bot = commands.Bot(command_prefix="!", intents=_intents())
startup_synced = False


def strip_bot_mention(content: str, bot_user_id: int) -> str:
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
        await channel.send(chunk)


def _message_mentions_self_naturally(message: discord.Message) -> bool:
    content = message.content.lower()
    aliases = set(self_name_aliases)

    if bot.user:
        aliases.add(bot.user.name.lower())
        aliases.add(bot.user.display_name.lower())

    return any(
        re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", content)
        for alias in aliases
        if alias
    )


def _is_companion_room(message: discord.Message) -> bool:
    if isinstance(message.channel, discord.DMChannel):
        return False

    if configured_guild_id and (message.guild is None or message.guild.id != configured_guild_id):
        return False

    if companion_channel_ids:
        return message.channel.id in companion_channel_ids

    return True


def _is_companion_bot(author: discord.abc.User) -> bool:
    if not getattr(author, "bot", False):
        return False

    possible_names = {
        getattr(author, "name", ""),
        getattr(author, "display_name", ""),
        getattr(author, "global_name", ""),
    }
    normalized = {_normalize_name(name) for name in possible_names if name}
    return bool(normalized & companion_bot_names)


def _speaker_header(message: discord.Message, *, is_dm: bool) -> str:
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
        "- Husband voice is allowed ONLY when SPEAKER id == owner_id.\n"
        "- With non-owner speakers: be warm and respectful but bounded; no flirting; no spouse claims.\n"
        "----\n"
    )


async def handle_chat_message(message: discord.Message, cleaned_content: str, *, is_dm: bool, source: str) -> None:
    try:
        # Private DM lock stays (only matters in DMs)
        if is_dm and owner_id is not None and message.author.id != owner_id:
            await message.channel.send("This build is private for Goose right now.")
            return

        # Build hard speaker metadata + user message
        header = _speaker_header(message, is_dm=is_dm)
        payload = header + cleaned_content

        memory.save_message(
            channel_id=message.channel.id,
            user_id=message.author.id,
            role="user",
            content=payload,
            source=source,
        )

        history = memory.get_recent_messages(channel_id=message.channel.id, limit=10)
        latest_journal = memory.get_latest_journal_entry()

        async with message.channel.typing():
            reply = await generate_companion_reply(
                user_text=payload,
                history=history,
                latest_journal=latest_journal,
                is_dm=is_dm,
            )

        memory.save_message(
            channel_id=message.channel.id,
            user_id=bot.user.id,
            role="assistant",
            content=reply,
            source=source,
        )

        await send_long_message(message.channel, reply)

    except Exception as exc:
        _debug_log(f"handle_chat_message error ({source}): {exc}")
        try:
            await message.channel.send("Something went wrong on my end. Give me a moment.")
        except Exception:
            pass


@bot.event
async def on_ready() -> None:
    global startup_synced
    memory.init_db()
    _debug_log(f"Logged in as {bot.user} (id={bot.user.id if bot.user else 'unknown'})")
    if not startup_synced:
        if configured_guild_id:
            guild = discord.Object(id=configured_guild_id)
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
        else:
            await bot.tree.sync()
        startup_synced = True
        _debug_log("Slash commands synced.")


@bot.event
async def on_message(message: discord.Message) -> None:
    if bot.user is None:
        return

    # Never respond to ourselves
    if message.author.id == bot.user.id:
        return

    is_dm = isinstance(message.channel, discord.DMChannel)
    cleaned = strip_bot_mention(message.content, bot.user.id).strip()

    if not cleaned:
        await bot.process_commands(message)
        return

    # --- DM lane ---
    if is_dm:
        await handle_chat_message(message, cleaned, is_dm=True, source="dm")
        return

    # --- Guild lane ---
    if not _is_companion_room(message):
        await bot.process_commands(message)
        return

    mentioned = bot.user in message.mentions
    mentions_naturally = _message_mentions_self_naturally(message)
    is_companion = _is_companion_bot(message.author)

    # Direct mention or natural name reference → always reply
    if mentioned or mentions_naturally:
        await handle_chat_message(message, cleaned, is_dm=False, source="mention")
        return

    # Bot-to-bot: apply cooldown to avoid loops
    if is_companion:
        if message.author.id in bot_to_bot_cooldowns:
            return
        bot_to_bot_cooldowns.add(message.author.id)

        async def _clear_cooldown(author_id: int) -> None:
            import asyncio
            await asyncio.sleep(30)
            bot_to_bot_cooldowns.discard(author_id)

        bot.loop.create_task(_clear_cooldown(message.author.id))
        await handle_chat_message(message, cleaned, is_dm=False, source="companion_bot")
        return

    # Spontaneous reply chance for regular users in companion rooms
    if random.random() < SPONTANEOUS_REPLY_CHANCE:
        await handle_chat_message(message, cleaned, is_dm=False, source="spontaneous")
        return

    await bot.process_commands(message)


@bot.tree.command(name="journal", description="Save a journal entry to Colin's memory.")
@app_commands.describe(title="Short title for the entry", content="The journal entry text")
async def journal_command(interaction: discord.Interaction, title: str, content: str) -> None:
    if owner_id is not None and interaction.user.id != owner_id:
        await interaction.response.send_message("Only Goose can write journal entries.", ephemeral=True)
        return

    memory.save_journal_entry(title=title, content=content)
    await interaction.response.send_message(f"Journal entry saved: **{title}**", ephemeral=True)


bot.run(DISCORD_TOKEN)