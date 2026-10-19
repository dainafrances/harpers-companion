"""Private, invocation-backed Room of Becoming storage; never canonical memory.

This module has no import-time I/O and imports none of the companion's memory,
identity, retrieval, or Discord integrations. Only explicit Room commands use it.
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from uuid import uuid4

SCHEMA_VERSION = 1
APPLICATION_ID = 0x52423031  # RB01: a separate database, not a legacy schema extension.
PROMPT_VERSION = "room-of-becoming-v1"
UNKNOWN = "UNKNOWN"
ERROR_CATEGORIES = frozenset({
    "storage_failure", "database_collision", "schema_mismatch", "invalid_input",
    "not_found", "stale_revision", "operation_conflict", "operation_incomplete",
    "provider_failure", "malformed_output", "internal_failure", "unauthorized",
})


class RoomError(RuntimeError):
    """Only a bounded category can cross an application error boundary."""

    def __init__(self, category: str):
        self.category = category if category in ERROR_CATEGORIES else "internal_failure"
        super().__init__(self.category)


class RoomModelError(RoomError):
    def __init__(self, category: str, metadata: dict[str, str] | None = None):
        super().__init__(category)
        self.metadata = metadata or {}


@dataclass(frozen=True)
class RoomModelResult:
    action: str
    title: str = ""
    content: str = ""
    reason: str = ""
    requested_model: str = UNKNOWN
    returned_model: str = UNKNOWN
    returned_provider: str = UNKNOWN
    provider_response_id: str = UNKNOWN
    prompt_digest: str = UNKNOWN


def _discord_id(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    if not re.fullmatch(r"[1-9][0-9]{0,19}", text):
        return None
    return text if int(text) < 2**64 else None


@dataclass(frozen=True)
class RoomScope:
    owner_id: str
    channel_id: str
    guild_id: str | None = None

    def values(self) -> tuple[str, str, str | None]:
        if (
            _discord_id(self.owner_id) != self.owner_id
            or _discord_id(self.channel_id) != self.channel_id
            or (self.guild_id is not None and _discord_id(self.guild_id) != self.guild_id)
        ):
            raise RoomError("invalid_input")
        return self.owner_id, self.channel_id, self.guild_id


@dataclass(frozen=True)
class RoomConfig:
    enabled: bool
    owner_id: str | None
    channel_id: str | None
    guild_id: str | None
    db_path: Path
    valid: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RoomConfig:
        env = os.environ if env is None else env
        owner = _discord_id(env.get("BOT_OWNER_DISCORD_ID"))
        channel = _discord_id(env.get("BECOMING_CHANNEL_ID"))
        guild_raw = env.get("BECOMING_GUILD_ID", "").strip()
        guild = _discord_id(guild_raw) if guild_raw else None
        path_raw = env.get("BECOMING_DB_PATH", "data/room_of_becoming.sqlite3").strip()
        enabled = env.get("BECOMING_ENABLED", "false").strip().lower() == "true"
        valid = bool(owner and channel and path_raw and (not guild_raw or guild))
        return cls(enabled, owner, channel, guild, Path(path_raw), valid)

    def authorized(
        self, *, user_id: object, guild_id: object, channel_id: object, is_dm: bool,
    ) -> bool:
        if not self.enabled or not self.valid:
            return False
        if _discord_id(user_id) != self.owner_id or _discord_id(channel_id) != self.channel_id:
            return False
        if self.guild_id is None:
            return is_dm and guild_id is None
        return not is_dm and _discord_id(guild_id) == self.guild_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _metadata(value: object) -> str:
    if isinstance(value, str) and value.strip() and len(value) <= 256:
        if all(ord(char) >= 32 and ord(char) != 127 for char in value):
            return value
    return UNKNOWN


def validate_result(result: RoomModelResult, *, expected_action: str) -> None:
    if not isinstance(result, RoomModelResult) or result.action not in {expected_action, "abstain"}:
        raise RoomModelError("malformed_output")
    for text, limit in ((result.title, 160), (result.content, 6000), (result.reason, 500)):
        if not isinstance(text, str) or len(text) > limit or "\x00" in text:
            raise RoomModelError("malformed_output")
    if result.action == "abstain":
        if result.title or result.content or not result.reason.strip():
            raise RoomModelError("malformed_output")
    elif not result.title.strip() or not result.content.strip():
        raise RoomModelError("malformed_output")
    elif expected_action == "revise" and not result.reason.strip():
        raise RoomModelError("malformed_output")


_SCHEMA = (
    """CREATE TABLE invocations (
        invocation_id TEXT PRIMARY KEY,
        operation_key TEXT NOT NULL UNIQUE,
        trigger_type TEXT NOT NULL CHECK(trigger_type IN ('becoming_want','becoming_revise')),
        actor_classification TEXT NOT NULL CHECK(actor_classification IN ('colin_model','daina_human','system')),
        owner_id TEXT NOT NULL, guild_id TEXT, channel_id TEXT NOT NULL,
        started_at TEXT NOT NULL, completed_at TEXT,
        outcome TEXT CHECK(outcome IN ('created','revised','abstained','denied','failed')),
        requested_model TEXT NOT NULL, returned_model TEXT NOT NULL DEFAULT 'UNKNOWN',
        returned_provider TEXT NOT NULL DEFAULT 'UNKNOWN',
        provider_response_id TEXT NOT NULL DEFAULT 'UNKNOWN',
        prompt_version TEXT NOT NULL, prompt_digest TEXT NOT NULL DEFAULT 'UNKNOWN',
        error_category TEXT, abstention_reason TEXT,
        entry_id TEXT, revision_number INTEGER,
        CHECK((outcome IS NULL AND completed_at IS NULL) OR
              (outcome IS NOT NULL AND completed_at IS NOT NULL))
    )""",
    """CREATE TABLE entries (
        entry_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind = 'want'),
        author_classification TEXT NOT NULL CHECK(author_classification IN ('colin_model','daina_human','system')),
        owner_id TEXT NOT NULL, guild_id TEXT, channel_id TEXT NOT NULL,
        created_at TEXT NOT NULL, current_revision INTEGER NOT NULL CHECK(current_revision >= 1),
        status TEXT NOT NULL CHECK(status IN ('active','fulfilled','retired','superseded')),
        FOREIGN KEY(entry_id,current_revision) REFERENCES revisions(entry_id,revision_number)
            DEFERRABLE INITIALLY DEFERRED
    )""",
    """CREATE TABLE revisions (
        entry_id TEXT NOT NULL REFERENCES entries(entry_id),
        revision_number INTEGER NOT NULL CHECK(revision_number >= 1),
        parent_revision_number INTEGER,
        title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 160),
        content TEXT NOT NULL CHECK(length(content) BETWEEN 1 AND 6000),
        revision_reason TEXT NOT NULL CHECK(length(revision_reason) <= 500),
        author_classification TEXT NOT NULL CHECK(author_classification IN ('colin_model','daina_human','system')),
        invocation_id TEXT NOT NULL UNIQUE REFERENCES invocations(invocation_id),
        timestamp TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','fulfilled','retired','superseded')),
        PRIMARY KEY(entry_id,revision_number),
        FOREIGN KEY(entry_id,parent_revision_number) REFERENCES revisions(entry_id,revision_number),
        CHECK((revision_number = 1 AND parent_revision_number IS NULL) OR
              (revision_number > 1 AND parent_revision_number = revision_number - 1))
    )""",
    "CREATE INDEX idx_entries_scope ON entries(owner_id,channel_id,guild_id)",
    """CREATE TRIGGER immutable_revision_update BEFORE UPDATE ON revisions
       BEGIN SELECT RAISE(ABORT,'immutable_revision'); END""",
    """CREATE TRIGGER immutable_revision_delete BEFORE DELETE ON revisions
       BEGIN SELECT RAISE(ABORT,'immutable_revision'); END""",
    """CREATE TRIGGER immutable_receipt_update BEFORE UPDATE ON invocations
       WHEN OLD.outcome IS NOT NULL BEGIN SELECT RAISE(ABORT,'immutable_receipt'); END""",
    """CREATE TRIGGER immutable_receipt_delete BEFORE DELETE ON invocations
       BEGIN SELECT RAISE(ABORT,'immutable_receipt'); END""",
)


class RoomStore:
    def __init__(self, path: str | Path, *, protected_paths: tuple[str | Path, ...] = ()):
        try:
            self.path = Path(path).expanduser().resolve()
        except (OSError, RuntimeError, TypeError, ValueError):
            raise RoomError("invalid_input") from None
        self.protected_paths = (
            Path("data/colin_memory.sqlite3"),
            Path("data/paintbox-usage.sqlite"),
            Path(os.getenv("DISCORD_STATUS_PATH", "data/discord_status.json")),
            *( (Path(os.environ["PAINTBOX_USAGE_PATH"]),) if os.environ.get("PAINTBOX_USAGE_PATH") else () ),
            *map(Path, protected_paths),
        )
        self._validate_path()

    def _validate_path(self) -> None:
        try:
            # A database must never occupy SQLite's reserved sidecar pathname,
            # even when that sidecar does not exist yet.
            if self.path.name.endswith(("-journal", "-wal", "-shm")):
                raise RoomError("database_collision")
            own_paths = (self.path, *(Path(str(self.path) + suffix) for suffix in ("-journal", "-wal", "-shm")))
            for protected in self.protected_paths:
                for other in (protected, *(Path(str(protected) + suffix) for suffix in ("-journal", "-wal", "-shm"))):
                    other = other.expanduser().resolve()
                    for candidate in own_paths:
                        candidate = candidate.resolve()
                        if candidate == other:
                            raise RoomError("database_collision")
                        if candidate.exists() and other.exists() and os.path.samefile(candidate, other):
                            raise RoomError("database_collision")
            if self.path.name in {":memory:", ""} or self.path.is_dir():
                raise RoomError("invalid_input")
        except RoomError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise RoomError("invalid_input") from None

    @contextmanager
    def _connection(self, *, initializing: bool = False):
        self._validate_path()
        conn = None
        try:
            conn = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            if not initializing:
                self._check_schema(conn)
            yield conn
        except (sqlite3.Error, OSError, ValueError):
            raise RoomError("storage_failure") from None
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _check_schema(conn: sqlite3.Connection) -> None:
        if conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise RoomError("database_collision")
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise RoomError("schema_mismatch")
        objects = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        if objects != {"entries", "revisions", "invocations"}:
            raise RoomError("schema_mismatch")
        triggers = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        if not {
            "immutable_revision_update", "immutable_revision_delete",
            "immutable_receipt_update", "immutable_receipt_delete",
        }.issubset(triggers):
            raise RoomError("schema_mismatch")

    def initialize(self) -> None:
        self._validate_path()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
            with self._connection(initializing=True) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    app_id = conn.execute("PRAGMA application_id").fetchone()[0]
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                    objects = conn.execute("SELECT name FROM sqlite_master").fetchall()
                    if app_id == 0 and version == 0 and not objects:
                        for statement in _SCHEMA:
                            conn.execute(statement)
                        conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
                        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    else:
                        self._check_schema(conn)
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
        except OSError:
            raise RoomError("storage_failure") from None

    @staticmethod
    def _scope_sql() -> str:
        return "owner_id=? AND channel_id=? AND guild_id IS ?"

    def begin_invocation(
        self, *, operation_key: str, trigger_type: str, scope: RoomScope,
        requested_model: str,
    ) -> tuple[dict[str, Any], bool]:
        if not isinstance(operation_key, str) or not 1 <= len(operation_key) <= 128:
            raise RoomError("invalid_input")
        if trigger_type not in {"becoming_want", "becoming_revise"}:
            raise RoomError("invalid_input")
        owner, channel, guild = scope.values()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM invocations WHERE operation_key=?", (operation_key,)).fetchone()
                if row is not None:
                    if (row["owner_id"], row["channel_id"], row["guild_id"], row["trigger_type"]) != (
                        owner, channel, guild, trigger_type,
                    ):
                        raise RoomError("operation_conflict")
                    conn.execute("COMMIT")
                    return dict(row), False
                invocation_id = str(uuid4())
                conn.execute(
                    """INSERT INTO invocations(invocation_id,operation_key,trigger_type,
                       actor_classification,owner_id,channel_id,guild_id,started_at,
                       requested_model,prompt_version) VALUES (?,?,?,'system',?,?,?,?,?,?)""",
                    (invocation_id, operation_key, trigger_type, owner, channel, guild, _now(),
                     _metadata(requested_model), PROMPT_VERSION),
                )
                row = conn.execute("SELECT * FROM invocations WHERE invocation_id=?", (invocation_id,)).fetchone()
                conn.execute("COMMIT")
                return dict(row), True
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def _pending(self, conn: sqlite3.Connection, invocation_id: str, scope: RoomScope) -> sqlite3.Row:
        row = conn.execute(
            f"SELECT * FROM invocations WHERE invocation_id=? AND {self._scope_sql()}",
            (invocation_id, *scope.values()),
        ).fetchone()
        if row is None or row["outcome"] is not None:
            raise RoomError("operation_conflict")
        return row

    @staticmethod
    def _complete(
        conn: sqlite3.Connection, invocation_id: str, *, outcome: str,
        result: RoomModelResult | None = None, metadata: Mapping[str, str] | None = None,
        error_category: str | None = None, entry_id: str | None = None,
        revision_number: int | None = None,
    ) -> dict[str, Any]:
        data = dict(metadata or {})
        if result is not None:
            data = {key: getattr(result, key) for key in (
                "requested_model", "returned_model", "returned_provider", "provider_response_id", "prompt_digest",
            )}
        current = conn.execute("SELECT requested_model FROM invocations WHERE invocation_id=?", (invocation_id,)).fetchone()
        requested_model = _metadata(data.get("requested_model"))
        if requested_model == UNKNOWN:
            requested_model = current[0]
        conn.execute(
            """UPDATE invocations SET completed_at=?,outcome=?,actor_classification=?,
               requested_model=?,returned_model=?,returned_provider=?,provider_response_id=?,prompt_digest=?,
               error_category=?,abstention_reason=?,entry_id=?,revision_number=? WHERE invocation_id=?""",
            (_now(), outcome, "colin_model" if result is not None or data else "system",
             requested_model,
             _metadata(data.get("returned_model")), _metadata(data.get("returned_provider")),
             _metadata(data.get("provider_response_id")), _metadata(data.get("prompt_digest")),
             error_category if error_category in ERROR_CATEGORIES else None,
             result.reason if result is not None and outcome == "abstained" else None,
             entry_id, revision_number, invocation_id),
        )
        return dict(conn.execute("SELECT * FROM invocations WHERE invocation_id=?", (invocation_id,)).fetchone())

    def finish_without_entry(
        self, *, invocation_id: str, scope: RoomScope, outcome: str,
        result: RoomModelResult | None = None, metadata: Mapping[str, str] | None = None,
        error_category: str | None = None,
    ) -> dict[str, Any]:
        if outcome not in {"abstained", "denied", "failed"}:
            raise RoomError("invalid_input")
        if outcome == "abstained":
            if result is None:
                raise RoomError("invalid_input")
            validate_result(result, expected_action="abstain")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._pending(conn, invocation_id, scope)
                receipt = self._complete(conn, invocation_id, outcome=outcome, result=result,
                                         metadata=metadata, error_category=error_category)
                conn.execute("COMMIT")
                return receipt
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def write_revision(
        self, *, invocation_id: str, scope: RoomScope, result: RoomModelResult,
        entry_id: str | None = None, expected_revision: int | None = None,
    ) -> dict[str, Any]:
        action = "revise" if entry_id is not None else "create"
        validate_result(result, expected_action=action)
        if result.action != action:
            raise RoomError("invalid_input")
        owner, channel, guild = scope.values()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                invocation = self._pending(conn, invocation_id, scope)
                if invocation["trigger_type"] != ("becoming_revise" if entry_id else "becoming_want"):
                    raise RoomError("operation_conflict")
                timestamp = _now()
                status = "active"
                if entry_id is None:
                    entry_id = str(uuid4())
                    revision = 1
                    conn.execute(
                        """INSERT INTO entries(entry_id,kind,author_classification,owner_id,channel_id,
                           guild_id,created_at,current_revision,status) VALUES (?,'want','colin_model',?,?,?,?,1,'active')""",
                        (entry_id, owner, channel, guild, timestamp),
                    )
                else:
                    old = conn.execute(
                        f"SELECT current_revision,status FROM entries WHERE entry_id=? AND {self._scope_sql()}",
                        (entry_id, owner, channel, guild),
                    ).fetchone()
                    if old is None:
                        raise RoomError("not_found")
                    if type(expected_revision) is not int or old["current_revision"] != expected_revision:
                        raise RoomError("stale_revision")
                    revision, status = expected_revision + 1, old["status"]
                conn.execute(
                    """INSERT INTO revisions(entry_id,revision_number,parent_revision_number,title,content,
                       revision_reason,author_classification,invocation_id,timestamp,status)
                       VALUES (?,?,?,?,?,?,'colin_model',?,?,?)""",
                    (entry_id, revision, revision - 1 if revision > 1 else None, result.title,
                     result.content, result.reason, invocation_id, timestamp, status),
                )
                if revision > 1:
                    conn.execute("UPDATE entries SET current_revision=? WHERE entry_id=?", (revision, entry_id))
                receipt = self._complete(conn, invocation_id, outcome="created" if revision == 1 else "revised",
                                         result=result, entry_id=entry_id, revision_number=revision)
                conn.execute("COMMIT")
                return receipt
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def get_entry(self, entry_id: str, scope: RoomScope) -> dict[str, Any] | None:
        if not isinstance(entry_id, str) or len(entry_id) > 128:
            raise RoomError("invalid_input")
        with self._connection() as conn:
            row = conn.execute(
                """SELECT e.*,r.revision_number,r.parent_revision_number,r.title,r.content,
                   r.revision_reason,r.timestamp,r.invocation_id,i.requested_model,i.returned_model,
                   i.returned_provider,i.provider_response_id,i.prompt_version,i.prompt_digest
                   FROM entries e JOIN revisions r ON e.entry_id=r.entry_id AND e.current_revision=r.revision_number
                   JOIN invocations i ON r.invocation_id=i.invocation_id
                   WHERE e.entry_id=? AND e.owner_id=? AND e.channel_id=? AND e.guild_id IS ?""",
                (entry_id, *scope.values()),
            ).fetchone()
            return dict(row) if row is not None else None

    def get_history(self, entry_id: str, scope: RoomScope) -> list[dict[str, Any]]:
        if not isinstance(entry_id, str) or len(entry_id) > 128:
            raise RoomError("invalid_input")
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT r.*,i.requested_model,i.returned_model,i.returned_provider,
                   i.provider_response_id,i.prompt_version,i.prompt_digest
                   FROM revisions r JOIN entries e ON e.entry_id=r.entry_id
                   JOIN invocations i ON i.invocation_id=r.invocation_id
                   WHERE e.entry_id=? AND e.owner_id=? AND e.channel_id=? AND e.guild_id IS ?
                   ORDER BY r.revision_number""",
                (entry_id, *scope.values()),
            ).fetchall()
            return [dict(row) for row in rows]


