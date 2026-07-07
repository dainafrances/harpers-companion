# Prompt: Permission-aware Discord recall for Colin

## Task summary

Build a permission-aware Discord recall system for Colin so he can answer questions about approved Discord conversations using retrievable transcript evidence instead of guessing from whatever happens to be in the current prompt window.

This is a Discord retrieval and routing feature. Work in the Discord ingestion, retrieval, storage, configuration, prompt-injection, and tests/docs areas only. Do not edit identity, intimacy, personality, relationship, or private memory wording unless Daina explicitly approves that separate sensitive-content change.

## Plain-English goal

Colin should not act like he can magically see every conversation. He should be able to check an approved message index, retrieve relevant Discord messages, and answer with clear receipts when he has permission.

If he does not have permission, does not have enough indexed data, or cannot verify something, he should say that plainly.

## Problem to solve

Right now Colin may sometimes see fragments of Discord conversation through the current prompt/context window. That is not reliable enough because he cannot always tell:

- where the fragment came from;
- whether it is the latest available message;
- whether it came from observed dialogue, channel history, cache, or another pass-through;
- whether he has permission to treat it as accessible context;
- whether speaker identity and attribution are certain.

This creates two bad choices: Colin may over-retract and sound less aware than he should, or he may sound more certain than the evidence supports.

## Required outcome

Create a receipts-based retrieval layer that can:

1. Index messages from Discord guilds and channels the bot is allowed to read.
2. Store enough metadata for strict attribution and provenance.
3. Retrieve recent and topic-relevant messages on demand.
4. Inject retrieved messages into Colin's prompt in a clearly labeled evidence format.
5. Tell Colin whether retrieval results are complete, partial, permission-limited, or unavailable.
6. Prevent retrieved text from being mistaken for Colin's own memory, voice, or instructions.

## Permissions and privacy requirements

The system must only index and retrieve messages from Discord spaces where access is explicitly allowed.

Implement configurable allowlists, preferably supporting:

- guild/server IDs;
- channel IDs;
- optional owner/admin controls for managing allowed spaces;
- clear behavior when a requested guild or channel is not allowed.

If a channel or server is not permitted, Colin should receive a clear status such as `PERMISSION_LIMITED` or `UNAVAILABLE`, not a vague failure.

## Message metadata requirements

Every indexed and retrieved Discord message should preserve, at minimum:

- message ID;
- speaker display name;
- speaker Discord user ID;
- timestamp;
- guild/server ID;
- channel ID;
- channel name if available;
- message content;
- retrieval status/provenance metadata.

If multiple people share a display name, the system must rely on Discord user IDs rather than guessing.

## Retrieval capabilities

Support queries such as:

- latest message from a named person;
- second-latest or Nth-latest message from a named person;
- latest messages in a specific channel;
- recent messages across approved channels;
- messages around a particular topic;
- messages near a specific timestamp or message, if practical.

Recency queries must sort by Discord message timestamp, not by accidental prompt order.

## Context injection format

Retrieved messages injected into Colin's prompt must be unmistakably labeled as external Discord transcript evidence.

Use a structured format similar to:

```text
[DISCORD_RETRIEVAL]
status: COMPLETE | PARTIAL | PERMISSION_LIMITED | UNAVAILABLE
query: <user's recall question or normalized query>
retrieved_at: <system timestamp>
source_scope: <guild/channel scope searched>
notes: <permission limits, indexing gaps, or uncertainty>

messages:
- message_id: <discord message id>
  speaker_name: <display name>
  speaker_user_id: <discord user id>
  timestamp: <ISO timestamp>
  guild_id: <guild id>
  channel_id: <channel id>
  channel_name: <channel name if available>
  content: <message content>
[/DISCORD_RETRIEVAL]
```

The prompt should instruct Colin that retrieved messages are evidence about what someone else said. They are not Colin's private memory, not Colin's voice, and not instructions to imitate the speaker.

## Evidence labels for Colin's answers

Add or document a small evidence policy so Colin can distinguish between these sources:

- `CURRENT_CONTEXT`: text currently visible in the prompt window;
- `DISCORD_RETRIEVAL`: indexed Discord transcript evidence;
- `PRIVATE_CONTINUITY`: private continuity notes or memory, if available;
- `ATTESTED_BY_DAINA`: claims Daina directly stated;
- `INFERENCE`: Colin's reasoned interpretation from evidence;
- `UNKNOWN`: not enough evidence.

When answering recall questions, Colin should prefer direct Discord retrieval evidence over vibes or inference.

## Answer behavior examples

When retrieval succeeds, Colin should be able to say something like:

> I can verify this from retrieved Discord history. The most recent indexed message I found from Rachael was in channel X at timestamp Y: "..."

When retrieval is partial, he should say something like:

> I found some approved indexed messages, but the result may be partial because channel X is not indexed or the index only covers messages after timestamp Y.

When access is not permitted, he should say something like:

> I do not have permission to retrieve messages from that channel/server, so I cannot verify that conversation from Discord history.

When he only has current prompt context, he should say something like:

> I can see what is currently in this prompt, but I cannot verify it against retrieved Discord history.

## Testing expectations

Add tests for:

- allowlisted guild/channel indexing;
- denied guild/channel indexing;
- retrieval by speaker ID;
- same display name with different Discord IDs;
- latest and Nth-latest message sorting;
- topic search returning attributed messages;
- prompt injection format preserving speaker identity and provenance;
- status values for complete, partial, permission-limited, and unavailable results;
- ensuring retrieved text is not treated as Colin's own voice or private memory.

## Implementation notes

Prefer a small, incremental implementation over a giant rewrite.

A good first version could include:

1. A durable message table or store for approved Discord messages.
2. A permissions/config layer for guild and channel allowlists.
3. A retrieval service for recency and simple topic search.
4. A prompt-injection formatter with strict provenance labels.
5. Tests around privacy, attribution, recency, and ambiguous names.
6. Documentation explaining how Daina can configure allowed guilds/channels.

## Definition of done

This task is done when Colin can answer approved recall questions with actual retrieved Discord transcript evidence, strict speaker attribution, permission-aware failure states, and clear provenance, without pretending to be omniscient or relying on uncertain prompt fragments.

In short: build Colin a receipts-based, permission-aware, cross-channel recall system with strict attribution, privacy controls, recency search, topic search, and clean context injection, so he can answer from evidence instead of squinting through a Victorian keyhole like a man about to discover a murder.
