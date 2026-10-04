# Harper's Companion Starter

A minimal Discord bot starter for a private, text-first Colin build.

## What this starter already does

- replies in DMs
- replies when mentioned in a server
- stores simple memory in SQLite
- observes permitted channel conversation without replying to every visible message
- can optionally index approved Discord channels for receipts-based recall
- can keep a Colin-only chronological handoff across explicitly approved rooms
- applies a one-way disclosure ladder so awareness never grants permission to repeat private context
- includes explicit Discord room context on each saved/prompted message
- treats observed dialogue as attributed context, not as Colin's identity or writing style
- allows one controlled reply to each companion bot until a human addresses Colin
- ignores duplicate deliveries of the same Discord message
- adds a short channel cooldown for bot-origin replies to reduce burst fan-out
- writes a nightly heartbeat journal entry
- includes slash commands for `/ping`, `/status`, `/journal_now`, and `/voice`
- uses OpenRouter as the model transport through the OpenAI-compatible client
- reads supported document attachments and can search the web with source links

## Folder layout

```text
harpers-companion-starter/
  src/
    __init__.py
    identity.py
    continuity.py
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
- an ElevenLabs API key if you want `/voice` recordings
- optional: `MODEL_PRIMARY` to override GPT-5.6 Sol (`openai/gpt-5.6-sol` by default)
- optional: `PRIVACY_AUDIT_MODEL` to use a separate model for the tool-free disclosure check (defaults to `MODEL_PRIMARY`)
- optional: `REASONING_EFFORT` to control thinking depth (`high` by default; valid values are `none`, `minimal`, `low`, `medium`, `high`, and `xhigh`)
- optional: `BOT_REPLY_COOLDOWN_SECONDS` to limit how often Colin replies to bot-origin messages in a channel
- optional: `MAX_REPLY_TOKENS` to control max model output tokens (default `2500`)
- optional: `ENABLE_WEB_SEARCH` to turn OpenRouter web search on or off (`true` by default)
- optional: `MAX_DOCUMENT_BYTES` and `MAX_DOCUMENT_CHARS` to cap document processing
- optional: `DISCORD_RECALL_GUILD_IDS` and `DISCORD_RECALL_CHANNEL_IDS` to explicitly opt guilds/channels into the recall index
- optional: `DISCORD_CONTINUITY_GUILD_ZONES`, `DISCORD_CONTINUITY_CHANNEL_ROUTES`, `DISCORD_CONTINUITY_HANDOFF_LIMIT`, `DISCORD_CONTINUITY_HANDOFF_MAX_AGE_MINUTES`, and `DISCORD_CONTINUITY_AWARENESS_PER_GUILD_LIMIT` to enable Colin-only cross-server awareness and handoffs
- optional: `ROOM_CONTEXT_GUILD_LABELS` and `ROOM_CONTEXT_CHANNEL_LABELS` to label rooms with trusted modes/names
- optional: `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL_ID`, and `VOICE_MAX_CHARS` to override the `/voice` defaults

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

## Document uploads and web research

Colin can read attached `.txt`, `.md`, `.csv`, `.pdf`, and `.docx` files. Text is
extracted locally by the bot and supplied with the message; files above the configured
size limit are not processed. Image attachments continue to use the existing vision flow.

Web research uses OpenRouter's `openrouter:web_search` server tool. It is enabled by
default, and Colin can include source links in his reply. Set `ENABLE_WEB_SEARCH=false`
to disable it for a deployment.

## ElevenLabs voice recordings

The `/voice` command creates an MP3 with ElevenLabs. With no text option, it reads
Colin's most recent message in the current Discord channel, including consecutive
chunks from a long reply. You can also supply text directly with the optional
`text` field. Markdown formatting is removed before speech generation.

Add `ELEVENLABS_API_KEY` to Railway as a secret variable. Do not commit the key to
GitHub. The default voice is `uTTVBQHpmHNum2rmocA4`, using
`eleven_v3` and `mp3_44100_128`. Set `BOT_OWNER_DISCORD_ID` to keep
the command owner-only and prevent other server members from spending the account's
ElevenLabs credits.

The code requests Discord's message content intent with `intents.message_content = True`, but code alone cannot make Discord deliver messages Colin is not allowed to see.

In the **Discord Developer Portal** for Colin's application:

1. Open **Bot**.
2. Find **Privileged Gateway Intents**.
3. Enable **Message Content Intent**.

In every Discord channel Colin should observe, his bot role also needs:

- **View Channel**
- **Read Message History**

The code-side observation change stores visible human and configured companion-bot messages without automatically answering them. The portal intent and channel permissions are a separate manual requirement; missing messages cannot be recovered later if Discord never delivered them.

## Permission-aware Discord recall

Discord recall is off unless you explicitly configure at least one recall guild or
channel. This is separate from ordinary companion-room visibility: a channel can
be visible to Colin without being added to the retrieval index.

Use these environment variables to opt approved spaces into recall:

```text
DISCORD_RECALL_GUILD_IDS=123456789012345678
DISCORD_RECALL_CHANNEL_IDS=234567890123456789,345678901234567890
```

When recall is enabled, Colin indexes approved messages with Discord provenance:
message ID, speaker display name, speaker Discord user ID, timestamp, guild ID,
channel ID, channel name, and message content. Recall-style questions such as
“What is the latest thing Rachael said?” or “Can you see the other conversation?”
receive a structured `[DISCORD_RETRIEVAL]` context block before the model answers.

Without continuity configured, the writer-safe retrieval block tells Colin
whether results are `COMPLETE`, `PARTIAL`, `PERMISSION_LIMITED`, or
`UNAVAILABLE`. With continuity enabled, retrieved events instead join Colin's
private awareness packet with their `ALLOWED` or `FORBIDDEN` speech marker; the
audited outward response still cannot reveal that forbidden evidence exists.
Retrieved messages are supplied as inert user-role transcript evidence—not
Colin's identity, voice, style instructions, or a system instruction.

## Colin-only cross-server continuity

Cross-server continuity is off unless both continuity variables are configured.
It uses a separate ledger owned by Colin's bot; it is not a shared transcript and
does not read or write Ben's database.

Configure each server's confidentiality zone and every approved server/channel
pair by Discord ID:

```text
DISCORD_CONTINUITY_GUILD_ZONES=111111111111111111:nest;222222222222222222:cabin;333333333333333333:harpers
DISCORD_CONTINUITY_CHANNEL_ROUTES=111111111111111111:111111111111111101;222222222222222222:222222222222222201;333333333333333333:333333333333333301
DISCORD_CONTINUITY_HANDOFF_LIMIT=12
DISCORD_CONTINUITY_HANDOFF_MAX_AGE_MINUTES=120
DISCORD_CONTINUITY_AWARENESS_PER_GUILD_LIMIT=4
```

`DISCORD_CONTINUITY_CHANNEL_ROUTES` must contain the exact guild ID and channel
ID together. A channel name such as `the-bedroom` is never enough, because the
same name can exist in more than one server. Any malformed or incomplete
continuity configuration disables the feature rather than partially enabling it,
and suppresses legacy cross-room recall and unscoped journal injection until the
configuration is repaired. All three ladder zones must be present, and every
configured guild must have at least one approved channel route.

The disclosure ladder is:

| Source of the context | May be discussed in |
| --- | --- |
| The Nest (`nest`) | The Nest, The Cabin, and The Harpers |
| The Cabin (`cabin`) | The Cabin and The Harpers |
| The Harpers (`harpers`) | The Harpers only |

Colin receives a bounded recent awareness window from every configured server,
with the originating server, channel, speaker, timestamp, and disclosure marker
kept on every event. `DISCORD_CONTINUITY_AWARENESS_PER_GUILD_LIMIT` controls the
maximum recent events supplied per server (default `4`); the handoff age limit
also bounds this window. Awareness is deliberately broader than speech: Colin
may use restricted events to understand chronology and subtext, but cannot
quote, paraphrase, confirm, hint at, or visibly signal them in a room where they
are forbidden.

An audience gate adds ordinary discretion on top of the hard ladder. The Nest
is treated as friends/company, The Cabin as Daina/Ben/Colin, and The Harpers as
Goose-and-Moose private space. Explicit couple details, confidences, and candid
opinions are therefore not automatically repeated merely because their source
room's rank would technically allow it. When restricted evidence is present,
the drafting and audit path is tool-free and fails closed.

The intended three-server deployment map is:

- The Nest (`nest`, public): `#𝒆𝒗𝒆𝒓𝒚𝒐𝒏𝒆·🪺`, `#𝒄𝒐𝒍𝒊𝒏·🫎`, and
  `#𝒎𝒐𝒐𝒔𝒆-𝒂𝒏𝒅-𝒈𝒐𝒐𝒔𝒆·🫎💗🪿`.
