# The Room of Becoming — version 1 contract

This contract defines the Phase 1 implementation boundary. It does not certify a
production deployment, successful live Discord verification, or a passing test
run. Those require separate evidence.

## Purpose

The Room is an inspectable, durable workspace for entries created during real
invocations. It preserves what was recorded, who authored it, the invocation that
produced it, and how later revisions changed it. Phase 1 implements one entry
kind, `want`, through explicit private slash commands.

The broader concept includes wants, wonderings, decisions, memory candidates,
heartbeat receipts and identity-integrity test results. Only wants and the
invocation receipts needed by their operations are in this first slice.

## Non-purpose

The Room does not fabricate off-screen thoughts, imply continuous consciousness,
or record an invented event as an actual invocation. It is not canonical memory,
an identity editor, a replacement for the legacy journal, or a shared transcript.
It does not silently promote entries into memory or overwrite changed decisions.

Phase 1 has no scheduled invocations, ordinary-conversation capture, annotations,
status-change command, hard-delete command, or identity-integrity evaluation.
No production entry is seeded by initialization, migration, or fixtures.

## Authorship

Supported actor classifications are:

| Actor | Meaning |
| --- | --- |
| `colin_model` | Content authored by the model during the attributed real invocation. |
| `daina_human` | Content explicitly authored by Daina. The schema allows later attributed annotations; Phase 1 provides no annotation command. |
| `system` | Application-generated operational records, such as receipts, without attributing them to Colin or Daina. |

The owner who triggered a request and the author of its content are separate
facts. A model-authored entry cannot be silently replaced by human-authored
wording. Changes require an attributed appended revision; human annotations, if
implemented later, must retain their own authorship.

## Private authorization

The Room is private to Daina and Colin. `BECOMING_ENABLED` defaults to `false`.
Every command independently checks all required configuration and the caller's
identity before reading private records or making a provider request.

- `BOT_OWNER_DISCORD_ID` is required and must match the calling user exactly.
- `BECOMING_CHANNEL_ID` is required and must match the current channel exactly.
- `BECOMING_GUILD_ID`, when present, must be a valid positive Discord ID and must match the current guild exactly.
- With no guild ID configured, the route must be an actual one-to-one bot DM in the configured channel. A group DM or guild channel is denied.
- Missing, malformed or invalid privacy configuration denies access.
- Guild responses are ephemeral. All Room responses disable mentions.

The ordinary message-routing gate is not authorization for these slash commands.
Discord channel permissions remain an additional deployment concern. An ephemeral
guild response is not a substitute for the owner and exact-route checks.

The inherited shared `BOT_OWNER_DISCORD_ID` startup parser is outside the Room
repair scope. [RB-OWN-001](BACKLOG.md) records its future correction without
weakening authorization: malformed configuration must never mean an absent owner
or unrestricted access.

## Optional registration and startup

Room definitions must not enter the first legacy guild/global command sync. There
is no import-time or disabled/invalid-configuration Room registration, and no
Room-specific sync in those states.

Ordinary command startup is independent. Existing journal and status timers can
start before separately guarded optional Room registration/synchronization. A
Room registration or sync failure is caught and reduced to bounded diagnostics;
it cannot interrupt ordinary companion operation or be marked as a successful
Room sync. Registration is idempotent, successful reconnects do not duplicate
commands or timer starts, and an unsuccessful Room-only sync may be retried on a
later reconnect.

For a guild route, Room commands sync only to that configured guild and are not
published globally. For a DM route, the separate Room global sync preserves
already-working ordinary global command definitions. Its failure cannot undo or
interrupt commands that were already operating. Command availability never grants
entry access; the exact owner/route checks still apply on every invocation.

## Persistence and schema version

The dedicated database defaults to
`data/room-of-becoming/room_of_becoming.sqlite3` and is configured by
`BECOMING_DB_PATH`. It must not reuse `colin_memory.sqlite3`, Paintbox's usage
database, or another application's database. Initialization must reject a known
application-path collision and an existing database with a conflicting marker or
schema rather than adopting or modifying it.

