"""Local-only encrypted recovery and explicit synthetic drill acceptance."""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import pyrage

from src import becoming as room
from src import becoming_backup as backup
from src import becoming_recovery as recovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="room-recovery-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.identity = pyrage.x25519.Identity.generate()
        self.source = self.directory / "source" / "room.sqlite3"
        self.store, self.entry_id = recovery._synthetic_fixture(self.source)
        config = room.RoomConfig.from_env({"BECOMING_ENABLED": "true", "BOT_OWNER_DISCORD_ID": "10",
            "BECOMING_CHANNEL_ID": "20", "BECOMING_GUILD_ID": "30", "BECOMING_DB_PATH": str(self.source)})
        self.config = backup.BackupConfig.from_env({
            "BECOMING_BACKUP_ENABLED": "true", "BECOMING_BACKUP_AGE_RECIPIENT": str(self.identity.to_public()),
            "BECOMING_BACKUP_S3_ENDPOINT_URL": "https://s3.us-west-004.backblazeb2.com",
            "BECOMING_BACKUP_S3_REGION": "us-west-004", "BECOMING_BACKUP_S3_BUCKET": "synthetic-room-backups",
            "BECOMING_BACKUP_S3_ACCESS_KEY_ID": "synthetic-access", "BECOMING_BACKUP_S3_SECRET_ACCESS_KEY": "synthetic-secret",
            "BECOMING_BACKUP_STATUS_PATH": str(self.source.parent / "backup-status.json"),
        }, config)
        capture = self.directory / "captured"
        capture.mkdir(mode=0o700)
        self.bundle = backup.capture_bundle(self.store, capture, self.config)
        self.ciphertext = self.directory / "room.tar.age"
        encrypted = backup.encrypt_archive(self.bundle.archive_path, self.ciphertext,
            str(self.identity.to_public()), max_plaintext_bytes=134217728)
        self.metadata = {"backup_format_version": "1", "schema_version": "1",
            "backup_id": self.bundle.manifest["backup_id"], "tier": "manual", **encrypted}
        self.destination = self.directory / "restored"

    def restore(self, **changes):
        arguments = dict(identity_stdin=io.BytesIO(str(self.identity).encode()),
            expected_sha256=self.metadata["ciphertext_sha256"], expected_metadata=self.metadata)
        arguments.update(changes)
        return recovery.restore_ciphertext(self.ciphertext, self.destination, **arguments)

    def encrypt_archive(self, archive):
        self.ciphertext.unlink(missing_ok=True)
        encrypted = backup.encrypt_archive(archive, self.ciphertext,
            str(self.identity.to_public()), max_plaintext_bytes=134217728)
        self.metadata.update(encrypted)

    def test_valid_restore_after_source_deleted_fresh_process_history_and_immutability(self):
        shutil.rmtree(self.source.parent)
        report = self.restore()
        self.assertEqual(report["outcome"], "restored")
        self.assertEqual(report["revisions"], 2)
        self.assertEqual(report["immutable_checks"], 4)
        self.assertTrue(report["fresh_process_verified"])
        restored = self.destination / "room.sqlite3"
        self.assertEqual(restored.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)
        store = room.RoomStore(restored)
        history = store.get_history(self.entry_id, room.RoomScope("10", "20", "30"))
        self.assertEqual([r["revision_number"] for r in history], [1, 2])
        with sqlite3.connect(restored) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM invocations").fetchone()[0], 3)
        self.assertNotIn(history[0]["content"], json.dumps(report))

    def test_system_temporary_root_alias_is_canonicalized(self):
        with patch.object(recovery.tempfile, "gettempdir", return_value="/tmp"):
            self.assertEqual(recovery._temporary_root(), Path("/tmp").resolve())
            self.assertEqual(self.restore()["outcome"], "restored")

    def test_wrong_identity_bounded_and_no_partial_destination(self):
        other = pyrage.x25519.Identity.generate()
        with self.assertRaises(backup.BackupError) as error:
            self.restore(identity_stdin=io.BytesIO(str(other).encode()))
        self.assertEqual(error.exception.category, "decrypt_failed")
        self.assertFalse(self.destination.exists())
        self.assertNotIn(str(other), str(error.exception))

    def test_altered_ciphertext_rejected_before_decryption(self):
        data = bytearray(self.ciphertext.read_bytes())
        data[-1] ^= 1
        self.ciphertext.write_bytes(data)
        with patch.object(pyrage, "decrypt_io") as decrypt:
            with self.assertRaises(backup.BackupError) as error:
                self.restore()
        self.assertEqual(error.exception.category, "ciphertext_hash_mismatch")
        decrypt.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_metadata_must_be_bounded_and_content_neutral(self):
        for change in ({"private_title": "SYNTHETIC_PRIVATE_TITLE"}, {"tier": "private route"},
                       {"backup_id": "name/route"}, {"ciphertext_sha256": "bad"},
                       {"ciphertext_size": True}, {"schema_version": "9"}):
            with self.subTest(change=tuple(change)):
                metadata = {**self.metadata, **change}
                with self.assertRaises(backup.BackupError) as error:
                    self.restore(expected_metadata=metadata)
                self.assertEqual(error.exception.category, "object_metadata_invalid")

    def test_nonempty_destination_preserved(self):
        self.destination.mkdir(mode=0o700)
        sentinel = self.destination / "sentinel"
        sentinel.write_bytes(b"preserve")
        with self.assertRaises(backup.BackupError):
            self.restore()
        self.assertEqual(sentinel.read_bytes(), b"preserve")

    def test_known_production_and_repository_paths_rejected(self):
        for path in (Path("/app/data/room-of-becoming/restored"), room.REPOSITORY_ROOT / "data/room-of-becoming/restored"):
            with self.subTest(path=path.name), self.assertRaises(backup.BackupError):
                recovery.restore_ciphertext(self.ciphertext, path,
                    identity_stdin=io.BytesIO(str(self.identity).encode()),
                    expected_sha256=self.metadata["ciphertext_sha256"], expected_metadata=self.metadata)

    def test_private_identity_file_explicit_permissions_and_no_environment_fallback(self):
        identity = self.directory / "identity.txt"
        identity.write_text("# synthetic test identity\n" + str(self.identity) + "\n")
        identity.chmod(0o600)
        report = self.restore(identity_file=identity, identity_stdin=None)
        self.assertEqual(report["outcome"], "restored")
        shutil.rmtree(self.destination)
        identity.chmod(0o644)
        with self.assertRaises(backup.BackupError) as error:
            self.restore(identity_file=identity, identity_stdin=None)
        self.assertEqual(error.exception.category, "identity_invalid")
        with patch.dict(os.environ, {"AGE_IDENTITY": str(self.identity)}):
            with self.assertRaises(backup.BackupError):
                self.restore(identity_file=None, identity_stdin=None)

    def test_public_recipient_or_multiple_identities_rejected(self):
        for data in (str(self.identity.to_public()), str(self.identity) + "\n" + str(self.identity), "malformed"):
            with self.subTest(kind=len(data)), self.assertRaises(backup.BackupError) as error:
                self.restore(identity_stdin=io.BytesIO(data.encode()))
            self.assertEqual(error.exception.category, "identity_invalid")

    def test_identity_rejected_inside_every_git_checkout_or_worktree(self):
        for kind in ("directory", "file"):
            checkout = self.directory / ("other-checkout-" + kind)
            checkout.mkdir(mode=0o700)
            marker = checkout / ".git"
            marker.mkdir() if kind == "directory" else marker.write_text("gitdir: synthetic-only")
            identity = checkout / "identity.txt"
            identity.write_text(str(self.identity))
            identity.chmod(0o600)
            with self.subTest(kind=kind), self.assertRaises(backup.BackupError) as error:
                self.restore(identity_file=identity, identity_stdin=None)
            self.assertEqual(error.exception.category, "identity_invalid")
            self.assertFalse(self.destination.exists())

    def test_archive_traversal_links_duplicates_and_unexpected_members_rejected(self):
        for kind in ("traversal", "link", "duplicate", "unexpected"):
            archive = self.directory / (kind + ".tar")
            with tarfile.open(archive, "w") as target:
                member = tarfile.TarInfo("../outside" if kind == "traversal" else "unexpected")
                if kind == "link":
                    member.type = tarfile.SYMTYPE
                    member.linkname = "/outside"
                    target.addfile(member)
                else:
                    member.size = 1
                    target.addfile(member, io.BytesIO(b"x"))
                    if kind == "duplicate":
                        target.addfile(member, io.BytesIO(b"x"))
            self.encrypt_archive(archive)
            with self.subTest(kind=kind), self.assertRaises(backup.BackupError):
                self.restore()
            self.assertFalse(self.destination.exists())
            self.assertFalse((self.directory / "outside").exists())

    def _alter_bundle(self, *, database=None, manifest=None, refresh_database_hash=False):
        members = {}
        with tarfile.open(self.bundle.archive_path, "r") as archive:
            for member in archive.getmembers():
                members[member.name] = archive.extractfile(member).read()
        if database:
            db = self.directory / "altered.sqlite3"
            db.write_bytes(members["room.sqlite3"])
            database(db)
            members["room.sqlite3"] = db.read_bytes()
        if manifest or refresh_database_hash:
            payload = json.loads(members["manifest.json"])
            if manifest:
                manifest(payload)
            if refresh_database_hash:
                payload["database_sha256"] = hashlib.sha256(members["room.sqlite3"]).hexdigest()
                payload["database_size"] = len(members["room.sqlite3"])
            members["manifest.json"] = json.dumps(payload).encode()
        archive_path = self.directory / "altered.tar"
        with tarfile.open(archive_path, "w") as archive:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), 0o600
                archive.addfile(info, io.BytesIO(data))
        self.encrypt_archive(archive_path)

    def test_manifest_mismatch_rejected(self):
        self._alter_bundle(manifest=lambda m: m.update(backup_id="abcdef0123456789"))
        with self.assertRaises(backup.BackupError):
            self.restore()

    def test_sqlite_hash_mismatch_rejected(self):
        def alter(path):
            data = bytearray(path.read_bytes())
            data[28] ^= 1
            path.write_bytes(data)
        self._alter_bundle(database=alter)
        with self.assertRaises(backup.BackupError):
            self.restore()

    def test_schema_and_integrity_failure_rejected_after_archive_boundary(self):
        for category in ("schema_mismatch", "integrity_failure"):
            with self.subTest(category=category), patch.object(backup, "extract_validated_archive",
                    side_effect=backup.BackupError(category)):
                with self.assertRaises(backup.BackupError) as error:
                    self.restore()
                self.assertEqual(error.exception.category, category)
                self.assertFalse(self.destination.exists())

    def test_repacked_encrypted_invalid_schema_and_foreign_key_rejected(self):
        for category in ("schema_mismatch", "integrity_failure"):
            def alter(path):
                with sqlite3.connect(path) as connection:
                    if category == "schema_mismatch":
                        connection.execute("PRAGMA user_version=99")
                    else:
                        connection.execute("PRAGMA foreign_keys=OFF")
                        connection.execute("UPDATE entries SET current_revision=99")
            self._alter_bundle(database=alter, refresh_database_hash=True)
            with self.subTest(category=category), self.assertRaises(backup.BackupError) as error:
                self.restore()
            self.assertEqual(error.exception.category, category)
            self.assertFalse(self.destination.exists())

    def test_decrypted_plaintext_limit_and_temp_cleanup(self):
        names = set(self.directory.iterdir())
        # Ciphertext framing permits this size, but decrypted archive exceeds it.
        with patch.object(recovery, "_temporary_root", return_value=self.directory.resolve()), self.assertRaises(backup.BackupError):
            self.restore(max_plaintext_bytes=10240)
        self.assertEqual(set(self.directory.iterdir()), names)
        self.assertFalse(self.destination.exists())

    def test_object_recovery_uses_specific_key_and_same_validator(self):
        class Transport:
            def __init__(self, test):
                self.test, self.keys = test, []
            def download_for_restore(self, key, destination):
                self.keys.append(key)
                shutil.copyfile(self.test.ciphertext, destination)
                return dict(self.test.metadata)
        transport = Transport(self)
        report = recovery.restore_object("room-of-becoming/manual/synthetic.tar.age", self.destination,
            transport=transport, identity_stdin=io.BytesIO(str(self.identity).encode()))
        self.assertEqual(report["outcome"], "restored")
        self.assertEqual(transport.keys, ["room-of-becoming/manual/synthetic.tar.age"])

    def test_cli_emits_bounded_failure_without_identity_or_payload(self):
        identity = self.directory / "identity.txt"
        identity.write_text("SYNTHETIC_PRIVATE_IDENTITY_BODY")
        identity.chmod(0o600)
        metadata = self.directory / "metadata.json"
        metadata.write_text(json.dumps(self.metadata))
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = recovery.main(["restore", "--ciphertext", str(self.ciphertext), "--destination", str(self.destination),
                "--identity-file", str(identity), "--ciphertext-sha256", self.metadata["ciphertext_sha256"],
                "--metadata-file", str(metadata)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue()), {"category": "identity_invalid", "outcome": "failed"})