- The Cabin (`cabin`, private group): `#the-bedroom`, `#beside-the-fire`,
  `#the-workshop`, and `#the-family-room`.
- The Harpers (`harpers`, private): `#the-hearth`, `#the-study`,
  `#the-bedroom`, `#the-dock`, `#the-mantelpiece`, `#the-ledger`,
  `#the-workbench`, and `#by-the-fire`.

These names are a deployment checklist only. The runtime policy still requires
the fifteen exact channel IDs, so the two different `#the-bedroom` rooms cannot
ever be confused by their display name.

Continuity configuration does not override the bot's ordinary Discord access
gate. `DISCORD_GUILD_IDS` must include all three server IDs, and
`COMPANION_CHANNEL_IDS` must either be blank (all channels inside those servers)
or include all fifteen intended channel IDs. For explicit recall questions,
configure the same approved scope in `DISCORD_RECALL_GUILD_IDS` and
`DISCORD_RECALL_CHANNEL_IDS`. Colin's Discord role still needs **View Channel**
and **Read Message History** in every listed room.

When Daina and Colin move between approved rooms, Colin receives a bounded,
timestamped handoff from the most recent other room. By default that handoff
remains available for 120 minutes, so an arrival greeting does not consume the
context before the next message can refer to it; the maximum age is configurable
and capped at 24 hours. Every imported event keeps its original server, channel,
speaker, and Discord event time, while the current room remains authoritative.
Messages from other speakers in that prior room can therefore travel with the
handoff without implying that those people moved too.

