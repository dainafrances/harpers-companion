from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

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
            """
        )


def save_message(*, channel_id: int, user_id: int, role: str, content: str, source: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO messages (channel_id, user_id, role, content, source)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(channel_id), str(user_id), role, content, source),
        )


def get_recent_messages(*, channel_id: int, limit: int = 12) -> list[dict[str, str]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT role, content
            FROM messages
            WHERE channel_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (str(channel_id), limit),
        ).fetchall()

    rows = list(reversed(rows))
    return [{"role": row["role"], "content": row["content"]} for row in rows]


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
