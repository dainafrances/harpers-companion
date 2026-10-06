# Colin and Ben in The Cabin bedroom

This routing feature lets either bot deliberately invite the other into a short
exchange in exactly guild `1489462985897279631`, channel
`1497494466712440912`. Other rooms retain their existing rules.

## Behaviour

- An intentional `@Ben` / `[PING: Ben]`, or `@Colin` /
  `[PING: Colin]`, starts an exchange when chosen in a reply to Daina's current
  message. Mentioning a name without an @ does not invite a response.
- The first invitation is turn 1. The peer's answer is turn 2. An answer back is
  turn 3. Either bot can start. Three is the shared maximum, never three per bot.
- One complete reply is one turn. Its single live peer mention goes in its final
  Discord chunk, after all preceding text has been sent.
- A peer answer is triggered only by a direct tag from the exact other bot's
  Discord ID. Reply metadata, @everyone, @here, third-party bots and bot commands
  do not trigger bedroom exchanges.
- The third reply carries no live peer ping. Any extra attempt is rejected before
  a model call.
- Every message from Daina in this room resets the allowance, whether addressed
  to a bot or not. Other people, other rooms, time passing and restarts do not.
- If Daina posts while a model is answering, the old grant is invalidated.
  Stale replies and their reactions are cancelled before delivery.
- If coordination fails, automated responses stop. Human-requested text still
  sends, with companion pings suppressed.
- A failed or cancelled attempt keeps its spent slot until Daina's next message;
  the limit is conservative rather than retrying uncertain deliveries.

## Shared counter

Ben hosts the authenticated counter on his existing HTTP service at
`POST /internal/bedroom-exchange`. Colin connects over Railway's private
network. No new service or public domain is required.

The counter uses a **separate** SQLite database on Ben's existing volume.
Transactions reserve each slot atomically. It stores Discord IDs, opaque grant
IDs and counts only; no conversation text, credentials or relationship data.
All chunks share one grant, and a published turn can have only one successor.
On startup the counter reads the most recent Daina message among the latest
100 room messages; an already recorded message never refills the allowance.

The existing identity files, system-prompt wording, memory schemas, continuity
rules and archive are preserved. Ordinary memory recording uses the existing
paths.

## Deployment

Deploy the matching branches in **both** repositories. The feature defaults to
disabled and requires the following settings before activation:

| Service | Variable | Value |
| --- | --- | --- |
| Both | BEDROOM_EXCHANGE_ENABLED | true |
| Both | BEDROOM_EXCHANGE_SECRET | The same generated secret, at least 32 characters |
| Ben | BEDROOM_EXCHANGE_DB_PATH | /data/bedroom_exchange.sqlite3 |
| Colin | BEDROOM_COORDINATOR_URL | http://${{Ben.RAILWAY_PRIVATE_DOMAIN}}:${{Ben.PORT}} |

Keep the secret exclusively in Railway variables; never put its real value in
the repository, examples, logs or PR discussion.

Ben's existing `COLIN_DISCORD_USER_ID` identifies Colin; Ben's own ID is read from
his logged-in Discord account. Ben's `DAINA_USER_ID` must match Colin's existing
`BOT_OWNER_DISCORD_ID`. Profiles with an owner mismatch are rejected.

Keep Ben at one replica with his existing persistent `/data` volume. The counter
path must not be either bot's memory or continuity database. The HTTP server
listens on both IPv4 and IPv6; the existing `/` health check remains available.
Both bots need their existing room access and Read Message History permission.

Verify both deployments and the counter's `shared_three_turn_counter_ready`
startup log before exercising the feature. Configuration is staged separately
from code; apply the staged changes only with explicit deployment approval.

## Acceptance check

1. Post in #the-bedroom to open a fresh allowance.
2. Have Colin deliberately ask Ben a question with a direct tag. Ben may answer
   and tag Colin with a follow-up; Colin's third reply must not ping Ben again.
3. Confirm that extra peer tags do not cause a fourth model response.
4. Post any unaddressed message as Daina in the same room; a fresh exchange is
   allowed. Repeat with Ben starting.
5. Restart either bot without posting as Daina; its spent allowance must persist.

Unit tests use invented IDs, questions and keys. They cover concurrent
reservations, restart persistence, stale grants, duplicate and chunk handling,
exact room/peer/owner restrictions, authenticated HTTP, and preserving text and
reactions together. A paired local simulation additionally runs both bots'
actual event and send paths. These checks are separate from live Discord
acceptance.

