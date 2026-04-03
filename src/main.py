from __future__ import annotations

import asyncio
import os
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
MODEL_PRIMARY = os.getenv("MODEL_PRIMARY", "openai/gpt-4.1")

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing.")

owner_id = int(BOT_OWNER_DISCORD_ID) if BOT_OWNER_DISCORD_ID else None

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


async def handle_chat_message(message: discord.Message, cleaned_content: str, *, is_dm: bool) -> None:
    if owner_id is not None and message.author.id != owner_id:
        await message.channel.send("This build is private for Goose right now.")
        return

    source = "dm" if is_dm else "mention"
    memory.save_message(
        channel_id=message.channel.id,
        user_id=message.author.id,
        role="user",
        content=cleaned_content,
        source=source,
    )

    history = memory.get_recent_messages(channel_id=message.channel.id, limit=10)
    latest_journal = memory.get_latest_journal_entry()

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


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    is_dm = isinstance(message.channel, discord.DMChannel)

    if is_dm:
        cleaned = message.content.strip()
        if cleaned:
            await handle_chat_message(message, cleaned, is_dm=True)
        return

    if bot.user and bot.user.mentioned_in(message):
        cleaned = strip_bot_mention(message.content, bot.user.id)
        if not cleaned:
            cleaned = "I'm here."
        await handle_chat_message(message, cleaned, is_dm=False)
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
        f"Model: `{MODEL_PRIMARY}`\nOwner lock: {owner_text}\nSaved messages: {count}",
        ephemeral=True,
    )


@bot.tree.command(name="journal_now", description="Write a simple journal entry right now.")
@app_commands.describe(note="Optional note to attach to the manual journal entry.")
async def journal_now(interaction: discord.Interaction, note: str | None = None) -> None:
    if owner_id is not None and interaction.user.id != owner_id:
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
