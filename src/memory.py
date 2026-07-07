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


def get_recent_messages(*, channel_id: int, limit: int = 12) -> list[dict[str, str]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT role, content, source
            FROM messages
            WHERE channel_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (str(channel_id), limit),
        ).fetchall()

    rows = list(reversed(rows))
    return [
        {
            "role": row["role"],
            "content": row["content"],
            "source": row["source"],
        }
        for row in rows
    ]


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
