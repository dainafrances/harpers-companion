"""Synthetic-only capture, encryption and operating backup acceptance."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback
import unittest
from unittest.mock import Mock, patch

import pyrage

from src import becoming as room
from src import becoming_backup as backup
from src.becoming_recovery import SyntheticS3Client


NOW = datetime(2026, 10, 19, 3, tzinfo=timezone.utc)
PRIVATE = "SYNTHETIC_PRIVATE_CONTENT_NEVER_OPERATIONAL_METADATA"
SCOPE = room.RoomScope("10", "20", "30")


def state(path):
    result = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        item = Path(str(path) + suffix)
        result[suffix] = (item.exists(), hashlib.sha256(item.read_bytes()).hexdigest() if item.exists() else None)
    return result


def independent_manifest(path):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]
                for table, order in (("entries", "entry_id"), ("invocations", "invocation_id"),
                                     ("revisions", "entry_id,revision_number"))}
    encoded = json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest(), rows


class Fixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="room-backup-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "source" / "room.sqlite3"
        self.capture_dir = self.directory / "capture"
        self.capture_dir.mkdir(mode=0o700)
        self.staging = self.directory / "staging"
        self.staging.mkdir(mode=0o700)
        self.identity = pyrage.x25519.Identity.generate()
        self.room_config = room.RoomConfig.from_env({
            "BECOMING_ENABLED": "true", "BOT_OWNER_DISCORD_ID": "10", "BECOMING_CHANNEL_ID": "20",
            "BECOMING_GUILD_ID": "30", "BECOMING_DB_PATH": str(self.source),
        })
        self.env = {
            "BECOMING_BACKUP_ENABLED": "true",
            "BECOMING_BACKUP_AGE_RECIPIENT": str(self.identity.to_public()),
            "BECOMING_BACKUP_S3_ENDPOINT_URL": "https://s3.us-west-004.backblazeb2.com",
            "BECOMING_BACKUP_S3_REGION": "us-west-004", "BECOMING_BACKUP_S3_BUCKET": "synthetic-room-backups",
            "BECOMING_BACKUP_S3_ACCESS_KEY_ID": "synthetic-upload-key",
            "BECOMING_BACKUP_S3_SECRET_ACCESS_KEY": "synthetic-upload-secret",
        }
        self.config = backup.BackupConfig.from_env(self.env, self.room_config)
        self.assertTrue(self.config.can_run)
        self.keeper = None

    def fixture(self):
        self.store = room.RoomStore(self.source)
        self.store.initialize()
        first, _ = self.store.begin_invocation(operation_key="synthetic-create", trigger_type="becoming_want",
            scope=SCOPE, requested_model="synthetic/model")
        receipt = self.store.write_revision(invocation_id=first["invocation_id"], scope=SCOPE,
            result=room.RoomModelResult("create", "Synthetic want", PRIVATE + " FIRST"))
        self.entry_id = receipt["entry_id"]
        self.keeper = sqlite3.connect(self.source, isolation_level=None, check_same_thread=False)
        self.keeper.execute("PRAGMA journal_mode=WAL")
        self.keeper.execute("PRAGMA wal_autocheckpoint=0")
        # Enter the WAL reader set before other connections write/close; an
        # idle connection that has only changed PRAGMAs need not keep it alive.
        self.keeper.execute("SELECT COUNT(*) FROM entries").fetchone()
        self.addCleanup(self.keeper.close)
        second, _ = self.store.begin_invocation(operation_key="synthetic-revise", trigger_type="becoming_revise",
            scope=SCOPE, requested_model="synthetic/model")
        self.store.write_revision(invocation_id=second["invocation_id"], scope=SCOPE, entry_id=self.entry_id,
            expected_revision=1, result=room.RoomModelResult("revise", "Synthetic want", PRIVATE + " SECOND",
                                                           "Synthetic revised choice."))
        third, _ = self.store.begin_invocation(operation_key="synthetic-abstain", trigger_type="becoming_want",
            scope=SCOPE, requested_model="synthetic/model")
        self.store.finish_without_entry(invocation_id=third["invocation_id"], scope=SCOPE, outcome="abstained",
            result=room.RoomModelResult("abstain", reason="Synthetic abstention."))
        return self.store

    def capture(self, **kwargs):
        return backup.capture_bundle(self.store, self.capture_dir, self.config, now=NOW, **kwargs)

    def assert_error(self, category, operation):
        with self.assertRaises(backup.BackupError) as error:
            operation()
        self.assertEqual(error.exception.category, category)
        self.assertNotIn(PRIVATE, str(error.exception))


class BackupConfigurationTests(Fixture, unittest.TestCase):
    def test_defaults_are_disabled_required_and_three_utc(self):
        config = backup.BackupConfig.from_env({}, self.room_config)
        self.assertFalse(config.enabled or config.can_run)
        self.assertTrue(config.required_for_room)
        self.assertEqual((config.time_utc.hour, config.time_utc.minute, config.time_utc.tzinfo),
                         (3, 0, timezone.utc))
        self.assertEqual(config.max_plaintext_bytes, 134217728)
        self.assertEqual(config.status_path, self.source.parent / "backup-status.json")
        self.assertFalse(config.room_allowed(self.room_config))
        self.assertTrue(config.room_allowed(replace(self.room_config, enabled=False)))

    def test_valid_public_recipient_and_complete_explicit_config(self):
        self.assertTrue(self.config.room_allowed(self.room_config))
        representation = repr(self.config)
        for value in (str(self.identity.to_public()), "synthetic-upload-secret", "synthetic-upload-key",
                      "synthetic-room-backups", "backblazeb2.com"):
            self.assertNotIn(value, representation)
        self.assertFalse(self.source.exists() or self.source.parent.exists())

    def test_every_missing_storage_or_recipient_setting_fails_closed_without_io(self):
        for name in self.env:
            if name == "BECOMING_BACKUP_ENABLED":
                continue
            env = {key: value for key, value in self.env.items() if key != name}
            with self.subTest(name=name), patch.object(backup, "capture_bundle") as capture, patch.object(
                sqlite3, "connect", side_effect=AssertionError("SQLite access denied")), patch.object(
                backup, "B2Transport", side_effect=AssertionError("network access denied")):
                config = backup.BackupConfig.from_env(env, self.room_config)
                self.assertFalse(config.can_run or config.room_allowed(self.room_config))
                capture.assert_not_called()

    def test_private_identity_or_identity_environment_is_never_accepted(self):
        for changes in (
            {"BECOMING_BACKUP_AGE_RECIPIENT": str(self.identity)},
            {"BECOMING_BACKUP_AGE_RECIPIENT": str(self.identity.to_public()) + " " + str(self.identity.to_public())},
            {"AGE_IDENTITY": str(self.identity)},
            {"AGE_IDENTITY_FILE": "/synthetic/local/identity"},
            {"UNRELATED_SETTING": str(self.identity)},
        ):
            config = backup.BackupConfig.from_env({**self.env, **changes}, self.room_config)
            self.assertFalse(config.can_run)
            self.assertNotIn(str(self.identity), repr(config))

    def test_rejected_private_identity_and_credentials_are_discarded_from_all_config_fields(self):
        private_identity = str(self.identity)
        for changes in (
            {"BECOMING_BACKUP_AGE_RECIPIENT": private_identity},
            {"AGE_IDENTITY": private_identity},
            {"BECOMING_BACKUP_S3_SECRET_ACCESS_KEY": private_identity},
        ):
            with self.subTest(setting=next(iter(changes))):
                config = backup.BackupConfig.from_env({**self.env, **changes}, self.room_config)
                self.assertFalse(config.valid or config.can_run)
                self.assertEqual(config.age_recipient, "")
                for setting in fields(config):
                    self.assertNotIn(private_identity, str(getattr(config, setting.name)))
                for setting in ("age_recipient", "endpoint_url", "region", "bucket", "access_key_id", "secret_access_key"):
                    self.assertEqual(getattr(config, setting), "")

    def test_malformed_recipient_is_rejected_before_store_or_storage_access(self):
        for recipient in ("", "age1" + "q" * 58, "not-an-age-recipient", "AGE-SECRET-KEY-synthetic"):
            with self.subTest(length=len(recipient)), patch.object(backup, "B2Transport") as transport:
                config = backup.BackupConfig.from_env({**self.env, "BECOMING_BACKUP_AGE_RECIPIENT": recipient}, self.room_config)
                self.assertFalse(config.can_run)
                transport.assert_not_called()
                self.assertFalse(self.source.exists())

    def test_strict_utc_time_and_bounded_size_timeout_parsing(self):
        invalid = {
            "TIME_UTC": ("3:00", "24:00", "03:60", "03:00Z", "03:00:00", "UTC03:00"),
            "MAX_PLAINTEXT_BYTES": ("0", "-1", "268435457", "1.2", "999999999999999"),
            "TIMEOUT_SECONDS": ("0", "9", "901", "NaN", "1e2"),
            "ENABLED": ("yes", "1", "FALSE"), "REQUIRED_FOR_ROOM": ("maybe", "0"),
        }
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    config = backup.BackupConfig.from_env({**self.env, "BECOMING_BACKUP_" + key: value}, self.room_config)
                    self.assertFalse(config.can_run)
        config = backup.BackupConfig.from_env({**self.env, "BECOMING_BACKUP_TIME_UTC": "23:59"}, self.room_config)
        self.assertTrue(config.can_run)
        self.assertEqual((config.time_utc.hour, config.time_utc.minute), (23, 59))

    def test_endpoint_region_bucket_and_prefix_are_restricted(self):
        for setting, value in (
            ("S3_ENDPOINT_URL", "http://s3.us-west-004.backblazeb2.com"),
            ("S3_ENDPOINT_URL", "https://s3.us-west-004.backblazeb2.com/"),
            ("S3_ENDPOINT_URL", "https://synthetic.invalid"), ("S3_REGION", "us-west-2"),
            ("S3_BUCKET", "../bucket"), ("S3_PREFIX", "../private"),
            ("S3_PREFIX", "room//daily"), ("S3_PREFIX", "room/private route"),
        ):
            with self.subTest(setting=setting, value=value):
                config = backup.BackupConfig.from_env({**self.env, "BECOMING_BACKUP_" + setting: value}, self.room_config)
                self.assertFalse(config.can_run)

    def test_status_path_collision_repository_symlink_and_other_directory_rejected(self):
        self.source.parent.mkdir()
        outside = self.directory / "status.json"
        link = self.source.parent / "linked.json"
        link.symlink_to(outside)
        for target in (self.source, Path(str(self.source) + "-wal"), outside, link,
                       room.REPOSITORY_ROOT / "data" / "backup-status.json"):
            with self.subTest(name=target.name):
                config = backup.BackupConfig.from_env({**self.env, "BECOMING_BACKUP_STATUS_PATH": str(target)}, self.room_config)
                self.assertFalse(config.can_run)

    def test_required_gate_defaults_fail_closed_and_explicit_override_is_narrow(self):
        config = backup.BackupConfig.from_env({"BECOMING_BACKUP_REQUIRED_FOR_ROOM": "false"}, self.room_config)
        self.assertTrue(config.room_allowed(self.room_config))
        self.assertFalse(config.can_run)
        for value in ("bad", "TRUE", "1"):
            config = backup.BackupConfig.from_env({"BECOMING_BACKUP_REQUIRED_FOR_ROOM": value}, self.room_config)
            self.assertFalse(config.room_allowed(self.room_config))

    def test_environment_cannot_reduce_fixed_retention_minima(self):
        config = backup.BackupConfig.from_env({**self.env, "BECOMING_BACKUP_DAILY_DAYS": "1",
            "BECOMING_BACKUP_MANUAL_DAYS": "1", "BECOMING_BACKUP_RETENTION_DAYS": "1"}, self.room_config)
        self.assertTrue(config.can_run)
        self.assertEqual(backup.RETENTION_DAYS, {"daily": 30, "weekly": 84, "monthly": 366, "manual": 90})

    def test_disabled_complete_and_malformed_saved_settings_never_parse_recipient_or_retain_secrets(self):
        for malformed in (False, True):
            env = {**self.env, "BECOMING_BACKUP_ENABLED": "false"}
            if malformed:
                env.update(BECOMING_BACKUP_AGE_RECIPIENT=str(self.identity),
                           BECOMING_BACKUP_S3_SECRET_ACCESS_KEY=PRIVATE,
                           BECOMING_BACKUP_TIME_UTC="not-a-time",
                           BECOMING_BACKUP_STATUS_PATH=str(self.directory / PRIVATE))
            with self.subTest(malformed=malformed), patch.object(
                pyrage.x25519.Recipient, "from_str", side_effect=AssertionError("Recipient parsing denied")) as parse, patch.object(
                backup, "_safe_status_path", side_effect=AssertionError("Storage validation denied")) as storage:
                config = backup.BackupConfig.from_env(env, self.room_config)
            self.assertFalse(config.enabled or config.can_run)
            self.assertTrue(config.required_for_room)
            self.assertFalse(config.room_allowed(self.room_config))
            parse.assert_not_called()
            storage.assert_not_called()
            for name in ("age_recipient", "endpoint_url", "region", "bucket", "access_key_id", "secret_access_key"):
                self.assertEqual(getattr(config, name), "")
            self.assertNotIn(PRIVATE, repr(config))

    def test_fresh_disabled_startup_saved_settings_is_inert_and_preserves_ordinary_operation(self):
        code = r'''
import asyncio, builtins, dataclasses, io, json, logging, os, socket, sys
from contextlib import ExitStack, redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
counts = {"optional_imports": 0, "network": 0, "writes": 0}
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.split(".", 1)[0] in {"pyrage", "boto3", "botocore"}:
        counts["optional_imports"] += 1
        raise AssertionError("optional dependency import denied")
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
def audited(event, args):
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo"}:
        counts["network"] += 1
        raise AssertionError("network denied")
    if event == "open" and isinstance(args[2], int) and args[2] & (os.O_CREAT | os.O_WRONLY | os.O_RDWR | os.O_TRUNC | os.O_APPEND):
        counts["writes"] += 1
        raise AssertionError("filesystem write denied")
    if event in {"os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.link", "os.symlink", "os.chmod"}:
        counts["writes"] += 1
        raise AssertionError("filesystem mutation denied")
sys.addaudithook(audited)
import src.becoming_backup as backup
output = io.StringIO()
with ExitStack() as stack, redirect_stdout(output):
    path_guard = stack.enter_context(patch.object(backup, "_safe_status_path", side_effect=AssertionError("storage access denied")))
    manager = stack.enter_context(patch.object(backup, "BackupManager", side_effect=AssertionError("manager denied")))
    client = stack.enter_context(patch.object(backup, "B2Transport", side_effect=AssertionError("client denied")))
    import src.main as main
    scheduler = stack.enter_context(patch.object(main.becoming_backup_discord, "BackupScheduler", side_effect=AssertionError("scheduler denied")))
    register = stack.enter_context(patch.object(main.becoming_backup_discord, "register_commands", side_effect=AssertionError("backup commands denied")))
    stack.enter_context(patch.object(main.memory, "init_db"))
    stack.enter_context(patch.object(main.bot.tree, "sync", AsyncMock(return_value=[])))
    stack.enter_context(patch.object(main.becoming_commands, "sync_room_commands", AsyncMock()))
    journal = SimpleNamespace(is_running=Mock(return_value=False), start=Mock())
    refresh = SimpleNamespace(is_running=Mock(return_value=False), start=Mock())
    stack.enter_context(patch.object(main, "nightly_journal", journal))
    stack.enter_context(patch.object(main, "periodic_status_refresh", refresh))
    stack.enter_context(patch.object(main, "DISCORD_STATUS_ENABLED", True))
    stack.enter_context(patch.object(main, "DISCORD_STATUS_AUTO_ENABLED", True))
    stack.enter_context(patch.object(main, "status_presence", SimpleNamespace()))
    async def verify():
        await main._start_commands_and_jobs()
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        await main.bot.tree.get_command("ping").callback(interaction)
        return interaction.response.send_message.await_count
    ping = asyncio.run(verify())
    assert not main.becoming_backup_config.enabled and not main.becoming_backup_config.can_run
    assert all(getattr(main.becoming_backup_config, name) == "" for name in
               ("age_recipient", "endpoint_url", "region", "bucket", "access_key_id", "secret_access_key"))
    result = {**counts, "storage_checks": path_guard.call_count, "client": client.call_count,
              "manager": manager.call_count, "scheduler": scheduler.call_count, "registration": register.call_count,
              "backup_objects": main.becoming_backup_manager is not None or main.becoming_backup_scheduler is not None,
              "backup_commands": [command.name for command in main.bot.tree.get_commands() if command.name.startswith("becoming_backup_")],
              "room_service": main.becoming_service is not None, "ordinary_started": main.startup_synced,
              "journal_starts": journal.start.call_count, "status_starts": refresh.start.call_count, "ping": ping}
print(json.dumps(result))
'''
        for enabled, required, expected_service in (("false", "true", False), ("true", "true", False),
                                                    ("true", "false", True)):
            for malformed in (False, True):
                env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1",
                       "DISCORD_TOKEN": "synthetic-token", "OPENROUTER_API_KEY": "synthetic-key",
                       "BECOMING_ENABLED": enabled, "BOT_OWNER_DISCORD_ID": "10", "BECOMING_CHANNEL_ID": "20",
                       "BECOMING_GUILD_ID": "30", "BECOMING_DB_PATH": str(self.source), **self.env,
                       "BECOMING_BACKUP_ENABLED": "false", "BECOMING_BACKUP_REQUIRED_FOR_ROOM": required}
                if malformed:
                    env.update(BECOMING_BACKUP_AGE_RECIPIENT="AGE-SECRET-KEY-" + PRIVATE,
                               BECOMING_BACKUP_S3_ENDPOINT_URL="malformed-saved-endpoint",
                               BECOMING_BACKUP_S3_SECRET_ACCESS_KEY=PRIVATE,
                               BECOMING_BACKUP_TIME_UTC="malformed-saved-time",
                               BECOMING_BACKUP_MAX_PLAINTEXT_BYTES="malformed-saved-limit")
                with self.subTest(room=enabled, required=required, malformed=malformed):
                    child = subprocess.run([sys.executable, "-B", "-c", code], env=env, text=True,
                                           capture_output=True, timeout=20, check=True)
                    self.assertEqual(json.loads(child.stdout.splitlines()[-1]), {
                        "optional_imports": 0, "network": 0, "writes": 0, "storage_checks": 0,
                        "client": 0, "manager": 0, "scheduler": 0, "registration": 0,
                        "backup_objects": False, "backup_commands": [], "room_service": expected_service,
                        "ordinary_started": True, "journal_starts": 1, "status_starts": 1, "ping": 1})
                    self.assertNotIn(PRIVATE, child.stdout + child.stderr)
                    self.assertFalse(self.source.exists() or self.config.status_path.exists())


class BackupCaptureTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fixture()

    def test_wal_capture_contains_two_revisions_three_receipts_main_only_is_stale(self):
        self.assertGreater(Path(str(self.source) + "-wal").stat().st_size, 0)
        control = self.directory / "main-only.sqlite3"
        shutil.copyfile(self.source, control)
        _, stale = independent_manifest(control)
        self.assertEqual(len(stale["revisions"]), 1)
        self.assertEqual(len(stale["invocations"]), 1)
        before = state(self.source)
        result = self.capture()
        self.assertEqual(state(self.source), before)
        self.assertEqual(result.manifest["source_journal_mode"], "wal")
        self.assertEqual(result.manifest["row_counts"], {"entries": 1, "revisions": 2, "invocations": 3})
        restored = self.directory / "captured-validation"
        restored.mkdir()
        path = backup.extract_validated_archive(result.archive_path, restored)
        digest, rows = independent_manifest(path)
        self.assertEqual(digest, result.manifest["sql_manifest_digest"])
        self.assertEqual([row["content"] for row in rows["revisions"]], [PRIVATE + " FIRST", PRIVATE + " SECOND"])
        self.assertEqual([row["parent_revision_number"] for row in rows["revisions"]], [None, 1])
        self.assertEqual(rows["entries"][0]["current_revision"], 2)
        self.assertEqual({row["outcome"] for row in rows["invocations"]}, {"created", "revised", "abstained"})

    def test_manifest_is_bounded_content_neutral_and_archive_files_private(self):
        result = self.capture(code_commit="a" * 40)
        encoded = json.dumps(result.manifest)
        self.assertNotIn(PRIVATE, encoded)
        self.assertNotIn("Synthetic want", encoded)
        self.assertNotIn("Synthetic revised choice", encoded)
        self.assertNotIn("owner_id", encoded)
        self.assertLess(len(encoded), backup.MAX_MANIFEST_BYTES)
        self.assertEqual(result.manifest["code_commit"], "a" * 40)
        self.assertEqual(result.archive_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.capture_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(self.capture_dir.iterdir()), [result.archive_path])
        with tarfile.open(result.archive_path) as archive:
            self.assertEqual({item.name for item in archive.getmembers()}, {"room.sqlite3", "manifest.json"})
            self.assertTrue(all(item.mode == 0o600 for item in archive.getmembers()))

    def test_nonempty_rollback_journal_requires_maintenance_without_opening_original(self):
        journal = Path(str(self.source) + "-journal")
        journal.write_bytes(b"synthetic required rollback state")
        before = state(self.source)
        with patch.object(sqlite3, "connect", side_effect=AssertionError("Original must not be opened")):
            self.assert_error("maintenance_required", self.capture)
        self.assertEqual(state(self.source), before)
        self.assertEqual(list(self.capture_dir.iterdir()), [])

    def test_missing_source_does_not_create_database_or_parent(self):
        missing = self.directory / "absent" / "room.sqlite3"
        store = room.RoomStore(missing)
        config = replace(self.config, db_path=missing, status_path=missing.parent / "backup-status.json")
        self.assert_error("source_missing", lambda: backup.capture_bundle(store, self.capture_dir, config))
        self.assertFalse(missing.exists() or missing.parent.exists())

    def test_invalid_application_marker_and_schema_fingerprint_are_rejected(self):
        for mutation in ("PRAGMA application_id=17", "CREATE TABLE unexpected_table(value TEXT)"):
            with self.subTest(mutation=mutation):
                self.keeper.execute(mutation)
                before = state(self.source)
                self.assert_error("schema_mismatch", self.capture)
                self.assertEqual(state(self.source), before)
                self.assertEqual(list(self.capture_dir.iterdir()), [])
                if mutation.startswith("PRAGMA"):
                    self.keeper.execute(f"PRAGMA application_id={room.APPLICATION_ID}")
                else:
                    self.keeper.execute("DROP TABLE unexpected_table")

    def test_actual_integrity_check_failure_rejected_and_original_unchanged(self):
        self.keeper.execute("PRAGMA ignore_check_constraints=ON")
        self.keeper.execute("UPDATE entries SET kind='invalid-kind'")
        self.keeper.execute("PRAGMA ignore_check_constraints=OFF")
        before = state(self.source)
        self.assert_error("integrity_failure", self.capture)
        self.assertEqual(state(self.source), before)
        self.assertEqual(list(self.capture_dir.iterdir()), [])

    def test_actual_foreign_key_failure_rejected_and_original_unchanged(self):
        self.keeper.execute("PRAGMA foreign_keys=OFF")
        self.keeper.execute("UPDATE entries SET current_revision=999")
        before = state(self.source)
        self.assert_error("integrity_failure", self.capture)
        self.assertEqual(state(self.source), before)
        self.assertEqual(list(self.capture_dir.iterdir()), [])

    def test_required_immutability_triggers_part_of_exact_schema(self):
        self.keeper.execute("DROP TRIGGER immutable_revision_update")
        before = state(self.source)
        self.assert_error("schema_mismatch", self.capture)
        self.assertEqual(state(self.source), before)

    def test_no_sqlite_connection_targets_original_or_original_sidecars(self):
        original = sqlite3.connect
        connections = []

        def connect(path, *args, **kwargs):
            connections.append(str(path))
            self.assertNotEqual(str(path), str(self.source))
            self.assertFalse(str(path).startswith(self.source.as_uri()))
            return original(path, *args, **kwargs)

        with patch.object(sqlite3, "connect", side_effect=connect):
            self.capture()
        self.assertTrue(connections)

    def test_concurrent_room_writer_waits_for_capture_then_resumes(self):
        entered, release, writer_started = threading.Event(), threading.Event(), threading.Event()
        original = backup.validate_database

        def validating(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise AssertionError("capture release missing")
            return original(*args, **kwargs)

        def writer():
            writer_started.set()
            return self.store.begin_invocation(operation_key="synthetic-concurrent-writer",
                trigger_type="becoming_want", scope=SCOPE, requested_model="synthetic/model")

        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(backup, "validate_database", side_effect=validating):
            captured = pool.submit(self.capture)
            self.assertTrue(entered.wait(10))
            writing = pool.submit(writer)
            self.assertTrue(writer_started.wait(10))
            try:
                with self.assertRaises(FutureTimeout):
                    writing.result(timeout=0.05)
            finally:
                release.set()
            result = captured.result(timeout=10)
            invocation, created = writing.result(timeout=10)
        self.assertTrue(created)
        self.assertEqual(invocation["outcome"], None)
        self.assertEqual(result.manifest["row_counts"]["invocations"], 3)

    def test_plaintext_source_ceiling_checked_before_sqlite_and_no_partial_files(self):
        config = replace(self.config, max_plaintext_bytes=1)
        before = state(self.source)
        with patch.object(sqlite3, "connect", side_effect=AssertionError("Too large before SQLite")):
            self.assert_error("plaintext_too_large", lambda: backup.capture_bundle(self.store, self.capture_dir, config))
        self.assertEqual(state(self.source), before)
        self.assertEqual(list(self.capture_dir.iterdir()), [])

    def test_archive_ceiling_rejects_before_encryption_and_cleans_staging(self):
        source_bytes = sum(Path(str(self.source) + suffix).stat().st_size for suffix in ("", "-wal"))
        self.keeper.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        config = replace(self.config, max_plaintext_bytes=self.source.stat().st_size + 1000)
        self.assert_error("plaintext_too_large", lambda: backup.capture_bundle(self.store, self.capture_dir, config))
        self.assertGreater(source_bytes, 0)
        self.assertEqual(list(self.capture_dir.iterdir()), [])

    def test_expired_deadline_and_cancellation_leave_source_and_destination_unchanged(self):
        before = state(self.source)
        self.assert_error("timeout", lambda: self.capture(deadline=time.monotonic() - 1))
        cancel = threading.Event()
        cancel.set()
        self.assert_error("cancelled", lambda: self.capture(cancel=cancel))
        self.assertEqual(state(self.source), before)
        self.assertEqual(list(self.capture_dir.iterdir()), [])


class BackupEncryptionTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.plaintext = self.directory / "synthetic-plaintext.tar"
        self.plaintext.write_bytes((PRIVATE + "\n").encode() * 2048)
        self.ciphertext = self.directory / "synthetic-ciphertext.tar.age"

    def encrypt(self, **kwargs):
        return backup.encrypt_archive(self.plaintext, self.ciphertext, str(self.identity.to_public()), **kwargs)

    def test_streaming_encryption_decrypts_exact_plaintext_and_final_file_is_private(self):
        result = self.encrypt()
        self.assertEqual(pyrage.decrypt(self.ciphertext.read_bytes(), [self.identity]), self.plaintext.read_bytes())
        self.assertEqual(result["ciphertext_sha256"], hashlib.sha256(self.ciphertext.read_bytes()).hexdigest())
        self.assertEqual(result["ciphertext_size"], self.ciphertext.stat().st_size)
        self.assertEqual(self.ciphertext.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(PRIVATE.encode(), self.ciphertext.read_bytes())
        self.assertEqual(list(self.directory.glob(".encrypt-*.pending")), [])

    def test_size_rejected_before_encryptor_reads_plaintext(self):
        with patch.object(pyrage, "encrypt_io") as encrypt:
            self.assert_error("plaintext_too_large", lambda: self.encrypt(max_plaintext_bytes=16))
        encrypt.assert_not_called()
        self.assertFalse(self.ciphertext.exists())

    def test_private_or_malformed_recipient_rejected_before_encryptor(self):
        for recipient in (str(self.identity), "invalid-recipient"):
            with patch.object(pyrage, "encrypt_io") as encrypt:
                self.assert_error("invalid_configuration", lambda: backup.encrypt_archive(
                    self.plaintext, self.ciphertext, recipient))
            encrypt.assert_not_called()
        self.assertFalse(self.ciphertext.exists())

    def test_interrupted_encryption_never_publishes_partial_ciphertext(self):
        def interrupted(reader, writer, recipients):
            writer.write(b"synthetic partial ciphertext")
            raise KeyboardInterrupt(PRIVATE)

        with patch.object(pyrage, "encrypt_io", side_effect=interrupted):
            self.assert_error("cancelled", self.encrypt)
        self.assertFalse(self.ciphertext.exists())
        self.assertEqual(list(self.directory.glob(".encrypt-*.pending")), [])

    def test_raw_encryption_error_is_bounded_and_leaves_no_final_or_pending_file(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), patch.object(
            pyrage, "encrypt_io", side_effect=RuntimeError(PRIVATE)):
            self.assert_error("encryption_failed", self.encrypt)
        self.assertFalse(self.ciphertext.exists())
        self.assertEqual(list(self.directory.glob(".encrypt-*.pending")), [])
        self.assertNotIn(PRIVATE, stdout.getvalue() + stderr.getvalue())

    def test_existing_destination_bytes_preserved(self):
        self.ciphertext.write_bytes(b"synthetic existing ciphertext")
        self.assert_error("destination_exists", self.encrypt)
        self.assertEqual(self.ciphertext.read_bytes(), b"synthetic existing ciphertext")
        self.assertEqual(list(self.directory.glob(".encrypt-*.pending")), [])

    def test_hash_failure_after_publication_removes_unverified_final_ciphertext(self):
        with patch.object(backup, "file_sha256", side_effect=OSError(PRIVATE)):
            self.assert_error("encryption_failed", self.encrypt)
        self.assertFalse(self.ciphertext.exists())
        self.assertEqual(list(self.directory.glob(".encrypt-*.pending")), [])


class BackupCleanupRegressionTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fixture()

    def bounded_failure(self, operation, category, *, cleanup_failed):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logging.getLogger().addHandler(handler)
        try:
            with redirect_stdout(output), redirect_stderr(output), patch.object(backup, "B2Transport") as transport:
                try:
                    operation()
                except backup.BackupError as error:
                    text = "".join(traceback.format_exception(error)) + str(error) + repr(error) + repr(error.__dict__)
                    self.assertEqual(error.category, category)
                    self.assertEqual(error.cleanup_failed, cleanup_failed)
                    self.assertIsNone(error.__cause__)
                    self.assertIsNone(error.__context__)
                else:
                    self.fail("Cleanup fault was recorded as success")
                transport.assert_not_called()
        finally:
            logging.getLogger().removeHandler(handler)
        for private in (PRIVATE, str(self.directory)):
            self.assertNotIn(private, text + output.getvalue())
        self.assertFalse(self.config.status_path.exists())

    def assert_writer_progress(self):
        def writer():
            # RLock reentry on the test's capture thread would hide H1. Require
            # acquisition and a real Room write from a different thread.
            acquired = room._STORAGE_LOCK.acquire(timeout=0.5)
            if not acquired:
                return False
            try:
                _, created = self.store.begin_invocation(operation_key="synthetic-after-cleanup-fault",
                    trigger_type="becoming_want", scope=SCOPE, requested_model="synthetic/model")
                return created
            finally:
                room._STORAGE_LOCK.release()
        with ThreadPoolExecutor(max_workers=1) as pool:
            self.assertTrue(pool.submit(writer).result(timeout=2))

    def assert_only_blocked_artifact(self, blocked):
        remaining = set(self.capture_dir.iterdir())
        self.assertLessEqual(remaining, {blocked})
        self.assertFalse((self.capture_dir / "room.sqlite3").exists())

    def test_successful_capture_unlink_failure_is_bounded_and_never_strands_storage_lock(self):
        original = Path.unlink
        blocked = self.capture_dir / ".capture-source.sqlite3"
        def unlink(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError(PRIVATE + str(path))
            return original(path, *args, **kwargs)
        with patch.object(room, "_STORAGE_LOCK", threading.RLock()):
            with patch.object(Path, "unlink", unlink):
                self.bounded_failure(self.capture, "cleanup_failure", cleanup_failed=True)
            self.assert_writer_progress()
        self.assert_only_blocked_artifact(blocked)

    def test_connection_close_failure_preserves_primary_failure_and_runs_later_cleanup(self):
        original = sqlite3.connect
        closed = []
        class Connection:
            def __init__(self, value):
                self.connection = value
            def __getattr__(self, name):
                return getattr(self.connection, name)
            def close(self):
                self.connection.close()
                closed.append(True)
                raise OSError(PRIVATE)
        def connect(*args, **kwargs):
            return Connection(original(*args, **kwargs))
        with patch.object(room, "_STORAGE_LOCK", threading.RLock()):
            with patch.object(sqlite3, "connect", side_effect=connect), patch.object(
                backup, "_sql_manifest", side_effect=backup.BackupError("integrity_failure")):
                self.bounded_failure(self.capture, "integrity_failure", cleanup_failed=True)
            self.assert_writer_progress()
        self.assertTrue(closed)
        self.assertEqual(list(self.capture_dir.iterdir()), [])

    def test_archive_unlink_failure_preserves_validation_category_and_cleans_database_copies(self):
        original = Path.unlink
        blocked = self.capture_dir / "bundle.tar"
        def unlink(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError(PRIVATE + str(path))
            return original(path, *args, **kwargs)
        with patch.object(room, "_STORAGE_LOCK", threading.RLock()):
            with patch.object(Path, "unlink", unlink), patch.object(
                backup, "validate_archive", side_effect=backup.BackupError("archive_invalid")):
                self.bounded_failure(self.capture, "archive_invalid", cleanup_failed=True)
            self.assert_writer_progress()
        self.assert_only_blocked_artifact(blocked)

    def test_pending_ciphertext_cleanup_failure_preserves_encryption_category_and_private_error(self):
        plain = self.directory / "synthetic-plaintext.tar"
        plain.write_bytes(PRIVATE.encode())
        destination = self.directory / "synthetic-ciphertext.tar.age"
        original = Path.unlink
        def unlink(path, *args, **kwargs):
            if path.name.startswith(".encrypt-"):
                raise PermissionError(PRIVATE + str(path))
            return original(path, *args, **kwargs)
        def encrypt(reader, writer, recipients):
            writer.write(b"synthetic partial ciphertext")
            raise RuntimeError(PRIVATE)
        with patch.object(room, "_STORAGE_LOCK", threading.RLock()):
            with patch.object(Path, "unlink", unlink), patch.object(pyrage, "encrypt_io", side_effect=encrypt):
                self.bounded_failure(lambda: backup.encrypt_archive(plain, destination, str(self.identity.to_public())),
                                     "encryption_failed", cleanup_failed=True)
            self.assert_writer_progress()
        self.assertFalse(destination.exists())
        self.assertEqual(len(list(self.directory.glob(".encrypt-*.pending"))), 1)

    def test_cancellation_during_cleanup_is_bounded_and_later_cleanup_still_runs(self):
        original = Path.unlink
        blocked = self.capture_dir / ".capture-source.sqlite3"
        def unlink(path, *args, **kwargs):
            if path == blocked:
                raise asyncio.CancelledError(PRIVATE + str(path))
            return original(path, *args, **kwargs)
        with patch.object(room, "_STORAGE_LOCK", threading.RLock()):
            with patch.object(Path, "unlink", unlink):
                self.bounded_failure(self.capture, "cancelled", cleanup_failed=True)
            self.assert_writer_progress()
        self.assert_only_blocked_artifact(blocked)

    def test_timeout_reached_during_cleanup_cannot_return_success_or_skip_remaining_removals(self):
        original_unlink, original_clock = Path.unlink, time.monotonic
        deadline = original_clock() + 100
        expired = False
        def clock():
            return deadline + 1 if expired else original_clock()
        def unlink(path, *args, **kwargs):
            nonlocal expired
            result = original_unlink(path, *args, **kwargs)
            if path.parent == self.capture_dir:
                expired = True
            return result
        with patch.object(room, "_STORAGE_LOCK", threading.RLock()):
            with patch.object(Path, "unlink", unlink), patch.object(backup.monotonic_time, "monotonic", side_effect=clock):
                self.bounded_failure(lambda: self.capture(deadline=deadline), "timeout", cleanup_failed=False)
            self.assert_writer_progress()
        self.assertTrue(expired)
        self.assertEqual(list(self.capture_dir.iterdir()), [])

    def test_immutability_rollback_failure_cannot_replace_primary_validation_category(self):
        original = sqlite3.connect
        class Connection:
            def __init__(self, connection):
                object.__setattr__(self, "connection", connection)
            def __getattr__(self, name):
                return getattr(self.connection, name)
            def __setattr__(self, name, value):
                setattr(self.connection, name, value)
            def execute(self, sql, *args):
                if sql == "UPDATE revisions SET title=title":
                    raise sqlite3.IntegrityError(PRIVATE)
                if sql == "ROLLBACK":
                    raise sqlite3.OperationalError(PRIVATE)
                return self.connection.execute(sql, *args)
        with patch.object(room, "_STORAGE_LOCK", threading.RLock()):
            with patch.object(sqlite3, "connect", side_effect=lambda *args, **kwargs: Connection(original(*args, **kwargs))):
                self.bounded_failure(lambda: backup.validate_database(self.source),
                                     "immutability_failure", cleanup_failed=True)
            self.assert_writer_progress()

    def test_hash_stream_close_failure_preserves_primary_cancellation_after_encryption(self):
        plain = self.directory / "synthetic-plaintext.tar"
        plain.write_bytes(PRIVATE.encode())
        destination = self.directory / "synthetic-ciphertext.tar.age"
        original = Path.open
        cancel = threading.Event()
        class Stream:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.close()
            def read(self, *args):
                value = self.stream.read(*args)
                cancel.set()
                return value
            def close(self):
                self.stream.close()
                raise OSError(PRIVATE)
        def opened(path, *args, **kwargs):
            stream = original(path, *args, **kwargs)
            return Stream(stream) if path.name.startswith(".encrypt-") and args == ("rb",) else stream
        with patch.object(room, "_STORAGE_LOCK", threading.RLock()):
            with patch.object(Path, "open", opened):
                self.bounded_failure(lambda: backup.encrypt_archive(plain, destination, str(self.identity.to_public()), cancel=cancel),
                                     "cancelled", cleanup_failed=True)
            self.assert_writer_progress()
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.directory.glob(".encrypt-*.pending")), [])


class BackupManagerTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.fixture()
        self.client = SyntheticS3Client()
        self.transport = backup.B2Transport(self.config, client=self.client, clock=lambda: NOW)
        self.manager = backup.BackupManager(self.config, self.store, transport=self.transport, code_commit="a" * 40)
        self.temp_patch = patch.object(backup, "_temp_parent", return_value=self.staging)
        self.temp_patch.start()
        self.addCleanup(self.temp_patch.stop)

    async def test_success_requires_full_remote_download_and_cleans_all_temporary_plaintext(self):
        before = state(self.source)
        result = await self.manager.run(tiers=("manual",), now=NOW)
        self.assertEqual(result["outcome_category"], "success")
        self.assertEqual(result["successful_tiers"], ["manual"])
        self.assertTrue(result["retention_verified"] and result["download_verified"])
        self.assertEqual(state(self.source), before)
        self.assertEqual(list(self.staging.iterdir()), [])
        self.assertEqual({call[0] for call in self.client.calls} & {"get_object", "get_object_retention"},
                         {"get_object", "get_object_retention"})
        encoded = json.dumps(result) + self.config.status_path.read_text()
        self.assertNotIn(PRIVATE, encoded)
        self.assertNotIn(str(self.identity.to_public()), encoded)
        self.assertNotIn("synthetic-upload", encoded)
        self.assertEqual(self.config.status_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.source.parent.glob(".backup-status-*.pending")), [])

    async def test_multi_tier_same_snapshot_independent_uploads_and_partial_success_honest(self):
        original = self.transport.upload_and_verify
        seen = []

        def upload(path, *, key, metadata, retain_until, **kwargs):
            seen.append((metadata["tier"], backup.file_sha256(path)))
            if metadata["tier"] == "weekly":
                raise backup.BackupError("storage_unavailable")
            return original(path, key=key, metadata=metadata, retain_until=retain_until, **kwargs)

        with patch.object(self.transport, "upload_and_verify", side_effect=upload):
            result = await self.manager.run(tiers=("daily", "weekly", "monthly"), now=NOW)
        self.assertEqual(result["outcome_category"], "partial_success")
        self.assertEqual(result["successful_tiers"], ["daily", "monthly"])
        self.assertEqual(result["failed_tiers"], ["weekly"])
        self.assertEqual(len(set(digest for _, digest in seen)), 1)
        self.assertEqual(len(self.client.objects), 2)
        self.assertEqual([tier for tier, _ in seen], ["daily", "weekly", "monthly"])
        self.assertEqual(list(self.staging.iterdir()), [])

    async def test_fail_then_success_keeps_future_runs_and_resets_failure_count(self):
        with patch.object(self.transport, "upload_and_verify", side_effect=backup.BackupError("timeout")):
            failed = await self.manager.run(now=NOW)
        self.assertEqual(failed["outcome_category"], "timeout")
        self.assertEqual(failed["consecutive_failure_count"], 1)
        succeeded = await self.manager.run(now=NOW)
        self.assertEqual(succeeded["outcome_category"], "success")
        self.assertEqual(succeeded["consecutive_failure_count"], 0)
        self.assertFalse(self.manager.active)

    async def test_room_storage_lock_released_before_remote_transfer_from_another_thread(self):
        original = self.transport.upload_and_verify
        observed = []

        def upload(*args, **kwargs):
            def acquire():
                acquired = room._STORAGE_LOCK.acquire(timeout=1)
                if acquired:
                    room._STORAGE_LOCK.release()
                return acquired
            with ThreadPoolExecutor(max_workers=1) as pool:
                observed.append(pool.submit(acquire).result(timeout=2))
            return original(*args, **kwargs)

        with patch.object(self.transport, "upload_and_verify", side_effect=upload):
            result = await self.manager.run(now=NOW)
        self.assertEqual(result["outcome_category"], "success")
        self.assertEqual(observed, [True])

    async def test_no_overlapping_manual_or_scheduled_manager_runs(self):
        entered, release = threading.Event(), threading.Event()
        original = self.transport.upload_and_verify

        def upload(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise AssertionError("test release missing")
            return original(*args, **kwargs)

        with patch.object(self.transport, "upload_and_verify", side_effect=upload):
            first = asyncio.create_task(self.manager.run(tiers=("manual",), now=NOW))
            self.assertTrue(await asyncio.to_thread(entered.wait, 10))
            try:
                with self.assertRaises(backup.BackupError) as error:
                    await self.manager.run(tiers=("daily",), now=NOW)
                self.assertEqual(error.exception.category, "busy")
            finally:
                release.set()
            self.assertEqual((await first)["outcome_category"], "success")

    async def test_cancellation_waits_for_worker_cleanup_and_keeps_overlap_guard(self):
        entered, release = threading.Event(), threading.Event()
        original = self.transport.upload_and_verify

        def upload(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise AssertionError("test release missing")
            return original(*args, **kwargs)

        with patch.object(self.transport, "upload_and_verify", side_effect=upload):
            task = asyncio.create_task(self.manager.run(now=NOW))
            self.assertTrue(await asyncio.to_thread(entered.wait, 10))
            task.cancel()
            await asyncio.sleep(0)
            self.assertTrue(self.manager.active)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.manager.active)
        self.assertEqual(list(self.staging.iterdir()), [])
        self.assertEqual(self.manager.status()["outcome_category"], "cancelled")

    async def test_invalid_time_does_not_latch_global_busy_lock(self):
        try:
            with self.assertRaises(backup.BackupError) as error:
                await self.manager.run(now=datetime(2026, 10, 19))
            self.assertEqual(error.exception.category, "invalid_configuration")
            self.assertFalse(self.manager.active)
        finally:
            # Keep a regression failure from poisoning unrelated suite cases.
            if backup._BACKUP_LOCK.locked():
                backup._BACKUP_LOCK.release()

    async def test_invalid_or_disabled_config_never_captures_or_constructs_transport(self):
        for config in (replace(self.config, enabled=False), replace(self.config, valid=False)):
            manager = backup.BackupManager(config, self.store)
            with patch.object(backup, "capture_bundle") as capture, patch.object(backup, "B2Transport") as transport:
                with self.assertRaises(backup.BackupError) as error:
                    await manager.run(now=NOW)
                self.assertEqual(error.exception.category, "invalid_configuration")
                capture.assert_not_called()
                transport.assert_not_called()
        self.assertEqual(self.client.calls, [])

    async def test_one_deadline_is_shared_by_capture_encryption_and_every_tier(self):
        deadlines = []
        capture = backup.capture_bundle
        encrypt = backup.encrypt_archive
        upload = self.transport.upload_and_verify

        def captured(*args, **kwargs):
            deadlines.append(kwargs["deadline"])
            return capture(*args, **kwargs)

        def encrypted(*args, **kwargs):
            deadlines.append(kwargs["deadline"])
            return encrypt(*args, **kwargs)

        def uploaded(*args, **kwargs):
            deadlines.append(kwargs["deadline"])
            return upload(*args, **kwargs)

        before = time.monotonic()
        with patch.object(backup, "capture_bundle", side_effect=captured), patch.object(
            backup, "encrypt_archive", side_effect=encrypted), patch.object(
            self.transport, "upload_and_verify", side_effect=uploaded):
            result = await self.manager.run(tiers=("daily", "weekly", "monthly"), now=NOW)
        self.assertEqual(result["outcome_category"], "success")
        self.assertEqual(len(deadlines), 5)
        self.assertEqual(len(set(deadlines)), 1)
        self.assertGreater(deadlines[0], before)
        self.assertLessEqual(deadlines[0], time.monotonic() + self.config.timeout_seconds)

    async def test_backup_creates_no_room_entry_revision_or_invocation_receipt(self):
        with (
            patch.object(self.store, "begin_invocation", side_effect=AssertionError("No backup receipt")) as begin,
            patch.object(self.store, "write_revision", side_effect=AssertionError("No backup entry")) as write,
            patch.object(self.store, "finish_without_entry", side_effect=AssertionError("No backup receipt")) as finish,
        ):
            result = await self.manager.run(now=NOW)
        self.assertEqual(result["outcome_category"], "success")
        begin.assert_not_called()
        write.assert_not_called()
        finish.assert_not_called()

    async def test_encryption_failure_cleans_plaintext_and_cannot_upload_or_leak_content(self):
        before = state(self.source)
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output), patch.object(
            pyrage, "encrypt_io", side_effect=RuntimeError(PRIVATE)):
            result = await self.manager.run(now=NOW)
        self.assertEqual(result["outcome_category"], "encryption_failed")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(list(self.staging.iterdir()), [])
        self.assertEqual(state(self.source), before)
        self.assertNotIn(PRIVATE, output.getvalue() + json.dumps(result))

    async def test_unverified_remote_flags_cannot_be_success(self):
        for reply in ({"retention_verified": True, "download_verified": False},
                      {"retention_verified": False, "download_verified": True}, {}):
            with self.subTest(reply=reply), patch.object(self.transport, "upload_and_verify", return_value=reply):
                result = await self.manager.run(now=NOW)
                self.assertEqual(result["successful_tiers"], [])
                self.assertEqual(result["failed_tiers"], ["manual"])
                self.assertEqual(result["outcome_category"], "remote_unverified")
                self.assertEqual(result["tier_results"]["manual"], {
                    "outcome": "remote_unverified", "reason": "metadata_mismatch"})
        self.assertEqual(list(self.staging.iterdir()), [])

    async def test_status_atomic_write_failure_does_not_claim_durable_success(self):
        self.manager._record({"outcome_category": "not_run"})
        with patch.object(backup.os, "replace", side_effect=OSError(PRIVATE)):
            result = await self.manager.run(now=NOW)
        self.assertEqual(result["outcome_category"], "status_unavailable")
        self.assertEqual(result["successful_tiers"], ["manual"])
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertEqual(list(self.source.parent.glob(".backup-status-*.pending")), [])
        self.assertEqual(list(self.staging.iterdir()), [])

    async def test_existing_unowned_status_is_rejected_and_preserved_without_adoption(self):
        self.config.status_path.write_text(json.dumps({"entry": PRIVATE, "private_key": PRIVATE,
            "outcome_category": "timeout", "failed_tiers": ["manual", PRIVATE],
            "consecutive_failure_count": 4, "ciphertext_size": True}))
        before = self.config.status_path.read_bytes()
        self.assertEqual(self.manager.status()["outcome_category"], "status_unavailable")
        result = await self.manager.run(now=NOW)
        self.assertEqual(result["outcome_category"], "status_unavailable")
        self.assertEqual(self.config.status_path.read_bytes(), before)
        self.assertNotIn(PRIVATE, json.dumps(result))

    async def test_capture_cleanup_failure_prevents_encryption_and_all_uploads(self):
        original = Path.unlink
        failures = 0
        def unlink(path, *args, **kwargs):
            nonlocal failures
            if path.name == ".capture-source.sqlite3" and failures == 0:
                failures += 1
                raise PermissionError(PRIVATE + str(path))
            return original(path, *args, **kwargs)
        with patch.object(room, "_STORAGE_LOCK", threading.RLock()):
            with patch.object(Path, "unlink", unlink), patch.object(backup, "encrypt_archive") as encrypt:
                result = await self.manager.run(now=NOW)
            encrypt.assert_not_called()
            self.assertEqual(result["outcome_category"], "cleanup_failure")
            self.assertTrue(result["cleanup_failed"])
            self.assertFalse(result["successful_tiers"])
            self.assertEqual(self.client.calls, [])
            self.assertFalse(result.get("last_successful_time"))
            def writer():
                acquired = room._STORAGE_LOCK.acquire(timeout=0.5)
                if not acquired:
                    return False
                try:
                    _, created = self.store.begin_invocation(operation_key="synthetic-manager-cleanup-writer",
                        trigger_type="becoming_want", scope=SCOPE, requested_model="synthetic/model")
                    return created
                finally:
                    room._STORAGE_LOCK.release()
            self.assertTrue(await asyncio.to_thread(writer))
        self.assertEqual(list(self.staging.iterdir()), [])
        self.assertNotIn(PRIVATE, json.dumps(result) + self.config.status_path.read_text())

    async def test_persistent_cleanup_failure_reports_uncertain_plaintext_and_removes_other_artifacts(self):
        original = Path.unlink
        def unlink(path, *args, **kwargs):
            if path.name == ".capture-source.sqlite3":
                raise PermissionError(PRIVATE + str(path))
            return original(path, *args, **kwargs)
        before = state(self.source)
        with patch.object(room, "_STORAGE_LOCK", threading.RLock()):
            with patch.object(Path, "unlink", unlink), patch.object(backup, "encrypt_archive") as encrypt:
                result = await self.manager.run(now=NOW)
            encrypt.assert_not_called()
            self.assertEqual(result["outcome_category"], "cleanup_failure")
            self.assertTrue(result["cleanup_failed"])
            self.assertFalse(result["successful_tiers"])
            self.assertFalse(result.get("last_successful_time"))
            self.assertEqual(self.client.calls, [])
            self.assertEqual(state(self.source), before)
            parents = list(self.staging.iterdir())
            self.assertEqual(len(parents), 1)
            parent = parents[0]
            self.assertEqual(parent.stat().st_mode & 0o777, 0o700)
            members = list(parent.iterdir())
            self.assertEqual([member.name for member in members], [".capture-source.sqlite3"])
            self.assertEqual(members[0].stat().st_mode & 0o777, 0o600)
            def writer():
                acquired = room._STORAGE_LOCK.acquire(timeout=0.5)
                if not acquired:
                    return False
                try:
                    _, created = self.store.begin_invocation(operation_key="synthetic-persistent-cleanup-writer",
                        trigger_type="becoming_want", scope=SCOPE, requested_model="synthetic/model")
                    return created
                finally:
                    room._STORAGE_LOCK.release()
            self.assertTrue(await asyncio.to_thread(writer))
        encoded = json.dumps(result) + self.config.status_path.read_text()
        self.assertNotIn(PRIVATE, encoded)
        self.assertNotIn(str(parent), encoded)
        # The injected denial has ended. Explicitly remove the intentionally
        # stranded synthetic fixture, as permitted by the repair packet.
        shutil.rmtree(parent)
        self.assertEqual(list(self.staging.iterdir()), [])

    async def test_every_remote_unverified_reason_survives_status_without_advancing_last_success(self):
        succeeded = await self.manager.run(now=NOW)
        self.assertEqual(succeeded["outcome_category"], "success")
        previous = succeeded["last_successful_time"]
        for reason in ("metadata_mismatch", "retention_mismatch", "retention_too_short", "download_timeout",
                       "download_failure", "length_mismatch", "hash_mismatch", "verification_cleanup_failure"):
            with self.subTest(reason=reason), patch.object(self.transport, "upload_and_verify", side_effect=
                backup.BackupError("remote_unverified", verification_reason=reason, remote_uploaded=True)):
                result = await self.manager.run(now=NOW + timedelta(days=1))
            self.assertEqual(result["outcome_category"], "remote_unverified")
            self.assertEqual(result["tier_results"], {"manual": {"outcome": "remote_unverified", "reason": reason}})
            self.assertEqual(result["successful_tiers"], [])
            self.assertEqual(result["failed_tiers"], ["manual"])
            self.assertEqual(result["last_successful_time"], previous)
            self.assertGreater(result["consecutive_failure_count"], 0)
            persisted = json.loads(self.config.status_path.read_bytes())
            self.assertEqual(persisted["tier_results"], result["tier_results"])
            self.assertEqual(persisted["last_successful_time"], previous)
            self.assertNotIn(PRIVATE, json.dumps(result))

    async def test_multi_tier_remote_uncertainty_preserves_verified_siblings_and_pre_put_categories(self):
        original = self.transport.upload_and_verify
        def upload(path, *, metadata, **kwargs):
            if metadata["tier"] == "weekly":
                raise backup.BackupError("remote_unverified", verification_reason="hash_mismatch", remote_uploaded=True)
            if metadata["tier"] == "monthly":
                raise backup.BackupError("storage_unavailable")
            return original(path, metadata=metadata, **kwargs)
        with patch.object(self.transport, "upload_and_verify", side_effect=upload):
            result = await self.manager.run(tiers=("daily", "weekly", "monthly"), now=NOW)
        self.assertEqual(result["outcome_category"], "partial_success")
        self.assertEqual(result["successful_tiers"], ["daily"])
        self.assertEqual(result["failed_tiers"], ["weekly", "monthly"])
        self.assertEqual(result["tier_results"]["daily"], {"outcome": "verified"})
        self.assertEqual(result["tier_results"]["weekly"], {"outcome": "remote_unverified", "reason": "hash_mismatch"})
        self.assertEqual(result["tier_results"]["monthly"]["outcome"], "not_uploaded")
        self.assertFalse(result.get("last_successful_time"))
        self.assertEqual(json.loads(self.config.status_path.read_bytes())["tier_results"], result["tier_results"])
        with patch.object(self.transport, "upload_and_verify", side_effect=backup.BackupError("storage_unavailable")):
            failed = await self.manager.run(now=NOW)
        self.assertEqual(failed["outcome_category"], "storage_unavailable")
        self.assertEqual(failed["tier_results"]["manual"]["outcome"], "not_uploaded")


if __name__ == "__main__":
    unittest.main()
