# Harper's Companion Starter

A minimal Discord bot starter for a private, text-first Colin build.

## What this starter already does

- replies in DMs
- replies when mentioned in a server
- stores simple memory in SQLite
- observes permitted channel conversation without replying to every visible message
- treats observed dialogue as attributed context, not as Colin's identity or writing style
- allows one controlled reply to each companion bot until a human addresses Colin
- ignores duplicate deliveries of the same Discord message
- adds a short channel cooldown for bot-origin replies to reduce burst fan-out
- writes a nightly heartbeat journal entry
- includes slash commands for `/ping`, `/status`, and `/journal_now`
- uses OpenRouter as the model transport through the OpenAI-compatible client

## Folder layout

```text
harpers-companion-starter/
  src/
    __init__.py
    identity.py
    main.py
    memory.py
    router.py
  data/
  .env.example
  .gitignore
  requirements.txt
  README.md
```

## Before you run it

You will need:
- a Discord bot token
- your Discord server ID (`DISCORD_GUILD_ID`) for fast slash-command sync
- your own Discord user ID (`BOT_OWNER_DISCORD_ID`) if you want the bot locked to you
- an OpenRouter API key
- optional: `REASONING_EFFORT` to control GPT-5.5 thinking depth (`high` by default; valid values are `none`, `minimal`, `low`, `medium`, `high`, and `xhigh`)
- optional: `BOT_REPLY_COOLDOWN_SECONDS` to limit how often Colin replies to bot-origin messages in a channel
- optional: `MAX_REPLY_TOKENS` to control max model output tokens (default `2500`)

## Bot `@everyone` questions

Colin normally answers another bot only when that bot directly mentions him or
replies to one of his messages. A bot-authored `@everyone` or `@here` message is
also treated as an explicit room-wide invitation, even if that bot is not listed
in `COMPANION_BOT_NAMES`. Colin accepts either Discord's `mention_everyone` signal
or the literal `@everyone` / `@here` text in the received message. This covers
Discord deployments where the mention flag is not present on bot-authored messages.

This does not bypass Colin's bot-loop protections. His one-exchange latch,
per-channel time cooldown, message-ID deduplication, and safe mention handling still
apply. A bot `@everyone` or `@here` broadcast may start a new controlled exchange
even if Colin previously answered that same bot, but the per-channel time cooldown
still blocks rapid repeat broadcasts. Ordinary bot mentions/replies remain limited
to one exchange until a human addresses Colin.

## Discord visibility requirements

The code requests Discord's message content intent with `intents.message_content = True`, but code alone cannot make Discord deliver messages Colin is not allowed to see.

In the **Discord Developer Portal** for Colin's application:

1. Open **Bot**.
2. Find **Privileged Gateway Intents**.
3. Enable **Message Content Intent**.

In every Discord channel Colin should observe, his bot role also needs:

- **View Channel**
- **Read Message History**

The code-side observation change stores visible human and configured companion-bot messages without automatically answering them. The portal intent and channel permissions are a separate manual requirement; missing messages cannot be recovered later if Discord never delivered them.

## Local run (optional)

```bash
python -m venv .venv
source .venv/bin/activate   # macOS / Linux
# or
.venv\Scripts\activate      # Windows

pip install -r requirements.txt
cp .env.example .env
python -m src.main
```

## Railway deployment

1. Put this folder in a GitHub repo.
2. Create a new Railway project from that repo.
3. Add the environment variables from `.env.example` in Railway.
4. Set the start command to:

```bash
python -m src.main
```

5. Deploy.

## Notes

- This is a first skeleton, not the final architecture.
- SQLite is fine to start, but Postgres is a better long-term next step.
- The identity bundle lives in `src/identity.py`.
- The Discord behavior lives in `src/main.py`.
- The model call lives in `src/router.py`.
- The saved memory logic lives in `src/memory.py`.