class SyntheticDrillTests(unittest.TestCase):
    def test_both_explicit_flags_required(self):
        for values in ({}, {"synthetic": True}, {"test_only": True}):
            with self.subTest(values=values), self.assertRaises(backup.BackupError) as error:
                recovery.run_synthetic_drill(**values)
            self.assertEqual(error.exception.category, "synthetic_only_required")

    def test_drill_full_interfaces_fresh_processes_and_cleanup(self):
        with tempfile.TemporaryDirectory(prefix="room-drill-test-") as root:
            client = recovery.SyntheticS3Client()
            report = recovery.run_synthetic_drill(synthetic=True, test_only=True, s3_client=client, work_root=root)
            self.assertEqual(report["outcome"], "synthetic_drill_passed")
            self.assertTrue(report["source_removed"])
            self.assertEqual(report["restored_revisions"], 2)
            self.assertEqual(report["post_restore_revision"], 3)
            self.assertTrue(report["second_process_verified"])
            self.assertEqual(list(Path(root).iterdir()), [])
            methods = {name for name, arguments in client.calls}
            self.assertTrue({"put_object", "get_object", "get_object_retention"} <= methods)
            self.assertFalse(any("delete" in name.lower() or "governance" in name.lower() for name in methods))

    def test_drill_refuses_known_production_root(self):
        with self.assertRaises(backup.BackupError):
            recovery.run_synthetic_drill(synthetic=True, test_only=True, work_root="/app/data")

    def test_remote_synthetic_requires_separate_explicit_credentials_and_identity(self):
        with self.assertRaises(backup.BackupError):
            recovery.run_synthetic_drill(synthetic=True, allow_remote_synthetic=True)
        with tempfile.TemporaryDirectory(prefix="room-remote-drill-test-") as root:
            root = Path(root)
            identity = pyrage.x25519.Identity.generate()
            identity_file = root / "identity.txt"
            identity_file.write_text(str(identity))
            identity_file.chmod(0o600)
            credentials = root / "operations.json"
            credentials.write_text(json.dumps({
                "endpoint_url": "https://s3.us-west-004.backblazeb2.com", "region": "us-west-004",
                "bucket": "synthetic-room-backups", "prefix": "room-of-becoming",
                "access_key_id": "synthetic-operations", "secret_access_key": "synthetic-secret",
            }))
            credentials.chmod(0o600)
            client = recovery.SyntheticS3Client()
            report = recovery.run_synthetic_drill(synthetic=True, allow_remote_synthetic=True,
                storage_credentials_file=credentials, identity_file=identity_file, s3_client=client, work_root=root)
            self.assertEqual(report["outcome"], "synthetic_drill_passed")
            self.assertTrue(report["remote_synthetic_object_retained"])
            self.assertFalse(report["live_storage_verified"])
            self.assertEqual({p.name for p in root.iterdir()}, {"identity.txt", "operations.json"})
            with self.assertRaises(backup.BackupError):
                recovery.run_synthetic_drill(synthetic=True, test_only=True,
                    storage_credentials_file=credentials, identity_file=identity_file)


if __name__ == "__main__":
    unittest.main()
