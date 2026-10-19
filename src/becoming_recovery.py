"""Explicit local Room recovery administration; never imported by bot startup.

Private identities are accepted only from a private local file or secure stdin.
This module does not load dotenv, application configuration, or model clients.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import getpass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from typing import BinaryIO, Mapping

from . import becoming as room
from . import becoming_backup as backup


_MAX_METADATA_BYTES = 8192
_MAX_IDENTITY_BYTES = 65536
_DEFAULT_LIMIT = 134217728
_META_REQUIRED = frozenset({
    "backup_format_version", "schema_version", "backup_id", "tier",
    "ciphertext_sha256", "ciphertext_size",
})


def _error(category: str):
    raise backup.BackupError(category) from None


def _local_path(path: str | Path) -> Path:
    """Reject known live storage and the entire checkout, including aliases."""
    try:
        lexical = Path(path).expanduser().absolute()
        resolved = lexical.resolve()
        if ".." in lexical.parts or lexical.is_symlink():
            _error("restore_destination_invalid")
        for candidate in (lexical, resolved):
            for forbidden in (Path("/app/data"), room.REPOSITORY_ROOT.resolve()):
                if candidate == forbidden or forbidden in candidate.parents:
                    _error("restore_destination_invalid")
        return resolved
    except (OSError, RuntimeError, TypeError, ValueError):
        _error("restore_destination_invalid")


def _new_destination(path: str | Path) -> tuple[Path, bool]:
    destination = _local_path(path)
    try:
        if destination.exists():
            if not destination.is_dir() or any(destination.iterdir()):
                _error("restore_destination_invalid")
            if destination.stat().st_mode & 0o077:
                _error("restore_destination_invalid")
            return destination, False
        if not destination.parent.is_dir():
            _error("restore_destination_invalid")
        destination.mkdir(mode=0o700)
        return destination, True
    except OSError:
        _error("restore_destination_invalid")


def _temporary_root() -> Path:
    """A local override must not redirect decrypted bundles into live storage."""
    # macOS's ordinary /tmp is a system alias for /private/tmp. Canonicalize
    # this system-selected root, retaining strict leaf-symlink rejection for
    # explicit identity, ciphertext, credential and destination inputs.
    return _local_path(Path(tempfile.gettempdir()).resolve())


def _read_identity(*, identity_file=None, identity_stdin: BinaryIO | None = None):
    if (identity_file is None) == (identity_stdin is None):
        _error("identity_invalid")
    try:
        if identity_file is not None:
            path = _local_path(identity_file)
            lexical = Path(identity_file).expanduser().absolute()
            for route in (lexical, path):
                if any((parent / ".git").exists() or (parent / ".git").is_symlink()
                       for parent in route.parents):
                    _error("identity_invalid")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
                    _error("identity_invalid")
                data = handle.read(_MAX_IDENTITY_BYTES + 1)
        else:
            data = identity_stdin.read(_MAX_IDENTITY_BYTES + 1)
        if isinstance(data, str):
            data = data.encode("ascii")
        if not isinstance(data, bytes) or len(data) > _MAX_IDENTITY_BYTES:
            _error("identity_invalid")
        lines = [line.strip() for line in data.decode("ascii").splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
        if len(lines) != 1 or not re.fullmatch(r"AGE-SECRET-KEY-1[0-9A-Z]+", lines[0]):
            _error("identity_invalid")
        import pyrage
        return pyrage.x25519.Identity.from_str(lines[0])
    except backup.BackupError:
        raise
    except Exception:
        _error("identity_invalid")


def _metadata(value: Mapping[str, object]) -> dict:
    """Only content-neutral uploader metadata may cross this boundary."""
    try:
        result = dict(value)
        if not _META_REQUIRED <= result.keys() or result.keys() - (_META_REQUIRED | {"code_commit"}):
            _error("object_metadata_invalid")
        if len(json.dumps(result)) > _MAX_METADATA_BYTES:
            _error("object_metadata_invalid")
        if result["backup_format_version"] != "1" or result["schema_version"] != str(room.SCHEMA_VERSION):
            _error("object_metadata_invalid")
        if not re.fullmatch(r"[a-f0-9]{64}", result["ciphertext_sha256"]):
            _error("object_metadata_invalid")
        if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", result["backup_id"]):
            _error("object_metadata_invalid")
        if result["tier"] not in {"daily", "weekly", "monthly", "manual"}:
            _error("object_metadata_invalid")
        if type(result["ciphertext_size"]) is not int or result["ciphertext_size"] <= 0:
            _error("object_metadata_invalid")
        if result.get("code_commit") and not re.fullmatch(r"[a-f0-9]{40}", result["code_commit"]):
            _error("object_metadata_invalid")
        return result
    except backup.BackupError:
        raise
    except Exception:
        _error("object_metadata_invalid")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1048576), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _LimitedWriter:
    def __init__(self, target, limit):
        self.target, self.limit, self.count, self.exceeded = target, limit, 0, False

    def write(self, data):
        if self.count + len(data) > self.limit:
            self.exceeded = True
            raise ValueError("bounded_plaintext")
        written = self.target.write(data)
        self.count += written
        return written

    def flush(self):
        return self.target.flush()


_VERIFY_SCRIPT = r'''
import json,sqlite3,sys
from src import becoming as room
from src import becoming_backup as backup
try:
 path=sys.argv[1]
 verified=backup.validate_database(path,immutable=True)
 store=room.RoomStore(path)
 count=0
 with sqlite3.connect(path) as conn:
  conn.row_factory=sqlite3.Row
  entries=[dict(r) for r in conn.execute("SELECT * FROM entries ORDER BY entry_id")]
  for e in entries:
   scope=room.RoomScope(e["owner_id"],e["channel_id"],e["guild_id"])
   current=store.get_entry(e["entry_id"],scope)
   history=store.get_history(e["entry_id"],scope)
   expected=[dict(r) for r in conn.execute("SELECT * FROM revisions WHERE entry_id=? ORDER BY revision_number",(e["entry_id"],))]
   if current is None or current["current_revision"]!=e["current_revision"] or len(history)!=len(expected): raise ValueError()
   for actual,wanted in zip(history,expected):
    if any(actual.get(k)!=v for k,v in wanted.items()): raise ValueError()
   if not expected or any(current.get(k)!=v for k,v in expected[-1].items()): raise ValueError()
   count+=len(history)
  checks=(("revisions","UPDATE revisions SET content=content WHERE rowid=?"),("revisions","DELETE FROM revisions WHERE rowid=?"),("invocations","UPDATE invocations SET outcome=outcome WHERE rowid=?"),("invocations","DELETE FROM invocations WHERE rowid=?"))
  checked=0
  for table,statement in checks:
   where=" WHERE outcome IS NOT NULL" if table=="invocations" else ""
   row=conn.execute("SELECT rowid FROM "+table+where+" LIMIT 1").fetchone()
   if row is None: continue
   conn.execute("SAVEPOINT immutable_probe")
   try:
    conn.execute(statement,(row[0],))
   except sqlite3.IntegrityError:
    checked+=1
   else:
    raise ValueError()
   finally:
    conn.execute("ROLLBACK TO immutable_probe")
    conn.execute("RELEASE immutable_probe")
 print(json.dumps({"verified":True,"entries":len(entries),"revisions":count,"immutable_checks":checked},sort_keys=True))
except BaseException:
 print('{"verified":false}')
 sys.exit(1)
'''


def verify_fresh_process(path: str | Path) -> dict:
    """Retrieve every history through actual RoomStore without emitting rows."""
    try:
        completed = subprocess.run(
            [sys.executable, "-B", "-c", _VERIFY_SCRIPT, str(path)],
            cwd=room.REPOSITORY_ROOT,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, timeout=60, check=False,
        )
        if completed.returncode or len(completed.stdout) > 4096:
            _error("recovery_verification_failed")
        report = json.loads(completed.stdout)
        if report.get("verified") is not True:
            _error("recovery_verification_failed")
        return report
    except backup.BackupError:
        raise
    except Exception:
        _error("recovery_verification_failed")


def restore_ciphertext(
    ciphertext: str | Path, destination_dir: str | Path, *,
    identity_file=None, identity_stdin: BinaryIO | None = None,
    expected_sha256: str, expected_metadata: Mapping[str, object],
    max_plaintext_bytes: int = _DEFAULT_LIMIT,
) -> dict:
    """Restore a verified encrypted bundle only into a new private destination."""
    destination = None
    created = False
    published = False
    try:
        if type(max_plaintext_bytes) is not int or not 1 <= max_plaintext_bytes <= 1073741824:
            _error("ciphertext_invalid")
        metadata = _metadata(expected_metadata)
        if expected_sha256 != metadata["ciphertext_sha256"]:
            _error("ciphertext_hash_mismatch")
        identity = _read_identity(identity_file=identity_file, identity_stdin=identity_stdin)
        encrypted = _local_path(ciphertext)
        info = encrypted.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != metadata["ciphertext_size"]:
            _error("ciphertext_invalid")
        if info.st_size > max_plaintext_bytes * 2 + 65536:
            _error("ciphertext_invalid")
        if _sha256(encrypted) != expected_sha256:
            _error("ciphertext_hash_mismatch")
        destination, created = _new_destination(destination_dir)
        with tempfile.TemporaryDirectory(prefix="room-recovery-", dir=_temporary_root()) as temporary:
            private = Path(temporary)
            os.chmod(private, 0o700)
            archive = private / "recovery.tar"
            limited = None
            try:
                import pyrage
                with encrypted.open("rb") as source, os.fdopen(
                    os.open(archive, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb",
                ) as target:
                    limited = _LimitedWriter(target, max_plaintext_bytes)
                    pyrage.decrypt_io(source, limited, [identity])
                    target.flush()
                    os.fsync(target.fileno())
            except Exception:
                _error("decrypt_failed")
            manifest = backup.validate_archive(archive, max_plaintext_bytes=max_plaintext_bytes)
            for key in ("backup_id", "schema_version", "code_commit"):
                if metadata.get(key) and str(manifest.get(key)) != str(metadata[key]):
                    _error("manifest_mismatch")
            if str(manifest.get("backup_format_version")) != metadata["backup_format_version"]:
                _error("manifest_mismatch")
            extracted = private / "extracted"
            extracted.mkdir(mode=0o700)
            database = backup.extract_validated_archive(
                archive, extracted, max_plaintext_bytes=max_plaintext_bytes,
            )
            report = verify_fresh_process(database)
            # Recheck the destination immediately before publishing. Never use
            # replace(): an existing file, including a raced-in file, must win.
            if any(destination.iterdir()):
                _error("restore_destination_invalid")
            output = destination / "room.sqlite3"
            pending = destination / ".room.sqlite3.pending"
            descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as target, database.open("rb") as source:
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
                if _sha256(pending) != _sha256(database):
                    _error("database_hash_mismatch")
                # link is atomic and fails if the final name already exists;
                # rename/replace could overwrite a raced-in live file.
                os.link(pending, output)
                published = True
            finally:
                pending.unlink(missing_ok=True)
            return {
                "outcome": "restored", "schema_version": room.SCHEMA_VERSION,
                "backup_id": metadata["backup_id"], "ciphertext_sha256": expected_sha256,
                "entries": report["entries"], "revisions": report["revisions"],
                "immutable_checks": report["immutable_checks"],
                "fresh_process_verified": True,
            }
    except backup.BackupError:
        raise
    except Exception:
        _error("restore_io_failure")
    finally:
        if destination is not None and created and not published:
            try:
                destination.rmdir()
            except OSError:
                pass


def restore_object(
    key: str, destination_dir: str | Path, *, transport,
    identity_file=None, identity_stdin: BinaryIO | None = None,
    max_plaintext_bytes: int = _DEFAULT_LIMIT,
) -> dict:
    """Fetch one specific prefix-scoped object using a separate read client."""
    # Reject bad destinations before making a remote request, without creating.
    destination = _local_path(destination_dir)
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        _error("restore_destination_invalid")
    with tempfile.TemporaryDirectory(prefix="room-recovery-download-", dir=_temporary_root()) as temporary:
        private = Path(temporary)
        os.chmod(private, 0o700)
        ciphertext = private / "recovery.tar.age"
        metadata = dict(transport.download_for_restore(key, ciphertext))
        for confirmation in ("retention_verified", "download_verified"):
            if confirmation in metadata and metadata.pop(confirmation) is not True:
                _error("object_metadata_invalid")
        return restore_ciphertext(
            ciphertext, destination, identity_file=identity_file, identity_stdin=identity_stdin,
            expected_sha256=metadata.get("ciphertext_sha256", ""), expected_metadata=metadata,
            max_plaintext_bytes=max_plaintext_bytes,
        )


class SyntheticS3Client:
    """Explicit test-only S3 boundary; stores ciphertext in memory, never network."""
    def __init__(self):
        self.objects = {}
        self.calls = []

    def head_bucket(self, **kwargs):
        self.calls.append(("head_bucket", kwargs))
        return {}

    def get_object_lock_configuration(self, **kwargs):
        self.calls.append(("get_object_lock_configuration", kwargs))
        return {"ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"}}

    def list_objects_v2(self, **kwargs):
        self.calls.append(("list_objects_v2", kwargs))
        return {"Contents": [], "IsTruncated": False}

    def put_object(self, **kwargs):
        self.calls.append(("put_object", {k: v for k, v in kwargs.items() if k != "Body"}))
        source = kwargs["Body"]
        data = source.read() if hasattr(source, "read") else source
        self.objects[kwargs["Key"]] = {**kwargs, "Body": bytes(data)}
        return {"ETag": '"synthetic"', "VersionId": "synthetic-version"}

    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        item = self.objects[kwargs["Key"]]
        return {"ContentLength": len(item["Body"]), "Metadata": item["Metadata"],
                "ContentType": item["ContentType"],
                "ObjectLockMode": item["ObjectLockMode"],
                "ObjectLockRetainUntilDate": item["ObjectLockRetainUntilDate"],
                "VersionId": "synthetic-version"}

    def get_object_retention(self, **kwargs):
        self.calls.append(("get_object_retention", kwargs))
        item = self.objects[kwargs["Key"]]
        return {"Retention": {"Mode": item["ObjectLockMode"],
                              "RetainUntilDate": item["ObjectLockRetainUntilDate"]}}

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        return {**self.head_object(**kwargs), "Body": io.BytesIO(self.objects[kwargs["Key"]]["Body"])}


def _synthetic_fixture(path: Path):
    store = room.RoomStore(path)
    store.initialize()
    scope = room.RoomScope("10", "20", "30")
    first, _ = store.begin_invocation(operation_key="synthetic-create", trigger_type="becoming_want",
                                     scope=scope, requested_model="synthetic/model")
    receipt = store.write_revision(invocation_id=first["invocation_id"], scope=scope,
        result=room.RoomModelResult("create", "Paper lantern", "A synthetic paper lantern."))
    second, _ = store.begin_invocation(operation_key="synthetic-revise", trigger_type="becoming_revise",
                                      scope=scope, requested_model="synthetic/model")
    store.write_revision(invocation_id=second["invocation_id"], scope=scope,
        entry_id=receipt["entry_id"], expected_revision=1,
        result=room.RoomModelResult("revise", "Paper lantern", "A synthetic blue paper lantern.", "Synthetic revision."))
    third, _ = store.begin_invocation(operation_key="synthetic-abstain", trigger_type="becoming_want",
                                     scope=scope, requested_model="synthetic/model")
    store.finish_without_entry(invocation_id=third["invocation_id"], scope=scope, outcome="abstained",
        result=room.RoomModelResult("abstain", reason="Synthetic abstention."))
    return store, receipt["entry_id"]


_APPEND_SCRIPT = r'''
import json,sys
from src import becoming as room
try:
 store=room.RoomStore(sys.argv[1]);scope=room.RoomScope("10","20","30")
 if sys.argv[2]=="append":
  receipt,_=store.begin_invocation(operation_key="synthetic-restored-revision",trigger_type="becoming_revise",scope=scope,requested_model="synthetic/model")
  store.write_revision(invocation_id=receipt["invocation_id"],scope=scope,entry_id=sys.argv[3],expected_revision=2,result=room.RoomModelResult("revise","Paper lantern","A synthetic green paper lantern.","Synthetic restored revision."))
 e=store.get_entry(sys.argv[3],scope);h=store.get_history(sys.argv[3],scope)
 if e["current_revision"]!=3 or len(h)!=3 or e["content"]!="A synthetic green paper lantern.": raise ValueError()
 print('{"verified":true}')
except BaseException:
 print('{"verified":false}');sys.exit(1)
'''


def run_synthetic_drill(*, synthetic=False, test_only=False, s3_client=None, work_root=None,
                        allow_remote_synthetic=False, storage_credentials_file=None,
                        identity_file=None, identity_stdin=None) -> dict:
    """Synthetic data only; remote retention requires a separate explicit opt-in.

    Offline mode is the default available test route. Remote mode uses a local
    identity and separately supplied operations credentials; retained synthetic
    objects intentionally remain subject to normal COMPLIANCE lifecycle rules.
    """
    if synthetic is not True or (test_only is not True and allow_remote_synthetic is not True):
        _error("synthetic_only_required")
    if test_only and allow_remote_synthetic:
        _error("synthetic_only_required")
    if allow_remote_synthetic:
        if not storage_credentials_file:
            _error("object_metadata_invalid")
        identity = _read_identity(identity_file=identity_file, identity_stdin=identity_stdin)
        storage = _credentials_data(storage_credentials_file)
    else:
        if storage_credentials_file or identity_file is not None or identity_stdin is not None:
            _error("synthetic_only_required")
        import pyrage
        identity = pyrage.x25519.Identity.generate()
        storage = {"endpoint_url": "https://s3.us-west-004.backblazeb2.com", "region": "us-west-004",
                   "bucket": "synthetic-room-backups", "prefix": "room-of-becoming",
                   "access_key_id": "synthetic-readwrite", "secret_access_key": "synthetic-secret"}
    work_root = _local_path(work_root) if work_root is not None else _temporary_root()
    with tempfile.TemporaryDirectory(prefix="room-synthetic-drill-", dir=work_root) as temporary:
        private = Path(temporary)
        os.chmod(private, 0o700)
        source = private / "source" / "room.sqlite3"
        store, entry_id = _synthetic_fixture(source)
        room_config = room.RoomConfig.from_env({"BECOMING_ENABLED": "true", "BOT_OWNER_DISCORD_ID": "10",
            "BECOMING_CHANNEL_ID": "20", "BECOMING_GUILD_ID": "30", "BECOMING_DB_PATH": str(source)})
        config = backup.BackupConfig.from_env({
            "BECOMING_BACKUP_ENABLED": "true", "BECOMING_BACKUP_AGE_RECIPIENT": str(identity.to_public()),
            "BECOMING_BACKUP_S3_ENDPOINT_URL": storage["endpoint_url"],
            "BECOMING_BACKUP_S3_REGION": storage["region"], "BECOMING_BACKUP_S3_BUCKET": storage["bucket"],
            "BECOMING_BACKUP_S3_PREFIX": storage["prefix"], "BECOMING_BACKUP_S3_ACCESS_KEY_ID": storage["access_key_id"],
            "BECOMING_BACKUP_S3_SECRET_ACCESS_KEY": storage["secret_access_key"],
            "BECOMING_BACKUP_STATUS_PATH": str(source.parent / "backup-status.json"),
        }, room_config)
        client = s3_client if s3_client is not None else None if allow_remote_synthetic else SyntheticS3Client()
        transport = backup.B2Transport(config, client=client)
        manager = backup.BackupManager(config, store, transport=transport)
        now = datetime.now(timezone.utc)
        outcome = asyncio.run(manager.run(tiers=("manual",), now=now))
        if outcome.get("successful_tiers") != ["manual"]:
            _error("recovery_verification_failed")
        key = f"{config.prefix}/manual/{now:%Y/%m/%d}/{now:%Y%m%dT%H%M%SZ}-{outcome['backup_id']}.tar.age"
        original = backup.validate_database(source, immutable=True)
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(source) + suffix).unlink(missing_ok=True)
        if any(Path(str(source) + suffix).exists() for suffix in ("", "-wal", "-shm", "-journal")):
            _error("recovery_verification_failed")
        destination = private / "restored"
        report = restore_object(key, destination, transport=transport,
            identity_stdin=io.BytesIO(str(identity).encode("ascii")))
        restored = destination / "room.sqlite3"
        recovered = backup.validate_database(restored, immutable=True)
        if any(original[key] != recovered[key] for key in (
            "application_id", "schema_version", "sql_manifest_digest", "row_counts", "revision_count_summary",
        )):
            _error("recovery_verification_failed")
        for operation in ("append", "retrieve"):
            child = subprocess.run([sys.executable, "-B", "-c", _APPEND_SCRIPT, str(restored), operation, entry_id],
                cwd=room.REPOSITORY_ROOT, env={"PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True, timeout=60)
            if child.returncode or json.loads(child.stdout).get("verified") is not True:
                _error("recovery_verification_failed")
        report = {"outcome": "synthetic_drill_passed", "source_removed": True,
                  "restored_revisions": report["revisions"], "restored_receipts": 3,
                  "post_restore_revision": 3, "second_process_verified": True,
                  "artifacts_cleaned": True,
                  "live_storage_verified": bool(allow_remote_synthetic and s3_client is None),
                  "remote_synthetic_object_retained": bool(allow_remote_synthetic)}
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Local Room encrypted recovery administration")
    sub = parser.add_subparsers(dest="operation", required=True)
    restore = sub.add_parser("restore")
    source = restore.add_mutually_exclusive_group(required=True)
    source.add_argument("--ciphertext")
    source.add_argument("--object-key")
    identity = restore.add_mutually_exclusive_group(required=True)
    identity.add_argument("--identity-file")
    identity.add_argument("--identity-stdin", action="store_true")
    restore.add_argument("--destination", required=True)
    restore.add_argument("--ciphertext-sha256")
    restore.add_argument("--metadata-file")
    restore.add_argument("--read-credentials-file")
    restore.add_argument("--max-plaintext-bytes", type=int, default=_DEFAULT_LIMIT)
    drill = sub.add_parser("drill")
    drill.add_argument("--synthetic", action="store_true", required=True)
    mode = drill.add_mutually_exclusive_group(required=True)
    mode.add_argument("--test-only", action="store_true")
    mode.add_argument("--allow-remote-synthetic", action="store_true")
    drill.add_argument("--storage-credentials-file")
    identities = drill.add_mutually_exclusive_group()
    identities.add_argument("--identity-file")
    identities.add_argument("--identity-stdin", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.operation == "drill":
            stream = _stdin_identity() if args.identity_stdin else None
            result = run_synthetic_drill(synthetic=args.synthetic, test_only=args.test_only,
                allow_remote_synthetic=args.allow_remote_synthetic,
                storage_credentials_file=args.storage_credentials_file,
                identity_file=args.identity_file, identity_stdin=stream)
        else:
            # Piped stdin is never echoed; an interactive identity prompt must
            # also suppress terminal echo rather than expose a private key.
            stream = _stdin_identity() if args.identity_stdin else None
            kwargs = {"identity_file": args.identity_file, "identity_stdin": stream,
                      "max_plaintext_bytes": args.max_plaintext_bytes}
            if args.ciphertext:
                if not args.ciphertext_sha256 or not args.metadata_file or args.read_credentials_file:
                    _error("object_metadata_invalid")
                path = _local_path(args.metadata_file)
                if path.stat().st_size > _MAX_METADATA_BYTES:
                    _error("object_metadata_invalid")
                metadata = json.loads(path.read_text("utf-8"))
                result = restore_ciphertext(args.ciphertext, args.destination,
                    expected_sha256=args.ciphertext_sha256, expected_metadata=metadata, **kwargs)
            else:
                if not args.read_credentials_file:
                    _error("object_metadata_invalid")
                transport = _read_transport(args.read_credentials_file)
                result = restore_object(args.object_key, args.destination, transport=transport, **kwargs)
        print(json.dumps(result, sort_keys=True))
        return 0
    except backup.BackupError as error:
        print(json.dumps({"outcome": "failed", "category": error.category}, sort_keys=True))
        return 1
    except Exception:
        print('{"category":"restore_io_failure","outcome":"failed"}')
        return 1


def _stdin_identity():
    if sys.stdin.isatty():
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return io.BytesIO(getpass.getpass("Local age identity: ").encode("ascii"))
    return sys.stdin.buffer


def _credentials_data(path):
    """Explicit local credential file, never ambient production configuration."""
    credentials = _local_path(path)
    try:
        descriptor = os.open(credentials, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
                _error("object_metadata_invalid")
            payload = handle.read(_MAX_METADATA_BYTES + 1)
        if len(payload) > _MAX_METADATA_BYTES:
            _error("object_metadata_invalid")
        data = json.loads(payload)
        expected = {"endpoint_url", "region", "bucket", "prefix", "access_key_id", "secret_access_key"}
        if set(data) != expected or not all(isinstance(v, str) and v for v in data.values()):
            _error("object_metadata_invalid")
        return data
    except backup.BackupError:
        raise
    except Exception:
        _error("object_metadata_invalid")


def _read_transport(path):
    """Separate explicit local read credentials; no running-bot environment."""
    credentials = _local_path(path)
    data = _credentials_data(credentials)
    try:
        # Public recipient is irrelevant to downloads; a fresh synthetic public
        # recipient lets the same strict transport config validator be reused.
        import pyrage
        recipient = str(pyrage.x25519.Identity.generate().to_public())
        config = backup.BackupConfig.from_env({
            "BECOMING_BACKUP_ENABLED": "true", "BECOMING_BACKUP_AGE_RECIPIENT": recipient,
            "BECOMING_BACKUP_S3_ENDPOINT_URL": data["endpoint_url"], "BECOMING_BACKUP_S3_REGION": data["region"],
            "BECOMING_BACKUP_S3_BUCKET": data["bucket"], "BECOMING_BACKUP_S3_PREFIX": data["prefix"],
            "BECOMING_BACKUP_S3_ACCESS_KEY_ID": data["access_key_id"], "BECOMING_BACKUP_S3_SECRET_ACCESS_KEY": data["secret_access_key"],
            "BECOMING_BACKUP_STATUS_PATH": str(credentials.parent / "unused-status.json"),
        }, room.RoomConfig(False, None, None, None, credentials.parent / "unused.sqlite3", False))
        if not config.valid:
            _error("object_metadata_invalid")
        return backup.B2Transport(config)
    except backup.BackupError:
        raise
    except Exception:
        _error("object_metadata_invalid")


if __name__ == "__main__":
    raise SystemExit(main())
