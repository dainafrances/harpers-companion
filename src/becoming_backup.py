"""Optional encrypted Room exports. No import-time storage, network or model I/O."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time as monotonic_time
from typing import Mapping
from uuid import uuid4

from . import becoming

FORMAT_VERSION = 1
RESTORE_TOOL_VERSION = 1
RETENTION_DAYS = {"daily": 30, "weekly": 84, "monthly": 366, "manual": 90}
DEFAULT_MAX_PLAINTEXT_BYTES = 134217728
MAX_PLAINTEXT_BYTES = 268435456
MAX_MANIFEST_BYTES = 16384
MAX_STATUS_BYTES = 8192
_BACKUP_LOCK = threading.Lock()
_CATEGORIES = frozenset({
    "busy", "invalid_configuration", "source_missing", "schema_mismatch",
    "integrity_failure", "immutability_failure", "maintenance_required",
    "source_changed", "plaintext_too_large", "temporary_storage_unavailable",
    "capture_failed", "encryption_failed", "timeout", "cancelled", "internal_failure",
    "invalid_object_key", "storage_unavailable", "object_lock_required",
    "retention_mismatch", "metadata_mismatch", "destination_exists",
    "destination_unavailable", "download_size_mismatch", "download_hash_mismatch",
    "download_failure", "ciphertext_invalid", "status_unavailable",
    "restore_destination_invalid", "identity_invalid", "ciphertext_hash_mismatch",
    "decrypt_failed", "archive_invalid", "manifest_mismatch", "database_hash_mismatch",
    "recovery_verification_failed", "restore_io_failure", "object_metadata_invalid",
    "synthetic_only_required", "partial_success", "success", "not_run",
})
BACKUP_CATEGORIES = _CATEGORIES


class BackupError(RuntimeError):
    def __init__(self, category: str):
        self.category = category if category in _CATEGORIES else "internal_failure"
        super().__init__(self.category)


def _utc(value: datetime | None = None) -> datetime:
    value = datetime.now(timezone.utc) if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BackupError("invalid_configuration")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _commit(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) else None


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def file_sha256(path: str | Path, *, deadline=None, cancel=None) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            _check(deadline, cancel)
            digest.update(block)
    return digest.hexdigest()


def _integer(raw: str, default: int, minimum: int, maximum: int) -> int:
    if raw == "":
        return default
    if not re.fullmatch(r"[0-9]{1,10}", raw):
        raise ValueError
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ValueError
    return value


def _bool(raw: str, default: bool) -> bool:
    if not raw:
        return default
    if raw not in {"true", "false"}:
        raise ValueError
    return raw == "true"


def _safe_status_path(path: Path, database: Path) -> bool:
    try:
        if ".." in path.parts or path.is_symlink():
            return False
        target, database = path.absolute().resolve(), database.absolute().resolve()
        if target.parent != database.parent or target.suffix != ".json":
            return False
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", target.name):
            return False
        if target.exists() and (not target.is_file() or target.stat().st_nlink != 1):
            return False
        if any(target == Path(str(database) + suffix) for suffix in ("", "-wal", "-shm", "-journal")):
            return False
        root = becoming.REPOSITORY_ROOT.resolve()
        identity = becoming._filesystem_identity(root)
        becoming._private_repository_route(path.absolute(), root, identity)
        becoming._private_repository_route(target, root, identity)
        return True
    except (OSError, RuntimeError, ValueError, becoming.RoomError):
        return False


@dataclass(frozen=True)
class BackupConfig:
    enabled: bool = False
    required_for_room: bool = True
    valid: bool = False
    time_utc: time = time(3, 0, tzinfo=timezone.utc)
    age_recipient: str = field(default="", repr=False)
    endpoint_url: str = field(default="", repr=False)
    region: str = field(default="", repr=False)
    bucket: str = field(default="", repr=False)
    prefix: str = field(default="room-of-becoming", repr=False)
    access_key_id: str = field(default="", repr=False)
    secret_access_key: str = field(default="", repr=False)
    status_path: Path = field(default=Path("/app/data/room-of-becoming/backup-status.json"), repr=False)
    db_path: Path = field(default=becoming.DEFAULT_DB_PATH, repr=False)
    max_plaintext_bytes: int = DEFAULT_MAX_PLAINTEXT_BYTES
    timeout_seconds: int = 180

    @property
    def can_run(self) -> bool:
        return self.enabled and self.valid

    def room_allowed(self, room_config: becoming.RoomConfig) -> bool:
        return not room_config.enabled or not self.required_for_room or self.can_run

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None,
                 room_config: becoming.RoomConfig | None = None) -> BackupConfig:
        env = os.environ if env is None else env
        room_config = room_config or becoming.RoomConfig.from_env(env)
        database = room_config.db_path.absolute()
        values = {"db_path": database, "status_path": database.parent / "backup-status.json"}
        try:
            get = lambda name, default="": env.get("BECOMING_BACKUP_" + name, default).strip()
            values["required_for_room"] = _bool(get("REQUIRED_FOR_ROOM"), True)
            values["enabled"] = _bool(get("ENABLED"), False)
            clock = get("TIME_UTC", "03:00")
            if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock):
                raise ValueError
            values["time_utc"] = time(*map(int, clock.split(":")), tzinfo=timezone.utc)
            values["max_plaintext_bytes"] = _integer(get("MAX_PLAINTEXT_BYTES"), DEFAULT_MAX_PLAINTEXT_BYTES, 1, MAX_PLAINTEXT_BYTES)
            values["timeout_seconds"] = _integer(get("TIMEOUT_SECONDS"), 180, 10, 900)
            values["age_recipient"] = get("AGE_RECIPIENT")
            # A bot accepts only one public recipient. Local recovery has a
            # separate identity interface and is never imported at startup.
            if any("AGE-SECRET-KEY-" in str(value) for value in env.values()):
                raise ValueError
            if any("AGE" in key and "IDENTITY" in key and str(value).strip() for key, value in env.items()):
                raise ValueError
            recipient = values["age_recipient"]
            if not re.fullmatch(r"age1[0-9a-z]{58}", recipient):
                raise ValueError
            import pyrage
            pyrage.x25519.Recipient.from_str(recipient)
            for field_name, setting in (("endpoint_url", "S3_ENDPOINT_URL"), ("region", "S3_REGION"),
                                        ("bucket", "S3_BUCKET"), ("access_key_id", "S3_ACCESS_KEY_ID"),
                                        ("secret_access_key", "S3_SECRET_ACCESS_KEY")):
                value = get(setting)
                if not value or len(value) > 512 or any(ord(c) < 33 or ord(c) == 127 for c in value):
                    raise ValueError
                values[field_name] = value
            region = values["region"]
            if not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]{3}", region):
                raise ValueError
            if values["endpoint_url"] != f"https://s3.{region}.backblazeb2.com":
                raise ValueError
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{4,61}[a-z0-9]", values["bucket"]):
                raise ValueError
            prefix = get("S3_PREFIX", "room-of-becoming")
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}(?:/[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}){0,3}", prefix):
                raise ValueError
            values["prefix"] = prefix
            if get("STATUS_PATH"):
                values["status_path"] = Path(get("STATUS_PATH"))
            if not _safe_status_path(values["status_path"], database):
                raise ValueError
            return cls(**values, valid=True)
        except Exception:
            # Invalid input is never retained as public-key material or as a
            # connection configuration; in particular discard private identities.
            return cls(enabled=values.get("enabled", False),
                       required_for_room=values.get("required_for_room", True),
                       db_path=database, status_path=database.parent / "backup-status.json",
                       valid=False)


def _check(deadline: float | None = None, cancel: threading.Event | None = None) -> None:
    if cancel is not None and cancel.is_set():
        raise BackupError("cancelled")
    if deadline is not None and monotonic_time.monotonic() >= deadline:
        raise BackupError("timeout")


def _sql_manifest(conn: sqlite3.Connection, deadline=None, cancel=None) -> tuple[str, dict, dict]:
    conn.row_factory = sqlite3.Row
    digest = hashlib.sha256()
    counts = {}
    digest.update(b"{")
    orders = (("entries", "entry_id"), ("invocations", "invocation_id"), ("revisions", "entry_id,revision_number"))
    for index, (table, order) in enumerate(orders):
        if index:
            digest.update(b",")
        digest.update(_canonical(table) + b":[")
        count = 0
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}"):
            _check(deadline, cancel)
            if count:
                digest.update(b",")
            digest.update(_canonical(dict(row)))
            count += 1
        digest.update(b"]")
        counts[table] = count
    digest.update(b"}")
    summary = conn.execute("SELECT COUNT(*),COALESCE(MIN(n),0),COALESCE(MAX(n),0),COALESCE(SUM(n),0) FROM (SELECT COUNT(*) n FROM revisions GROUP BY entry_id)").fetchone()
    revisions = {"entry_count": summary[0], "min_per_entry": summary[1], "max_per_entry": summary[2], "total_revisions": summary[3]}
    return digest.hexdigest(), counts, revisions


def _immutability(conn: sqlite3.Connection) -> None:
    for table, sql, expected in (
        ("revisions", "UPDATE revisions SET title=title", "immutable_revision"),
        ("revisions", "DELETE FROM revisions", "immutable_revision"),
        ("invocations", "UPDATE invocations SET outcome=outcome WHERE outcome IS NOT NULL", "immutable_receipt"),
        ("invocations", "DELETE FROM invocations", "immutable_receipt"),
    ):
        condition = " WHERE outcome IS NOT NULL" if table == "invocations" and sql.startswith("UPDATE") else ""
        if conn.execute(f"SELECT 1 FROM {table}{condition} LIMIT 1").fetchone() is None:
            continue  # Exact schema fingerprint still verifies empty-table guards.
        conn.execute("BEGIN IMMEDIATE")
        try:
            try:
                conn.execute(sql)
            except sqlite3.IntegrityError as error:
                if str(error) != expected:
                    raise BackupError("immutability_failure") from None
            else:
                raise BackupError("immutability_failure")
        finally:
            conn.execute("ROLLBACK")


def validate_database(path: str | Path, *, immutable: bool = True, deadline=None, cancel=None) -> dict:
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise BackupError("source_missing")
    connection = None
    try:
        connection = sqlite3.connect(path.absolute().as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=5)
        connection.set_progress_handler(lambda: int((cancel is not None and cancel.is_set()) or (deadline is not None and monotonic_time.monotonic() >= deadline)), 1000)
        try:
            becoming.RoomStore._check_schema(connection)
        except becoming.RoomError:
            raise BackupError("schema_mismatch") from None
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BackupError("integrity_failure")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("integrity_failure")
        _check(deadline, cancel)
        before, counts, revisions = _sql_manifest(connection, deadline, cancel)
        if immutable:
            _immutability(connection)
            after, _, _ = _sql_manifest(connection, deadline, cancel)
            if before != after:
                raise BackupError("immutability_failure")
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        return {"application_id": becoming.APPLICATION_ID, "schema_version": becoming.SCHEMA_VERSION,
                "database_size": path.stat().st_size, "database_sha256": file_sha256(path, deadline=deadline, cancel=cancel),
                "sql_manifest_digest": before, "row_counts": counts,
                "revision_count_summary": revisions, "source_journal_mode": mode}
    except sqlite3.Error:
        _check(deadline, cancel)
        raise BackupError("integrity_failure") from None
    except OSError:
        raise BackupError("capture_failed") from None
    finally:
        if connection is not None:
            connection.close()


def _bundle_state(path: Path, *, deadline=None, cancel=None, limit=None) -> dict:
    sizes = [Path(str(path) + suffix).stat().st_size for suffix in ("", "-wal", "-journal") if Path(str(path) + suffix).exists()]
    if limit is not None and sum(sizes) > limit:
        raise BackupError("plaintext_too_large")
    out = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        item = Path(str(path) + suffix)
        out[suffix] = (item.exists(), item.stat().st_size if item.exists() else None,
                       file_sha256(item, deadline=deadline, cancel=cancel) if item.exists() else None)
    return out


def _private_file(path: Path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "wb")


def _temp_parent(database: Path) -> Path:
    parent = Path(tempfile.gettempdir()).resolve()
    volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if parent.is_relative_to(becoming.REPOSITORY_ROOT.resolve()) or parent.is_relative_to(database.parent.resolve()):
        raise BackupError("temporary_storage_unavailable")
    if volume:
        mount = Path(volume).resolve()
        if parent.is_relative_to(mount) or (mount.exists() and parent.stat().st_dev == mount.stat().st_dev):
            raise BackupError("temporary_storage_unavailable")
    return parent


@dataclass(frozen=True)
class CaptureResult:
    archive_path: Path
    manifest: dict


_MANIFEST_KEYS = {"backup_format_version", "backup_id", "created_at", "application_id", "schema_version",
                  "database_size", "database_sha256", "sql_manifest_digest", "row_counts",
                  "revision_count_summary", "capture_mechanism", "source_journal_mode", "required_restore_tool_version"}


def _manifest_shape(value: object) -> dict:
    if not isinstance(value, dict) or set(value) not in (_MANIFEST_KEYS, _MANIFEST_KEYS | {"code_commit"}):
        raise BackupError("manifest_mismatch")
    if any(type(value[key]) is not int for key in ("backup_format_version", "required_restore_tool_version", "application_id", "schema_version")):
        raise BackupError("manifest_mismatch")
    if value["backup_format_version"] != FORMAT_VERSION or value["required_restore_tool_version"] != RESTORE_TOOL_VERSION:
        raise BackupError("manifest_mismatch")
    if value["application_id"] != becoming.APPLICATION_ID or value["schema_version"] != becoming.SCHEMA_VERSION:
        raise BackupError("schema_mismatch")
    if not isinstance(value["backup_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", value["backup_id"]):
        raise BackupError("manifest_mismatch")
    for key in ("database_sha256", "sql_manifest_digest"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{64}", value[key]):
            raise BackupError("manifest_mismatch")
    if type(value["database_size"]) is not int or not 1 <= value["database_size"] <= MAX_PLAINTEXT_BYTES:
        raise BackupError("manifest_mismatch")
    if value["capture_mechanism"] != "sqlite_backup_api_locked_snapshot" or not isinstance(value["source_journal_mode"], str) or value["source_journal_mode"] not in {"wal", "delete", "truncate", "persist"}:
        raise BackupError("manifest_mismatch")
    try:
        if _stamp(datetime.fromisoformat(value["created_at"].replace("Z", "+00:00"))) != value["created_at"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise BackupError("manifest_mismatch") from None
    if "code_commit" in value and _commit(value["code_commit"]) is None:
        raise BackupError("manifest_mismatch")
    for key, names in (("row_counts", {"entries", "invocations", "revisions"}),
                       ("revision_count_summary", {"entry_count", "min_per_entry", "max_per_entry", "total_revisions"})):
        if not isinstance(value[key], dict) or set(value[key]) != names or any(type(n) is not int or not 0 <= n <= 100000000 for n in value[key].values()):
            raise BackupError("manifest_mismatch")
    return value


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BackupError("manifest_mismatch")
        result[key] = value
    return result


def validate_archive(path: str | Path, max_plaintext_bytes: int = DEFAULT_MAX_PLAINTEXT_BYTES) -> dict:
    path = Path(path)
    try:
        size = path.stat().st_size
        if size > max_plaintext_bytes:
            raise BackupError("plaintext_too_large")
        if size < 1024 or size % 512:
            raise BackupError("archive_invalid")
        with tarfile.open(path, mode="r:") as archive:
            members = []
            for member in archive:
                members.append(member)
                if len(members) > 2:
                    raise BackupError("archive_invalid")
            if len(members) != 2 or {m.name for m in members} != {"room.sqlite3", "manifest.json"}:
                raise BackupError("archive_invalid")
            cursor = 0
            for member in members:
                if not member.isreg() or member.type != tarfile.REGTYPE or member.pax_headers or member.sparse is not None:
                    raise BackupError("archive_invalid")
                if member.offset != cursor or member.offset_data != cursor + 512:
                    raise BackupError("archive_invalid")
                cursor = member.offset_data + ((member.size + 511) // 512) * 512
            manifest_member = archive.getmember("manifest.json")
            if not 0 < manifest_member.size <= MAX_MANIFEST_BYTES:
                raise BackupError("manifest_mismatch")
            stream = archive.extractfile(manifest_member)
            with stream:
                manifest = _manifest_shape(json.loads(stream.read(MAX_MANIFEST_BYTES + 1), object_pairs_hook=_unique_json))
            database = archive.getmember("room.sqlite3")
            if database.size != manifest["database_size"]:
                raise BackupError("database_hash_mismatch")
            digest = hashlib.sha256()
            with archive.extractfile(database) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != manifest["database_sha256"]:
                raise BackupError("database_hash_mismatch")
        with path.open("rb") as stream:
            stream.seek(cursor)
            remainder = size - cursor
            if remainder < 1024:
                raise BackupError("archive_invalid")
            while block := stream.read(8192):
                if any(block):
                    raise BackupError("archive_invalid")
        return manifest
    except BackupError:
        raise
    except (OSError, ValueError, tarfile.TarError, UnicodeError):
        raise BackupError("archive_invalid") from None


def extract_validated_archive(path: str | Path, destination_dir: str | Path,
                              max_plaintext_bytes: int = DEFAULT_MAX_PLAINTEXT_BYTES) -> Path:
    manifest = validate_archive(path, max_plaintext_bytes)
    directory = Path(destination_dir)
    if not directory.is_dir() or directory.is_symlink() or any(directory.iterdir()):
        raise BackupError("restore_destination_invalid")
    target = directory / "room.sqlite3"
    try:
        with tarfile.open(path, "r:") as archive, archive.extractfile("room.sqlite3") as source, _private_file(target) as dest:
            shutil.copyfileobj(source, dest)
            dest.flush()
            os.fsync(dest.fileno())
        info = validate_database(target)
        for key in ("application_id", "schema_version", "database_size", "database_sha256", "sql_manifest_digest", "row_counts", "revision_count_summary"):
            if info[key] != manifest[key]:
                raise BackupError("manifest_mismatch")
        return target
    except BaseException:
        if target.exists():
            target.unlink()
        raise


def capture_bundle(store: becoming.RoomStore, destination_dir: str | Path, config: BackupConfig,
                   *, code_commit: str | None = None, now: datetime | None = None,
                   backup_id: str | None = None, deadline: float | None = None,
                   cancel: threading.Event | None = None) -> CaptureResult:
    if not config.can_run:
        raise BackupError("invalid_configuration")
    directory = Path(destination_dir)
    if not directory.is_dir() or directory.is_symlink() or any(directory.iterdir()):
        raise BackupError("destination_unavailable")
    database = store.path
    _temp_parent(database)
    if directory.resolve().is_relative_to(database.parent.resolve()) or directory.resolve().is_relative_to(becoming.REPOSITORY_ROOT.resolve()):
        raise BackupError("temporary_storage_unavailable")
    os.chmod(directory, 0o700)
    source_copy = directory / ".capture-source.sqlite3"
    backup = directory / "room.sqlite3"
    archive_path = directory / "bundle.tar"
    created = _utc(now)
    identifier = backup_id or uuid4().hex
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise BackupError("invalid_configuration")
    acquired = False
    connection = dest = None
    try:
        _check(deadline, cancel)
        wait = max(0.0, deadline - monotonic_time.monotonic()) if deadline else config.timeout_seconds
        acquired = becoming._STORAGE_LOCK.acquire(timeout=wait)
        if not acquired:
            raise BackupError("timeout")
        store._validate_path()
        if not database.is_file() or database.stat().st_size == 0:
            raise BackupError("source_missing")
        journal = Path(str(database) + "-journal")
        if journal.exists() and journal.stat().st_size:
            raise BackupError("maintenance_required")
        before = _bundle_state(database, deadline=deadline, cancel=cancel, limit=config.max_plaintext_bytes)
        total = sum(item[1] or 0 for suffix, item in before.items() if suffix != "-shm")
        if total > config.max_plaintext_bytes:
            raise BackupError("plaintext_too_large")
        # The same in-process lock protects every Room writer. Copy all committed
        # state, rebuild SHM only in staging, and never ask SQLite to open source.
        for suffix in ("", "-wal"):
            source = Path(str(database) + suffix)
            if source.exists():
                with source.open("rb") as original, _private_file(Path(str(source_copy) + suffix)) as output:
                    while block := original.read(1024 * 1024):
                        _check(deadline, cancel)
                        output.write(block)
        if _bundle_state(database, deadline=deadline, cancel=cancel, limit=config.max_plaintext_bytes) != before:
            raise BackupError("source_changed")
        connection = sqlite3.connect(source_copy, isolation_level=None)
        connection.set_progress_handler(lambda: int((cancel is not None and cancel.is_set()) or (deadline is not None and monotonic_time.monotonic() >= deadline)), 1000)
        try:
            becoming.RoomStore._check_schema(connection)
        except becoming.RoomError:
            raise BackupError("schema_mismatch") from None
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("integrity_failure")
        original_digest, _, _ = _sql_manifest(connection, deadline, cancel)
        source_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        with _private_file(backup):
            pass
        dest = sqlite3.connect(backup, isolation_level=None)
        connection.backup(dest, pages=64, progress=lambda *_: _check(deadline, cancel), sleep=0.0)
        dest.close(); dest = None
        connection.close(); connection = None
        dest = sqlite3.connect(backup, isolation_level=None)
        if dest.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
            raise BackupError("capture_failed")
        dest.close(); dest = None
        info = validate_database(backup, deadline=deadline, cancel=cancel)
        if info["sql_manifest_digest"] != original_digest:
            raise BackupError("source_changed")
        manifest = {**{key: info[key] for key in ("application_id", "schema_version", "database_size", "database_sha256", "sql_manifest_digest", "row_counts", "revision_count_summary")},
                    "backup_format_version": FORMAT_VERSION, "backup_id": identifier, "created_at": _stamp(created),
                    "capture_mechanism": "sqlite_backup_api_locked_snapshot", "source_journal_mode": source_mode,
                    "required_restore_tool_version": RESTORE_TOOL_VERSION}
        if commit := _commit(code_commit):
            manifest["code_commit"] = commit
        encoded = _canonical(manifest)
        if len(encoded) > MAX_MANIFEST_BYTES or backup.stat().st_size + len(encoded) + 20480 > config.max_plaintext_bytes:
            raise BackupError("plaintext_too_large")
        with _private_file(archive_path) as output, tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            item = tarfile.TarInfo("room.sqlite3"); item.size = backup.stat().st_size; item.mode = 0o600
            with backup.open("rb") as stream:
                archive.addfile(item, stream)
            import io
            item = tarfile.TarInfo("manifest.json"); item.size = len(encoded); item.mode = 0o600
            archive.addfile(item, io.BytesIO(encoded))
        validate_archive(archive_path, config.max_plaintext_bytes)
        if _bundle_state(database, deadline=deadline, cancel=cancel, limit=config.max_plaintext_bytes) != before:
            raise BackupError("source_changed")
        _check(deadline, cancel)
        return CaptureResult(archive_path, manifest)
    except BackupError:
        if archive_path.exists(): archive_path.unlink()
        raise
    except (sqlite3.Error, OSError, becoming.RoomError):
        if archive_path.exists(): archive_path.unlink()
        _check(deadline, cancel)
        raise BackupError("capture_failed") from None
    finally:
        if dest is not None: dest.close()
        if connection is not None: connection.close()
        for base in (source_copy, backup):
            for suffix in ("", "-wal", "-shm", "-journal"):
                file = Path(str(base) + suffix)
                if file.exists(): file.unlink()
        if acquired:
            becoming._STORAGE_LOCK.release()


class _LimitedReader:
    def __init__(self, stream, limit, deadline=None, cancel=None):
        self.stream, self.limit, self.deadline, self.cancel = stream, limit, deadline, cancel
        self.count = 0
    def read(self, size=-1):
        _check(self.deadline, self.cancel)
        block = self.stream.read(min(size if size >= 0 else 8192, 8192))
        self.count += len(block)
        if self.count > self.limit: raise BackupError("plaintext_too_large")
        return block


class _LimitedWriter:
    def __init__(self, stream, limit, deadline=None, cancel=None):
        self.stream, self.limit, self.deadline, self.cancel = stream, limit, deadline, cancel
        self.count = 0
    def write(self, block):
        _check(self.deadline, self.cancel)
        self.count += len(block)
        if self.count > self.limit: raise BackupError("plaintext_too_large")
        return self.stream.write(block)
    def flush(self):
        return self.stream.flush()


def encrypt_archive(path: str | Path, ciphertext_path: str | Path, recipient: str,
                    max_plaintext_bytes: int = DEFAULT_MAX_PLAINTEXT_BYTES,
                    *, deadline: float | None = None, cancel: threading.Event | None = None) -> dict:
    path, ciphertext_path = Path(path), Path(ciphertext_path)
    pending = ciphertext_path.with_name(".encrypt-" + uuid4().hex + ".pending")
    published = False
    try:
        if ciphertext_path.exists() or ciphertext_path.is_symlink(): raise BackupError("destination_exists")
        if path.stat().st_size > max_plaintext_bytes: raise BackupError("plaintext_too_large")
        if not re.fullmatch(r"age1[0-9a-z]{58}", recipient): raise BackupError("invalid_configuration")
        import pyrage
        public = pyrage.x25519.Recipient.from_str(recipient)
        with path.open("rb") as source, _private_file(pending) as target:
            pyrage.encrypt_io(_LimitedReader(source, max_plaintext_bytes, deadline, cancel),
                              _LimitedWriter(target, max_plaintext_bytes + 1048576, deadline, cancel), [public])
            target.flush(); os.fsync(target.fileno())
        _check(deadline, cancel)
        digest, size = file_sha256(pending, deadline=deadline, cancel=cancel), pending.stat().st_size
        _check(deadline, cancel)
        os.link(pending, ciphertext_path)
        published = True
        pending.unlink()
        return {"ciphertext_sha256": digest, "ciphertext_size": size}
    except BackupError:
        raise
    except BaseException as error:
        if published and ciphertext_path.exists():
            ciphertext_path.unlink()
            published = False
        if isinstance(error, (KeyboardInterrupt, asyncio.CancelledError)):
            raise BackupError("cancelled") from None
        raise BackupError("encryption_failed") from None
    finally:
        if pending.exists(): pending.unlink()
        if not published and ciphertext_path.exists():
            # Existing destinations were rejected before any write.
            pass


def _safe_status(value: object) -> dict:
    if not isinstance(value, dict): return {}
    output = {}
    for key in ("last_attempt_time", "last_successful_time"):
        raw = value.get(key)
        if isinstance(raw, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", raw): output[key] = raw
    if isinstance(value.get("backup_id"), str) and re.fullmatch(r"[0-9a-f]{32}", value["backup_id"]): output["backup_id"] = value["backup_id"]
    for key in ("successful_tiers", "failed_tiers"):
        if isinstance(value.get(key), list): output[key] = sorted(set(x for x in value[key] if isinstance(x, str) and x in RETENTION_DAYS))
    for key in ("ciphertext_size", "consecutive_failure_count", "schema_version"):
        raw = value.get(key)
        if type(raw) is int and 0 <= raw <= 1000000000: output[key] = raw
    for key in ("retention_verified", "download_verified"):
        if type(value.get(key)) is bool: output[key] = value[key]
    if isinstance(value.get("outcome_category"), str) and value["outcome_category"] in _CATEGORIES: output["outcome_category"] = value["outcome_category"]
    if commit := _commit(value.get("code_commit")): output["code_commit"] = commit
    return output


class BackupManager:
    def __init__(self, config: BackupConfig, store: becoming.RoomStore, *, transport=None, code_commit=None):
        self.config, self.store = config, store
        self.transport = transport
        self.code_commit = _commit(code_commit or os.getenv("RAILWAY_GIT_COMMIT_SHA"))
        self._status = {}

    @property
    def active(self) -> bool:
        return _BACKUP_LOCK.locked()

    def status(self) -> dict:
        if not self.config.can_run: return {"outcome_category": "invalid_configuration"}
        if self._status: return _safe_status(self._status)
        try:
            path = self.config.status_path
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_STATUS_BYTES: return {}
            self._status = _safe_status(json.loads(path.read_bytes()))
        except (OSError, ValueError, TypeError):
            return {"outcome_category": "status_unavailable"}
        return _safe_status(self._status)

    def _record(self, value: dict) -> dict:
        safe = _safe_status(value)
        self._status = safe
        path = self.config.status_path
        pending = path.with_name(".backup-status-" + uuid4().hex + ".pending")
        try:
            if not _safe_status_path(path, self.store.path) or not path.parent.is_dir(): raise BackupError("status_unavailable")
            with _private_file(pending) as stream:
                stream.write(_canonical(safe)); stream.flush(); os.fsync(stream.fileno())
            os.replace(pending, path)
        except (OSError, BackupError):
            safe = {**safe, "outcome_category": "status_unavailable"}
            self._status = safe
        finally:
            if pending.exists(): pending.unlink()
        return _safe_status(safe)

    async def run(self, tiers=("manual",), now: datetime | None = None) -> dict:
        if not self.config.can_run: raise BackupError("invalid_configuration")
        instant = _utc(now)
        if self.store.path != self.config.db_path.resolve(): raise BackupError("invalid_configuration")
        tiers = tuple(dict.fromkeys(tiers))
        if not tiers or any(tier not in RETENTION_DAYS for tier in tiers): raise BackupError("invalid_configuration")
        if not _BACKUP_LOCK.acquire(blocking=False): raise BackupError("busy")
        cancel = threading.Event()
        try:
            task = asyncio.create_task(asyncio.to_thread(self._run, tiers, instant, cancel))
        except BaseException:
            _BACKUP_LOCK.release()
            raise
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancel.set()
            try: await asyncio.shield(task)
            except Exception: pass
            raise

    def _run(self, tiers, now, cancel):
        deadline = monotonic_time.monotonic() + self.config.timeout_seconds
        previous = self.status()
        identifier = uuid4().hex
        result = {"last_attempt_time": _stamp(now), "backup_id": identifier, "successful_tiers": [],
                  "failed_tiers": list(tiers), "schema_version": becoming.SCHEMA_VERSION,
                  "outcome_category": "internal_failure", "consecutive_failure_count": previous.get("consecutive_failure_count", 0) + 1,
                  "retention_verified": False, "download_verified": False}
        if previous.get("last_successful_time"): result["last_successful_time"] = previous["last_successful_time"]
        if self.code_commit: result["code_commit"] = self.code_commit
        try:
            parent = _temp_parent(self.store.path)
            with tempfile.TemporaryDirectory(prefix="room-backup-", dir=parent) as temporary:
                directory = Path(temporary); os.chmod(directory, 0o700)
                capture = capture_bundle(self.store, directory, self.config, code_commit=self.code_commit,
                                         now=now, backup_id=identifier, deadline=deadline, cancel=cancel)
                ciphertext = directory / (identifier + ".tar.age")
                encrypted = encrypt_archive(capture.archive_path, ciphertext, self.config.age_recipient,
                                            self.config.max_plaintext_bytes, deadline=deadline, cancel=cancel)
                capture.archive_path.unlink()
                result["ciphertext_size"] = encrypted["ciphertext_size"]
                transport = self.transport or B2Transport(self.config)
                failures = []
                for tier in tiers:
                    try:
                        _check(deadline, cancel)
                        key = f"{self.config.prefix}/{tier}/{now:%Y/%m/%d}/{now:%Y%m%dT%H%M%SZ}-{identifier}.tar.age"
                        metadata = {"backup_format_version": str(FORMAT_VERSION), "schema_version": str(becoming.SCHEMA_VERSION),
                                    "backup_id": identifier, "tier": tier, "ciphertext_sha256": encrypted["ciphertext_sha256"]}
                        if self.code_commit: metadata["code_commit"] = self.code_commit
                        retain_until = max(now, _utc()) + timedelta(days=RETENTION_DAYS[tier], seconds=60)
                        verified = transport.upload_and_verify(ciphertext, key=key, metadata=metadata, retain_until=retain_until, deadline=deadline)
                        _check(deadline, cancel)
                        if not verified.get("retention_verified") or not verified.get("download_verified"): raise BackupError("metadata_mismatch")
                        result["successful_tiers"].append(tier)
                        result["failed_tiers"].remove(tier)
                    except BackupError as error:
                        failures.append(error.category)
                    except Exception:
                        failures.append("storage_unavailable")
                if not failures:
                    result.update(outcome_category="success", consecutive_failure_count=0, last_successful_time=_stamp(now), retention_verified=True, download_verified=True)
                else:
                    result["outcome_category"] = "partial_success" if result["successful_tiers"] else failures[0]
                    result["retention_verified"] = bool(result["successful_tiers"])
                    result["download_verified"] = bool(result["successful_tiers"])
        except BackupError as error:
            result["outcome_category"] = error.category
        except Exception:
            result["outcome_category"] = "internal_failure"
        finally:
            try: result = self._record(result)
            finally: _BACKUP_LOCK.release()
        return result


from .becoming_backup_transport import B2Transport