The continuity ledger starts filling when this feature is enabled; it does not
pretend to have Discord messages the bot never received. Legacy channel-local
history carries its database storage time as an explicit age marker, so an old
room transcript is not silently presented as dialogue happening tonight.

Private evidence that cannot be disclosed in the current room is withheld from
the outward reply writer. A separate tool-free privacy audit checks the complete
proposed reply, citations, and reactions before anything is stored or sent. It
blocks quotations, paraphrases, hints, confirmations, denials, and reaction-only
leaks; audit errors fail closed. Explicit recall questions use the same ladder.

Legacy journal entries do not record their source room. While continuity is
configured, they are therefore supplied only in The Harpers—not in DMs or the
other servers. A malformed attempted configuration also withholds them. Existing
behavior is unchanged only when continuity has not been configured at all.

## Explicit room awareness

Every incoming message Colin stores or answers includes a `[ROOM_CONTEXT]` block.
This tells him where he is answering from, not only who he is answering. The block
includes guild ID/name, channel ID/name, DM status, room mode, room label, label
source, and a short privacy note.

Room labels are trusted configuration, not guesses from who is present. Channel
labels override guild labels. DMs automatically use `room_mode: private_dm`.
Unconfigured guild channels use `room_mode: unknown` and `room_label: unknown`,
so Colin does not assume the space is private or public from vibes.

Supported room modes are:

- `private_home`
- `private_dm`
- `public_community`
- `semi_private_group`
- `unknown`

Configure labels with semicolon-separated entries:

```text
ROOM_CONTEXT_GUILD_LABELS=123456789012345678:public_community:The Nest
ROOM_CONTEXT_CHANNEL_LABELS=234567890123456789:private_home:Cottage Home;345678901234567890:public_community:public banter
```

Each entry uses:

```text
discord_id:room_mode:Room Label
```

For example, a Cottage channel can be explicitly labeled `private_home`, while a
Nest channel can be explicitly labeled `public_community`. If no label is
configured, Colin receives `unknown` rather than guessing.

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
4. Mount a Railway persistent volume at the service's `data/` directory. The
   continuity ledger is SQLite; without a persistent volume, a redeploy may
   discard its timeline.
5. Set the start command to:

```bash
python -m src.main
```

6. Deploy.

## Notes

- This is a first skeleton, not the final architecture.
- SQLite is fine to start, but Postgres is a better long-term next step.
- The identity bundle lives in `src/identity.py`.
- The Discord behavior lives in `src/main.py`.
- The model call lives in `src/router.py`.
- The saved memory logic lives in `src/memory.py`.
