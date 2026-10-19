# Room of Becoming backlog

## RB-OWN-001 — Guard shared owner-ID parsing without weakening fail-closed authorization

Status: recorded; not implemented by the Room-specific repair commit.

The inherited shared `BOT_OWNER_DISCORD_ID` startup parser can fail on malformed
configuration before optional Room startup. This is broader than Room command
registration, storage, SDK provenance or schema-integrity repairs and is deferred
to its own scoped change.

The future correction must distinguish absent configuration from malformed
configuration. A malformed owner ID must never be interpreted as an absent owner
or unrestricted access. Preserve fail-closed authorization for the Room and every
existing owner-gated operation, with explicit regression tests for malformed,
missing and valid owner IDs.

Recording this item does not authorize changing shared owner parsing, identity,
memory, journal, continuity, Paintbox or status behavior in the current repairs.
