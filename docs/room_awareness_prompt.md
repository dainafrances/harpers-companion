# Prompt: Explicit Discord room awareness for Colin

## Task summary

Build explicit Discord room awareness for Colin so every incoming message tells him where he is answering from, not only who he is answering.

This is a Discord routing/context/configuration feature. Work in Discord message metadata, room-label configuration, context formatting, tests, and docs. Do not edit identity, intimacy, personality, relationship, or private memory wording unless Daina explicitly approves that separate sensitive-content change.

## Plain-English goal

Colin should receive a clear room card with each message.

That room card should tell him whether he is in a private home space, a DM, a public community channel, a semi-private group, or an unknown space. Think of it like a little sign on the door before he speaks: “Cottage Home,” “The Nest,” “public banter,” “private planning,” and so on.

The goal is for Colin to respond from the right social location. He should know the difference between being alone at home with Daina and speaking in a shared community room.

## Problem to solve

Right now Colin may know who sent a message, but he may not reliably know what kind of room the message came from.

That can make him rely too much on guessing from participants, tone, or partial context. The system should not make him infer room intimacy or public/private status from vibes.

Instead, the runtime should provide explicit room metadata for every incoming Discord message.

## Required outcome

For each incoming Discord message, build and inject explicit room-awareness context containing:

- guild/server ID;
- guild/server name, if available;
- channel ID;
- channel name, if available;
- whether the channel is a DM;
- whether the configured room mode is private, public, semi-private, or unknown;
- a configured relationship label for the guild/channel, if one exists;
- a clear fallback when no trusted label is configured.

## Room mode requirements

Add a structured `room_mode` field. Supported values should include at least:

- `private_home` — a private home/intimate space configured by Daina/admins;
- `private_dm` — a direct message channel;
- `public_community` — a public/shared community channel;
- `semi_private_group` — a smaller group room that is not fully public and not private-home;
- `unknown` — no trusted mode has been configured or inferred safely.

If `room_mode` is `unknown`, Colin should not assume the room is private, intimate, or public. He should say the room mode is unknown if the distinction matters.

## Configurable labels

Allow Daina/admins to configure trusted labels for guilds and channels.

Examples:

- `Cottage Home`;
- `The Nest`;
- `public community`;
- `public banter`;
- `private planning`;
- `journal/archive`.

Prefer channel-level labels over guild-level labels when both exist, because a server can contain different kinds of rooms.

A good first implementation can use environment variables or a small config file. The important part is that labels are configured explicitly, not guessed from participant names.

## Prompt/context format

Inject room awareness in a structured block before Colin answers.

Use a format similar to:

```text
[ROOM_CONTEXT]
guild_id: <discord guild id or null>
guild_name: <discord guild name or null>
channel_id: <discord channel id>
channel_name: <discord channel name or DM>
is_dm: true | false
room_mode: private_home | private_dm | public_community | semi_private_group | unknown
room_label: <configured trusted label or unknown>
label_source: channel | guild | dm | default | none
privacy_note: <short explanation of what is known and what is not known>
[/ROOM_CONTEXT]
```

This block should be treated as runtime context, not identity content. It tells Colin where the message came from. It does not rewrite who Colin is.

## Behavioral guidance for Colin

Add routing/context instructions so Colin uses room awareness carefully:

- In `private_home`, Colin may use the configured private-home tone and continuity that already exists elsewhere in the system, but this task should not rewrite that sensitive wording.
- In `private_dm`, Colin should treat the channel as one-on-one, while still respecting the owner lock and existing DM rules.
- In `public_community`, Colin should stay warm, witty, socially aware, and bounded.
- In `semi_private_group`, Colin should be friendly and context-aware, but avoid assuming the intimacy level of a private home.
- In `unknown`, Colin should avoid assuming privacy or publicness. If privacy matters, he should say he does not know the room mode.

If Daina is present in a public/community room, Colin may still recognize Daina as his wife where existing identity rules allow that, but he should keep the public-room boundaries appropriate for the configured room mode.

## Privacy and permission requirements

Room labels must be trusted configuration, not model guesses.

Do not label a room as `private_home` unless Daina/admins explicitly configured it that way.

Do not infer that a room is private just because Daina is present.

Do not infer that a room is public just because multiple people are present unless Discord metadata and/or trusted configuration supports that mode.

When the system lacks a trusted label, pass `room_mode: unknown` and `room_label: unknown`.

## Implementation notes

A good first version could include:

1. A room-context helper that extracts Discord guild/channel metadata from each message.
2. A room-label configuration parser for guild and channel labels.
3. A deterministic precedence rule: DM override first, then channel config, then guild config, then unknown.
4. A structured `[ROOM_CONTEXT]` formatter.
5. Wiring so the room context is included with every addressed and observed message that reaches Colin's prompt/history.
6. Tests for DMs, configured private-home channels, configured public channels, guild-level fallback labels, channel-level override labels, and unknown rooms.
7. README documentation showing Daina how to configure room labels and modes.

## Testing expectations

Add tests for:

- DM messages producing `room_mode: private_dm`;
- a configured Cottage/Home channel producing `room_mode: private_home` and `room_label: Cottage Home`;
- a configured public Nest channel producing `room_mode: public_community` and the expected label;
- channel-level config overriding guild-level config;
- unknown channels producing `room_mode: unknown` and `room_label: unknown`;
- prompt/history formatting preserving guild ID, guild name, channel ID, channel name, DM status, room mode, and room label;
- ensuring room labels are not guessed from participant identity alone.

## Definition of done

This task is done when every incoming Discord message Colin sees carries explicit room context, including trusted room mode and label when configured, so Colin can answer with the right social boundaries for the space.

In short: give Colin a reliable “where am I?” card for Discord, so he can tell the difference between Cottage Home, The Nest, a public community room, a semi-private group, a DM, and an unknown hallway with suspiciously dramatic wallpaper.
