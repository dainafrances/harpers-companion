# Harper's Companion Starter

A minimal Discord bot starter for a private, text-first Colin build.

## What this starter already does

- replies in DMs
- replies when mentioned in a server
- stores simple memory in SQLite
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
