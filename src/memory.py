from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path("data/colin_memory.sqlite3")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS journal_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS processed_discord_messages (
                message_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                author_id TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS discord_recall_messages (
                message_id TEXT PRIMARY KEY,
                guild_id TEXT,
                channel_id TEXT NOT NULL,
                channel_name TEXT,
                speaker_user_id TEXT NOT NULL,
                speaker_name TEXT NOT NULL,
                content TEXT NOT NULL,
                message_timestamp TEXT NOT NULL,
                source TEXT NOT NULL,
                indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_recall_recency
            ON discord_recall_messages (message_timestamp DESC);

            CREATE INDEX IF NOT EXISTS idx_recall_speaker
            ON discord_recall_messages (speaker_user_id, speaker_name, message_timestamp DESC);

            CREATE INDEX IF NOT EXISTS idx_recall_scope
            ON discord_recall_messages (guild_id, channel_id, message_timestamp DESC);

            CREATE TABLE IF NOT EXISTS continuity_events (
                event_id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                guild_name TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                channel_name TEXT NOT NULL,
                continuity_zone TEXT NOT NULL,
                speaker_user_id TEXT NOT NULL,
                speaker_name TEXT NOT NULL,
                speaker_is_bot INTEGER NOT NULL CHECK (speaker_is_bot IN (0, 1)),
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                event_timestamp TEXT NOT NULL,
                source TEXT NOT NULL,
                indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_continuity_channel_chronology
            ON continuity_events (guild_id, channel_id, event_timestamp DESC, event_id DESC);

            CREATE INDEX IF NOT EXISTS idx_continuity_speaker_chronology
            ON continuity_events (speaker_user_id, event_timestamp DESC, event_id DESC);

            CREATE INDEX IF NOT EXISTS idx_continuity_zone_chronology
            ON continuity_events (continuity_zone, event_timestamp DESC, event_id DESC);
            """
        )


def try_claim_discord_message(*, message_id: int, channel_id: int, author_id: int, source: str) -> bool:
    """
    Mark an incoming Discord message as handled.

    Returns False if this exact Discord message was already claimed, which protects
    against duplicate gateway delivery or two handlers racing in the same process.
    """
    with connect() as conn:
        try:
            conn.execute(
                """
                INSERT INTO processed_discord_messages (message_id, channel_id, author_id, source)
                VALUES (?, ?, ?, ?)
                """,
                (str(message_id), str(channel_id), str(author_id), source),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def save_message(*, channel_id: int, user_id: int, role: str, content: str, source: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO messages (channel_id, user_id, role, content, source)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(channel_id), str(user_id), role, content, source),
        )


def save_recall_message(
    *,
    message_id: str,
    guild_id: str | None,
    channel_id: str,
    channel_name: str | None,
    speaker_user_id: str,
    speaker_name: str,
    content: str,
    message_timestamp: str,
    source: str,
) -> bool:
    with connect() as conn:
        try:
            conn.execute(
                """
                INSERT INTO discord_recall_messages (
                    message_id,
                    guild_id,
                    channel_id,
                    channel_name,
                    speaker_user_id,
                    speaker_name,
                    content,
                    message_timestamp,
                    source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    guild_id,
                    channel_id,
                    channel_name,
                    speaker_user_id,
                    speaker_name,
                    content,
                    message_timestamp,
                    source,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def save_continuity_event(
    *,
    event_id: str,
    guild_id: str,
    guild_name: str,
    channel_id: str,
    channel_name: str,
    continuity_zone: str,
    speaker_user_id: str,
    speaker_name: str,
    speaker_is_bot: bool,
    role: str,
    content: str,
    event_timestamp: str,
    source: str,
) -> bool:
    """Save one observed Discord event without rewriting an existing event."""
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO continuity_events (
                event_id,
                guild_id,
                guild_name,
                channel_id,
                channel_name,
                continuity_zone,
                speaker_user_id,
                speaker_name,
                speaker_is_bot,
                role,
                content,
                event_timestamp,
                source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO NOTHING
            """,
            (
                event_id,
                guild_id,
                guild_name,
                channel_id,
                channel_name,
                continuity_zone,
                speaker_user_id,
                speaker_name,
                int(speaker_is_bot),
                role,
                content,
                event_timestamp,
                source,
            ),
        )
    return cursor.rowcount == 1


def _continuity_event_from_row(row: sqlite3.Row) -> dict[str, str | bool]:
    event = dict(row)
    event["speaker_is_bot"] = bool(event["speaker_is_bot"])
    return event


def get_latest_continuity_event_in_channel_before(
    *,
    guild_id: str,
    channel_id: str,
    before_timestamp: str,
) -> dict[str, str | bool] | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                event_id,
                guild_id,
                guild_name,
                channel_id,
                channel_name,
                continuity_zone,
                speaker_user_id,
                speaker_name,
                speaker_is_bot,
                role,
                content,
                event_timestamp,
                source,
                indexed_at
            FROM continuity_events
            WHERE guild_id = ?
              AND channel_id = ?
              AND event_timestamp < ?
            ORDER BY event_timestamp DESC, event_id DESC
            LIMIT 1
            """,
            (guild_id, channel_id, before_timestamp),
        ).fetchone()

    if row is None:
        return None
    return _continuity_event_from_row(row)


def get_latest_continuity_event_by_speaker_before(
    *,
    speaker_user_id: str,
    before_timestamp: str,
    exclude_guild_id: str | None = None,
    exclude_channel_id: str | None = None,
) -> dict[str, str | bool] | None:
    exclusion_sql = ""
    params: list[object] = [speaker_user_id, before_timestamp]
    if exclude_guild_id is not None or exclude_channel_id is not None:
        if exclude_guild_id is None or exclude_channel_id is None:
            raise ValueError("Both excluded guild and channel IDs are required together.")
        exclusion_sql = "AND NOT (guild_id = ? AND channel_id = ?)"
        params.extend([exclude_guild_id, exclude_channel_id])

    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT
                event_id,
                guild_id,
                guild_name,
                channel_id,
                channel_name,
                continuity_zone,
                speaker_user_id,
                speaker_name,
                speaker_is_bot,
                role,
                content,
                event_timestamp,
                source,
                indexed_at
            FROM continuity_events
            WHERE speaker_user_id = ?
              AND event_timestamp < ?
              {exclusion_sql}
            ORDER BY event_timestamp DESC, event_id DESC
            LIMIT 1
            """,
            params,
        ).fetchone()

    if row is None:
        return None
    return _continuity_event_from_row(row)


def get_recent_continuity_events_from_channel_before(
    *,
    guild_id: str,
    channel_id: str,
    before_timestamp: str,
    limit: int,
) -> list[dict[str, str | bool]]:
    if limit <= 0:
        return []

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                event_id,
                guild_id,
                guild_name,
                channel_id,
                channel_name,
                continuity_zone,
                speaker_user_id,
                speaker_name,
                speaker_is_bot,
                role,
                content,
                event_timestamp,
                source,
                indexed_at
            FROM continuity_events
            WHERE guild_id = ?
              AND channel_id = ?
              AND event_timestamp < ?
            ORDER BY event_timestamp DESC, event_id DESC
            LIMIT ?
            """,
            (guild_id, channel_id, before_timestamp, limit),
        ).fetchall()

    return [_continuity_event_from_row(row) for row in reversed(rows)]


def get_recent_continuity_events_from_guild_before(
    *,
    guild_id: str,
    approved_channel_ids: set[str],
    before_timestamp: str,
    after_timestamp: str,
    limit: int,
    exclude_channel_id: str | None = None,
) -> list[dict[str, str | bool]]:
    """Return a bounded chronological awareness window from approved guild routes."""
    if limit <= 0 or not approved_channel_ids:
        return []

    channel_ids = sorted(approved_channel_ids)
    placeholders = ",".join("?" for _ in channel_ids)
    clauses = [
        "guild_id = ?",
        f"channel_id IN ({placeholders})",
        "event_timestamp < ?",
        "event_timestamp >= ?",
    ]
    params: list[object] = [
        guild_id,
        *channel_ids,
        before_timestamp,
        after_timestamp,
    ]
    if exclude_channel_id is not None:
        clauses.append("channel_id != ?")
        params.append(exclude_channel_id)
    params.append(limit)

    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                event_id,
                guild_id,
                guild_name,
                channel_id,
                channel_name,
                continuity_zone,
                speaker_user_id,
                speaker_name,
                speaker_is_bot,
                role,
                content,
                event_timestamp,
                source,
                indexed_at
            FROM continuity_events
            WHERE {' AND '.join(clauses)}
            ORDER BY event_timestamp DESC, event_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    return [_continuity_event_from_row(row) for row in reversed(rows)]


def search_recall_messages(
    *,
    allowed_guild_ids: set[str] | None = None,
    allowed_channel_ids: set[str] | None = None,
    speaker_user_id: str | None = None,
    speaker_name: str | None = None,
    topic: str | None = None,
    limit: int = 8,
) -> list[dict[str, str]]:
    clauses: list[str] = []
    params: list[object] = []

    if allowed_guild_ids:
        placeholders = ",".join("?" for _ in allowed_guild_ids)
        clauses.append(f"guild_id IN ({placeholders})")
        params.extend(sorted(allowed_guild_ids))

    if allowed_channel_ids:
        placeholders = ",".join("?" for _ in allowed_channel_ids)
        clauses.append(f"channel_id IN ({placeholders})")
        params.extend(sorted(allowed_channel_ids))

    if speaker_user_id:
        clauses.append("speaker_user_id = ?")
        params.append(speaker_user_id)
    elif speaker_name:
        clauses.append("LOWER(speaker_name) = LOWER(?)")
        params.append(speaker_name)

    if topic:
        topic_words = [
            word
            for word in topic.lower().split()
            if len(word) >= 4 and word not in {"what", "when", "where", "latest", "recent", "message", "messages", "conversation"}
        ][:4]
        for word in topic_words:
            clauses.append("LOWER(content) LIKE ?")
            params.append(f"%{word}%")

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                message_id,
                guild_id,
                channel_id,
                channel_name,
                speaker_user_id,
                speaker_name,
                content,
                message_timestamp,
                source
            FROM discord_recall_messages
            {where_sql}
            ORDER BY message_timestamp DESC, message_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    return [dict(row) for row in rows]


def get_recent_messages(
    *,
    channel_id: int,
    limit: int = 12,
    include_created_at: bool = False,
) -> list[dict[str, str]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT role, content, source, created_at
            FROM messages
            WHERE channel_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (str(channel_id), limit),
        ).fetchall()

    rows = list(reversed(rows))
    messages: list[dict[str, str]] = []
    for row in rows:
        message = {
            "role": row["role"],
            "content": row["content"],
            "source": row["source"],
        }
        if include_created_at:
            message["created_at"] = row["created_at"]
        messages.append(message)
    return messages


def save_journal_entry(*, title: str, content: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO journal_entries (title, content)
            VALUES (?, ?)
            """,
            (title, content),
        )


def get_latest_journal_entry() -> str | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT title, content, created_at
            FROM journal_entries
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    if row is None:
        return None

    return f"[{row['created_at']}] {row['title']}: {row['content']}"


def count_messages() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
    return int(row["n"])