class RoomService:
    """Explicit operations only; DB work stays off Discord's event loop."""

    def __init__(self, store: RoomStore, generator: Callable[..., Awaitable[RoomModelResult]], requested_model: str):
        self.store, self.generator, self.requested_model = store, generator, requested_model

    async def show(self, scope: RoomScope, entry_id: str) -> dict[str, Any] | None:
        await asyncio.to_thread(self.store.initialize)
        return await asyncio.to_thread(self.store.get_entry, entry_id, scope)

    async def history(self, scope: RoomScope, entry_id: str) -> list[dict[str, Any]]:
        await asyncio.to_thread(self.store.initialize)
        return await asyncio.to_thread(self.store.get_history, entry_id, scope)

    async def create(self, scope: RoomScope, operation_key: str, seed: str = "") -> dict[str, Any]:
        return await self._operate(scope, operation_key, seed=seed)

    async def revise(
        self, scope: RoomScope, operation_key: str, entry_id: str,
        expected_revision: int, instruction: str,
    ) -> dict[str, Any]:
        return await self._operate(scope, operation_key, entry_id=entry_id,
                                   expected_revision=expected_revision, instruction=instruction)

    async def _operate(
        self, scope: RoomScope, operation_key: str, *, seed: str = "",
        entry_id: str | None = None, expected_revision: int | None = None, instruction: str = "",
    ) -> dict[str, Any]:
        revising = entry_id is not None
        if (not isinstance(seed, str) or len(seed) > 4000 or "\x00" in seed
            or not isinstance(instruction, str) or len(instruction) > 4000 or "\x00" in instruction
            or (revising and (not instruction.strip() or type(expected_revision) is not int or expected_revision < 1))):
            raise RoomError("invalid_input")
        await asyncio.to_thread(self.store.initialize)
        receipt, fresh = await asyncio.to_thread(
            self.store.begin_invocation, operation_key=operation_key,
            trigger_type="becoming_revise" if revising else "becoming_want",
            scope=scope, requested_model=self.requested_model,
        )
        if not fresh:
            return receipt  # Pending is truthful UNKNOWN, never a reason for a paid retry.
        invocation_id = receipt["invocation_id"]
        result = None
        try:
            current = None
            if revising:
                current = await asyncio.to_thread(self.store.get_entry, entry_id, scope)
                if current is None:
                    raise RoomError("not_found")
                if current["current_revision"] != expected_revision:
                    raise RoomError("stale_revision")
            result = await self.generator(seed=seed, current_revision=current, instruction=instruction)
            validate_result(result, expected_action="revise" if revising else "create")
            if result.action == "abstain":
                return await asyncio.to_thread(self.store.finish_without_entry,
                    invocation_id=invocation_id, scope=scope, outcome="abstained", result=result)
            return await asyncio.to_thread(self.store.write_revision,
                invocation_id=invocation_id, scope=scope, result=result,
                entry_id=entry_id, expected_revision=expected_revision)
        except Exception as error:
            category = error.category if isinstance(error, RoomError) else "internal_failure"
            metadata = getattr(error, "metadata", {}) if isinstance(error, RoomModelError) else {}
            if result is not None and isinstance(result, RoomModelResult):
                metadata = {key: getattr(result, key) for key in (
                    "requested_model", "returned_model", "returned_provider", "provider_response_id", "prompt_digest",
                )}
            # If storage itself is unavailable this may also fail. The command then
            # reports no confirmed outcome; it must never manufacture a receipt.
            return await asyncio.to_thread(self.store.finish_without_entry,
                invocation_id=invocation_id, scope=scope,
                outcome="denied" if category in {"not_found", "stale_revision", "operation_conflict"} else "failed",
                metadata=metadata, error_category=category)