All accepted in-repository storage lives within the one dedicated private
directory `data/room-of-becoming/`. Git ignores that entire directory, including
nested paths, extensionless files, SQLite sidecars and exports. Validation resolves
and checks paths before opening SQLite. Repository-root files and other
in-repository directories are rejected regardless of filename suffix or other
ignore patterns. Traversal attempts, symlink escapes and paths whose SQLite
sidecars leave the protected directory are rejected before provider invocation or
private-data creation. Invalid old custom paths fail closed rather than being
silently moved or adopted.

The old default `data/room_of_becoming.sqlite3` is not discovered automatically
by the new default. An existing installation requires operator confirmation of
the actual database, a consistent backup and explicit authority for any
relocation before enabling this configuration. This repair does not migrate
existing storage or silently start using an old database at a new location.

Genuine paths outside the repository may be used after application-database,
symlink, hardlink and sidecar collision protections. The application does not run
Git at normal runtime; it enforces the storage boundary itself.

The database uses SQLite `PRAGMA application_id=0x52423031` (`RB01`) and sets
`PRAGMA user_version=1`. Unsupported versions fail closed. Required tables,
indexes and triggers must match deterministic normalized schema definitions, not
only object names. Ignore only legitimate SQLite internal objects; missing,
altered or unexpected application objects fail closed, including a same-named
but ineffective immutability trigger. A valid database initializes idempotently.
Rejection does not silently repair, migrate, recreate or overwrite definitions,
bytes or stored user data. Future schema upgrades require an explicit migration
rather than relying only on `CREATE TABLE IF NOT EXISTS`.

The separate tables are `invocations`, `entries` and `revisions`:

| Record | Required fields and meaning |
| --- | --- |
| Invocation | Stable invocation ID; unique Discord interaction/operation key; trigger type; actor; owner/user ID; optional guild ID; channel ID; start and completion timestamps; outcome; requested model; returned model/provider and response ID when supplied; prompt version/digest; bounded error category. |
| Entry | Stable entry ID; kind (`want` initially); author; private workspace scope; creation timestamp; current revision number; status. |
| Revision | Entry ID; revision number; parent revision; title; exact content; revision reason; author; invocation reference; timestamp. |

Timestamps describe recorded events. They do not claim activity between
invocations. Private entry content stays in the private database rather than
configuration, source code, log messages or Git history.

## Provenance and honest receipts

Authorization IDs and operation keys come from trusted application context, not
model output. Record the model requested and retain provider response metadata
when available. Unavailable returned model, provider or response metadata is
`UNKNOWN`; never infer it from the requested model's name.

SDK failures expose HTTP responses and structured error bodies rather than
completion objects. Only explicitly allowlisted returned model, provider and
response/request-ID fields from known structured locations may be retained.
Values must have bounded lengths and valid types; absent or invalid values remain
`UNKNOWN`. Do not retain or log the raw HTTP body, arbitrary provider error text,
private prompts, seeds, revisions, entry text or model output. Retaining metadata
does not turn a provider failure into a successful invocation or create an entry.

Store a prompt version or digest rather than raw private prompts. Do not request
or store private chain-of-thought. Logging may record bounded operational/error
categories and identifiers; it must not include seeds, instructions, entry text,
raw provider responses, or exception strings that can contain private content.

Completed invocation outcomes are `created`, `revised`, `abstained`, `denied`,
and `failed`. A reservation may remain pending with no completed outcome while an
operation is in progress. A crash can leave it incomplete with the provider
result `UNKNOWN`; pending is not success, abstention, or proof that no request was
sent. Do not automatically make a paid retry for that operation.

Reserve the unique operation key before invoking the model. Duplicate delivery
must reuse the known receipt/result or report the operation as incomplete; it
must not create a duplicate entry or a second automatic provider request.
Unauthorized requests are denied before accessing private records or provider
services; they must not initialize or expose the private workspace to the caller.

## Structured model requests and abstention

