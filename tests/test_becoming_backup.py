"""Synthetic-only capture, encryption and operating backup acceptance."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import fields, replace
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
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
                self.assertEqual(result["outcome_category"], "metadata_mismatch")
        self.assertEqual(list(self.staging.iterdir()), [])

    async def test_status_atomic_write_failure_does_not_claim_durable_success(self):
        with patch.object(backup.os, "replace", side_effect=OSError(PRIVATE)):
            result = await self.manager.run(now=NOW)
        self.assertEqual(result["outcome_category"], "status_unavailable")
        self.assertEqual(result["successful_tiers"], ["manual"])
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertEqual(list(self.source.parent.glob(".backup-status-*.pending")), [])
        self.assertEqual(list(self.staging.iterdir()), [])

    async def test_existing_status_is_bounded_and_unknown_fields_never_reappear(self):
        self.config.status_path.write_text(json.dumps({"entry": PRIVATE, "private_key": PRIVATE,
            "outcome_category": "timeout", "failed_tiers": ["manual", PRIVATE],
            "consecutive_failure_count": 4, "ciphertext_size": True}))
        self.assertEqual(self.manager.status(), {"outcome_category": "timeout", "failed_tiers": ["manual"],
                                               "consecutive_failure_count": 4})
        result = await self.manager.run(now=NOW)
        self.assertEqual(result["outcome_category"], "success")
        self.assertNotIn(PRIVATE, self.config.status_path.read_text())


if __name__ == "__main__":
    unittest.main()
