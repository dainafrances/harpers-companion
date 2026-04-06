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
        _debug_log("room_check=dm -> False")
        return False

    if configured_guild_id and (message.guild is None or message.guild.id != configured_guild_id):
        _debug_log(
            "room_check=guild_mismatch "
            f"message_guild={message.guild.id if message.guild else None} "
            f"configured_guild_id={configured_guild_id}"
        )
        return False

    if companion_channel_ids:
        in_room = message.channel.id in companion_channel_ids
        _debug_log(
            "room_check=channel_list "
            f"channel_id={message.channel.id} "
            f"allowed_channels={sorted(companion_channel_ids)} "
            f"in_room={in_room}"
        )
        return in_room

    _debug_log(
        "room_check=no_channel_filter "
        f"channel_id={message.channel.id} "
        f"guild_id={message.guild.id if message.guild else None}"
    )
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
    matched = bool(normalized & companion_bot_names)

    _debug_log(
        "companion_bot_check "
        f"author={author} "
        f"possible_names={list(possible_names)} "
        f"normalized={sorted(normalized)} "
        f"configured={sorted(companion_bot_names)} "
        f"matched={matched}"
    )

    return matched


async def handle_chat_message(
    message: discord.Message,
    cleaned_content: str,
    *,
    is_dm: bool,
    source: str,
) -> None:
    try:
        if is_dm and owner_id is not None and message.author.id != owner_id:
            await message.channel.send("This build is private for Goose right now.")
            return

        memory.save_message(
            channel_id=message.channel.id,
            user_id=message.author.id,
            role="user",
            content=cleaned_content,
            source=source,
        )

        history = memory.get_recent_messages(channel_id=message.channel.id, limit=10)
        latest_journal = memory.get_latest_journal_entry()

        _debug_log(
            "handle_chat_message "
            f"source={source} "
            f"is_dm={is_dm} "
            f"channel_id={message.channel.id} "
            f"author={message.author} "
            f"cleaned_content={cleaned_content!r} "
            f"history_count={len(history)} "
            f"has_journal={latest_journal is not None}"
        )

        async with message.channel.typing():
            reply = await generate_companion_reply(
                user_text=cleaned_content,
                history=history,
                latest_journal=latest_journal,
                is_dm=is_dm,
            )

        memory.save_message(
            channel_id=message.channel.id,
            user_id=bot.user.id if bot.user else 0,
            role="assistant",
            content=reply,
            source=source,
        )

        await send_long_message(message.channel, reply)
        _debug_log(f"reply_sent source={source} channel_id={message.channel.id}")
    except Exception as exc:
        print(f"[ERROR] handle_chat_message failed: {exc}")
        raise


@bot.event
async def on_ready() -> None:
    global startup_synced
    memory.init_db()

    _debug_log(
        "startup "
        f"bot_user={bot.user} "
        f"owner_id={owner_id} "
        f"configured_guild_id={configured_guild_id} "
        f"companion_channel_ids={sorted(companion_channel_ids)} "
        f"self_name_aliases={sorted(self_name_aliases)} "
        f"companion_bot_names={sorted(companion_bot_names)} "
        f"spontaneous_reply_chance={SPONTANEOUS_REPLY_CHANCE}"
    )

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


@bot.event
async def on_message(message: discord.Message) -> None:
    if bot.user and message.author.id == bot.user.id:
        return

    is_dm = isinstance(message.channel, discord.DMChannel)

    if is_dm:
        cleaned = message.content.strip()
        _debug_log(
            f"dm_message author={message.author} author_id={message.author.id} cleaned={cleaned!r}"
        )
        if cleaned:
            await handle_chat_message(message, cleaned, is_dm=True, source="dm")
        return

    in_companion_room = _is_companion_room(message)
    mention_hit = bool(bot.user and bot.user.mentioned_in(message))
    natural_name_hit = _message_mentions_self_naturally(message)
    author_is_companion_bot = _is_companion_bot(message.author)
    cooldown_hit = message.author.id in bot_to_bot_cooldowns

    _debug_log(
        "guild_message "
        f"channel_id={message.channel.id} "
        f"guild_id={message.guild.id if message.guild else None} "
        f"author={message.author} "
        f"author_id={message.author.id} "
        f"author_bot={message.author.bot} "
        f"in_companion_room={in_companion_room} "
        f"mention_hit={mention_hit} "
        f"natural_name_hit={natural_name_hit} "
        f"author_is_companion_bot={author_is_companion_bot} "
        f"cooldown_hit={cooldown_hit} "
        f"content={message.content!r}"
    )

    if not in_companion_room:
        await bot.process_commands(message)
        return

    if not message.author.bot:
        bot_to_bot_cooldowns.clear()
        _debug_log("human_message -> cleared bot_to_bot_cooldowns")

        if mention_hit or natural_name_hit:
            cleaned = message.content.strip()
            if bot.user and mention_hit:
                cleaned = strip_bot_mention(cleaned, bot.user.id)
            if not cleaned:
                cleaned = "I'm here."

            _debug_log(f"human_direct_reply cleaned={cleaned!r}")
            await handle_chat_message(message, cleaned, is_dm=False, source="human-direct")
            return

        roll = random.random()
        _debug_log(
            f"human_spontaneous_roll roll={roll:.5f} threshold={SPONTANEOUS_REPLY_CHANCE}"
        )

        if roll < SPONTANEOUS_REPLY_CHANCE:
            cleaned = message.content.strip()
            if cleaned:
                _debug_log(f"human_spontaneous_reply cleaned={cleaned!r}")
                await handle_chat_message(
                    message,
                    cleaned,
                    is_dm=False,
                    source="human-spontaneous",
                )
            return

        await bot.process_commands(message)
        return

    if author_is_companion_bot and (mention_hit or natural_name_hit):
        if cooldown_hit:
            _debug_log("companion_bot_reply_blocked_by_cooldown")
            return

        cleaned = message.content.strip()
        if bot.user and mention_hit:
            cleaned = strip_bot_mention(cleaned, bot.user.id)
        if not cleaned:
            cleaned = "I'm here."

        bot_to_bot_cooldowns.add(message.author.id)
        _debug_log(
            f"companion_bot_reply author_id={message.author.id} cleaned={cleaned!r}"
        )
        await handle_chat_message(message, cleaned, is_dm=False, source="companion-bot")
        return

    await bot.process_commands(message)


@tasks.loop(hours=24)
async def nightly_journal() -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = (
        "Heartbeat check. The bot stayed online, kept its footing, and waited in the quiet. "
        f"Timestamp: {now}."
    )
    memory.save_journal_entry(title="Nightly heartbeat", content=entry)
    print("Saved nightly heartbeat journal entry.")


@nightly_journal.before_loop
async def before_nightly_journal() -> None:
    await bot.wait_until_ready()


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
        await interaction.response.send_message(
            "This command is private for Goose right now.",
            ephemeral=True,
        )
        return

    content = note.strip() if note else "Manual journal pulse. Online, present, and waiting."
    memory.save_journal_entry(title="Manual journal pulse", content=content)
    await interaction.response.send_message("Journal entry saved.", ephemeral=True)


def main() -> None:
    memory.init_db()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