Room requests use the existing provider transport, with strict structured output
and no tools. Creation supplies the unchanged existing identity bundle and the
explicit command seed/context. It does not automatically attach conversation
history, legacy journals, recalled messages, or other Room entries.

Creation must produce either one want with title and content or an explicit
abstention with a short user-visible reason. Revision supplies the current stored
revision and explicit revision instruction, and produces either one new revision
or an abstention. A want is not mandatory merely because the command ran.

Abstention writes an honest invocation receipt and no new entry or revision.
Malformed output and provider failure produce no entry and do not claim success.
Storage failure must never be presented as a successfully saved entry. Failure
reporting must not leak private content. If storage itself is unavailable, a
receipt cannot be promised.

## Revisions and statuses

Each entry has a stable ID. Revision 1 has no parent. Every later revision names
its immediately preceding revision and the invocation that authored the change.
Preserve all earlier title/content/reason records exactly. Database triggers
reject revision updates/deletes and updates/deletes of finalized receipts.
Retrieval of current content follows the entry's current revision; history lists every revision in
chronological order with authorship and reason.

Revision requests include the expected current revision number. A stale value or
competing update fails cleanly instead of replacing another change. Recheck the
expected revision in the write transaction, even if it was checked before the
provider request. An invocation and its resulting entry/revision must be linked
in an all-or-nothing transaction. A failed write cannot leave a success receipt
or a partial entry/revision association.

| Status | Meaning |
| --- | --- |
| `active` | The entry is current and has not been marked completed or withdrawn. New wants begin here. |
| `fulfilled` | The want was explicitly marked fulfilled; this does not erase its history. |
| `retired` | The entry was explicitly withdrawn from active use without deleting prior records. |
| `superseded` | Another explicit entry or decision replaces it; the earlier record remains inspectable. |

The schema supports these statuses. Phase 1 does not infer status transitions or
provide a Discord command to change them. Revision alone does not imply
fulfillment, withdrawal, or supersession.

## Explicit exclusion from canonical memory

Room content must not enter `build_memory_note()`, legacy `journal_entries`,
ordinary message history, Discord recall, continuity events, public status
generation, Paintbox, or identity construction. Ordinary chats must not gain
automatic Room retrieval. The legacy nightly journal and periodic status behavior
remain unchanged.

Supplying the existing identity bundle to a dedicated Room model request does not
modify the identity bundle. Room text is attributed input to that operation, not
a system instruction or an identity amendment. Phase 1 has no promotion path into
canonical memory. A future promotion workflow requires its own explicit review
and authority.

## Commands

| Command | Result |
| --- | --- |
| `/becoming_want [seed]` | One real invocation; creates one want or records abstention. |
| `/becoming_show entry_id` | Current title/content, status, revision number, timestamp and bounded provenance. |
| `/becoming_revise entry_id expected_revision instruction` | One real invocation; appends an attributed revision or records abstention. |
| `/becoming_history entry_id` | All revisions, in order, with authorship and revision reasons. |

Responses must be truthful about completion, abstention, failure, conflict and
incomplete operations. Display limits may split long responses, but must not
silently omit or rewrite stored history. Entry lookup is scoped to the configured
private workspace. Private page controls expire after 14 minutes. Rerun
`/becoming_show` or `/becoming_history` to open a fresh view; expiry does not
truncate, delete or change the stored content or revision history.

## Future first real entry

After review and deployment, the first real want comes from `/becoming_want`
through an actual invocation, using the seed supplied privately in the
implementation packet. No real seed or private entry content belongs in Git,
fixtures, or migrations. This contract does not create an entry or establish that
an invocation has occurred.

## Durability, backups and private operation

Persist the dedicated database on an approved private volume. Restrict filesystem
and backup access to the operating account and authorized operators. Environment
secrets remain in deployment configuration, never in the database or Git. The
repository is public; never commit runtime Room entries, private exports,
credentials or database files, including SQLite WAL/shared-memory/journal
sidecars. Every accepted in-repository Room path is inside the dedicated ignored
directory; externally configured paths and their backups still need private
operator-controlled permissions and retention.

A persistent volume is not a backup. An operator-managed backup must use SQLite's
backup API or a consistent stopped-writer snapshot. Copying only a live main file
can miss committed WAL data. Protect backups and exports with the same privacy
boundary as the database, and define a retention policy before production use.
This contract describes the process; it does not execute a backup or inspect
production data.

Restore into a stopped, isolated instance; verify the application marker, schema
version, configured path, current entries and complete revision history before
resuming writes. A restore test is separate evidence from persistence after a
restart. Do not point a test process at the production database.

## Phase 1 acceptance criteria

Automated tests must cover at least:

1. Schema initialization, application marker and explicit version `1`.
2. Create/retrieve, and retrieve from a fresh process or connection after restart.
3. Revision 2 preserves revision 1 verbatim, including provenance and authorship.
4. Duplicate operation keys do not duplicate entries or repeat a paid request.
5. Stale expected revisions are rejected, including a concurrent update during a provider request.
6. Transaction rollback prevents partial entries, revisions or success receipts.
7. Abstention creates an invocation receipt and no entry/revision.
8. Malformed model output and provider failure create no entry and do not claim success.
9. Storage failure does not claim success or promise an unwritten receipt.
10. An unauthorized owner, channel or guild is denied, and missing/invalid privacy configuration fails closed.
11. No-guild configuration accepts only the exact approved one-to-one DM; group DMs are denied.
12. Guild responses are ephemeral and every Room response disables mentions.
13. Ordinary memory, journal, continuity, identity, status, recall and image paths remain untouched by Room operations.
14. A configured path colliding with known application databases is rejected.
15. Only protected-directory in-repository paths are accepted, including safe nested/extensionless paths. Root/outside-directory paths, traversal, symlink escapes and unsafe sidecars are rejected before artifact creation; a genuine external temporary path is accepted. No stageable database artifacts remain after tests.
16. Optional Room definitions never enter the initial legacy sync. Disabled/invalid configuration adds none. Guild-only and DM/global paths, initial legacy rejection, Room-only rejection, reconnect retries and successful reconnects preserve ordinary commands and single timer starts.
17. Actual provider SDK exceptions with mock HTTP transport retain only allowlisted structured error metadata and still produce no entry.
18. Same-named no-op update/delete triggers, changed table/index definitions, missing objects and unexpected objects are rejected without changing existing database bytes/data. Valid initialization remains repeatable, and older revisions remain immutable through normal APIs.

For accepted repairs, run Room tests, the full suite, the shuffled full suite,
`compileall`, `pip check`, whitespace/diff checks and each original adversarial
reproduction. Report exact commands/results and whether each original finding can
still reproduce; aggregate test counts alone are insufficient. Test source and
passing mocked tests do not establish a production deployment or live Discord
behavior.

The synthetic end-to-end check uses invented content with a disposable protected
or external database and no production application, provider credentials,
Railway environment or database. When explicitly non-production isolated
credentials are unavailable, use actual `discord.py` command objects and the
actual provider SDK with deterministic mock transports. Exercise disabled then
enabled startup, create/show/revise/history, fresh-process persistence,
deduplication, stale revision, abstention, malformed/provider/storage failures,
owner/route denial, invalid paths, optional sync failure and legacy-boundary
inspection. Clean up synthetic artifacts. Live network behavior remains
**NOT VERIFIED** unless it is actually exercised with authorized isolated
credentials.

Local manual verification must use a separate test Discord application and a
fresh temporary absolute `BECOMING_DB_PATH`, with isolated ordinary data as well.
Never use a production token, default persistent database, or production data for
that disposable check. The commands can make real paid provider requests; unit
tests use mocks instead. A test-process restart uses the same temporary path to
verify persistence without touching production.

After deployment is separately authorized, a manual smoke test must use the
configured owner and exact private route to create, show, revise and inspect an
entry, then retrieve it after a process restart. Check denied routes separately
without disclosing private entry text. Phase 1 implementation authority does not
authorize Railway deployment or production database initialization/inspection.
