"""H2: owned status documents preserve unrelated files and filesystem identity."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src import becoming
from src import becoming_backup as backup
from src import becoming_backup_status as documents


class _FileHook:
    def __init__(self, stream, *, after_close=None, write_error=None, close_error=None):
        self.stream = stream
        self.after_close = after_close
        self.write_error, self.close_error = write_error, close_error
        self.close_calls = 0

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def write(self, value):
        if self.write_error is not None:
            raise self.write_error
        return self.stream.write(value)

    def close(self):
        self.close_calls += 1
        self.stream.close()
        if self.after_close is not None:
            self.after_close()
        if self.close_error is not None:
            raise self.close_error


class OwnedStatusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="room-status-repair-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.database = self.directory / "room.sqlite3"
        self.path = self.directory / "backup-status.json"
        self.store = becoming.RoomStore(self.database)
        self.config = backup.BackupConfig(enabled=True, valid=True, db_path=self.database,
                                          status_path=self.path)
        self.manager = backup.BackupManager(self.config, self.store)

    def valid(self, **changes):
        return {"application": documents.APPLICATION, "format_version": 1,
                "outcome_category": "success", "last_attempt_time": "2026-10-20T03:00:00Z",
                "last_successful_time": "2026-10-20T03:00:00Z", "backup_id": "a" * 32,
                "successful_tiers": ["manual"], "failed_tiers": [], "ciphertext_size": 200,
                "consecutive_failure_count": 0, "schema_version": 1,
                "retention_verified": True, "download_verified": True, **changes}

    def write(self, value, path=None):
        path = path or self.path
        payload = value if isinstance(value, bytes) else json.dumps(value).encode()
        path.write_bytes(payload)
        path.chmod(0o600)

    def snapshot(self, path=None):
        path = path or self.path
        data = path.read_bytes()
        info = path.lstat()
        return (data, hashlib.sha256(data).hexdigest(), info.st_dev, info.st_ino,
                info.st_size, info.st_mode, info.st_uid, info.st_gid,
                info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)

    def assert_rejected_preserved(self, path=None):
        path = path or self.path
        manager = backup.BackupManager(replace(self.config, status_path=path), self.store)
        before = self.snapshot(path)
        self.assertFalse(backup._safe_status_path(path, self.database))
        self.assertEqual(manager.status()["outcome_category"], "status_unavailable")
        outcome = manager._record(self.valid())
        self.assertEqual(outcome["outcome_category"], "status_unavailable")
        self.assertEqual(before, self.snapshot(path))
        self.assertEqual(list(self.directory.glob(".backup-status-*.pending")), [])

    def test_unrelated_sibling_json_is_preserved_bytes_hash_inode_mode_and_times(self):
        self.write({"another_application": "SYNTHETIC_PRIVATE_MARKER"})
        self.assert_rejected_preserved()

    def test_default_discord_status_is_reserved_even_if_owned_shape(self):
        path = self.directory / "discord_status.json"
        self.write(self.valid(), path)
        self.assert_rejected_preserved(path)

    def test_configured_public_status_is_reserved_with_metadata_unchanged(self):
        path = self.directory / "public-presence.json"
        self.write(self.valid(), path)
        with patch.dict(os.environ, {"DISCORD_STATUS_PATH": str(path)}):
            self.assert_rejected_preserved(path)
        self.assertFalse(backup._safe_status_path(path, self.database,
                         env={"DISCORD_STATUS_PATH": str(path)}))

    def test_other_configured_application_persistence_is_protected(self):
        path = self.directory / "paintbox-persistence.json"
        self.write(self.valid(), path)
        with patch.dict(os.environ, {"PAINTBOX_USAGE_PATH": str(path)}):
            self.assert_rejected_preserved(path)
        self.assertFalse(backup._safe_status_path(path, self.database,
                         env={"PAINTBOX_USAGE_PATH": str(path)}))

    def test_roomstore_explicit_protected_path_is_reserved(self):
        self.write(self.valid())
        protected_store = becoming.RoomStore(self.database, protected_paths=(self.path,))
        manager = backup.BackupManager(self.config, protected_store)
        before = self.snapshot()
        self.assertEqual(manager._record(self.valid())["outcome_category"], "status_unavailable")
        self.assertEqual(before, self.snapshot())

    def test_valid_owned_version_one_is_read_and_detached(self):
        self.write(self.valid())
        self.assertTrue(backup._safe_status_path(self.path, self.database))
        status = self.manager.status()
        self.assertEqual(status, self.valid())
        status["successful_tiers"].clear()
        self.assertEqual(self.manager.status()["successful_tiers"], ["manual"])

    def test_malformed_owned_status_is_preserved(self):
        malformed = [
            b'{"application":"room-of-becoming-backup-status",',
            self.valid(ciphertext_size=True), self.valid(cleanup_failed="yes"),
            self.valid(last_attempt_time="2026-02-31T03:00:00Z"),
            self.valid(successful_tiers=["manual", "manual"]),
            self.valid(unknown="SYNTHETIC_PRIVATE_MARKER"),
            self.valid(tier_results={"manual": {"outcome": "remote_unverified", "reason": "raw error/path"}}),
            self.valid(tier_results={"manual": {"outcome": "verified", "extra": 1}}),
            self.valid(format_version=True),
        ]
        for value in malformed:
            with self.subTest(value=value):
                self.write(value)
                self.assert_rejected_preserved()

    def test_duplicate_ownership_fields_are_ambiguous_and_preserved(self):
        self.write(b'{"application":"room-of-becoming-backup-status","application":"room-of-becoming-backup-status","format_version":1,"outcome_category":"success"}')
        self.assert_rejected_preserved()

    def test_unowned_similar_status_fields_are_not_adopted(self):
        value = self.valid()
        del value["application"]
        self.write(value)
        self.assert_rejected_preserved()

    def test_unsupported_future_owned_version_is_preserved(self):
        self.write(self.valid(format_version=2))
        self.assert_rejected_preserved()

    def test_symlink_and_hardlink_aliases_preserve_target_bytes_and_metadata(self):
        original = self.directory / "other-application.json"
        self.write(self.valid(), original)
        before = self.snapshot(original)
        self.path.symlink_to(original)
        self.assert_rejected_preserved()
        self.assertEqual(before, self.snapshot(original))
        self.path.unlink()
        os.link(original, self.path)
        before = self.snapshot(original)
        self.assert_rejected_preserved()
        self.assertEqual(before, self.snapshot(original))

    def test_configured_application_physical_alias_is_protected(self):
        original = self.directory / "public-presence.json"
        self.write(self.valid())
        original.symlink_to(self.path)
        before = self.snapshot()
        with patch.dict(os.environ, {"DISCORD_STATUS_PATH": str(original)}):
            self.assert_rejected_preserved()
        self.assertEqual(before, self.snapshot())

    def test_symlinked_and_unsafe_parent_are_rejected(self):
        linked = self.directory / "linked-parent"
        real = self.directory / "real-parent"
        real.mkdir(mode=0o700)
        linked.symlink_to(real, target_is_directory=True)
        self.assertFalse(backup._safe_status_path(linked / "backup-status.json", linked / "room.sqlite3"))
        real.chmod(0o777)
        self.assertFalse(backup._safe_status_path(real / "backup-status.json", real / "room.sqlite3"))

    def test_target_swapped_between_validation_and_replace_is_not_overwritten(self):
        self.write(self.valid())
        replacement = self.directory / "replacement.json"
        self.write(self.valid(backup_id="b" * 32), replacement)
        original_file = backup._private_file
        swapped = []

        def swap_after_pending_write(path):
            def swap():
                os.replace(replacement, self.path)
                swapped.append(self.snapshot())
            return _FileHook(original_file(path), after_close=swap)

        with patch.object(backup, "_private_file", swap_after_pending_write), patch.object(backup.os, "replace", wraps=os.replace) as rename:
            result = self.manager._record(self.valid(backup_id="c" * 32))
        self.assertEqual(result["outcome_category"], "status_unavailable")
        self.assertEqual(self.snapshot(), swapped[0])
        self.assertEqual(rename.call_count, 1)  # The injected swap, never publication.
        self.assertEqual(list(self.directory.glob(".backup-status-*.pending")), [])

    def test_target_changed_in_place_between_validation_and_replace_is_preserved(self):
        self.write(self.valid())
        original_file = backup._private_file
        changed = []

        def change_after_pending_write(path):
            def change():
                self.write(self.valid(backup_id="b" * 32))
                changed.append(self.snapshot())
            return _FileHook(original_file(path), after_close=change)

        with patch.object(backup, "_private_file", change_after_pending_write):
            result = self.manager._record(self.valid(backup_id="c" * 32))
        self.assertEqual(result["outcome_category"], "status_unavailable")
        self.assertEqual(self.snapshot(), changed[0])

    def test_existing_valid_status_is_atomically_replaced_and_private(self):
        self.write(self.valid())
        before_inode = self.path.stat().st_ino
        replacement = self.valid(backup_id="b" * 32)
        original_replace = os.replace
        observed = []

        def inspect_atomic(source, target):
            self.assertEqual(self.path.stat().st_ino, before_inode)
            self.assertEqual(Path(source).stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(Path(source).read_bytes()), replacement)
            observed.append((source, target))
            return original_replace(source, target)

        with patch.object(backup.os, "replace", side_effect=inspect_atomic):
            self.assertEqual(self.manager._record(replacement), replacement)
        self.assertEqual(len(observed), 1)
        self.assertNotEqual(self.path.stat().st_ino, before_inode)
        self.assertEqual(json.loads(self.path.read_bytes()), replacement)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_absent_status_is_created_atomically_mode_0600(self):
        self.assertFalse(self.path.exists())
        result = self.manager._record(self.valid())
        self.assertEqual(result, self.valid())
        self.assertEqual(json.loads(self.path.read_bytes()), self.valid())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.directory.glob(".backup-status-*.pending")), [])

    def test_absent_target_created_by_another_writer_is_preserved(self):
        original_file = backup._private_file
        changed = []

        def create_after_pending_write(path):
            def create():
                self.write({"another_application": "SYNTHETIC_PRIVATE_MARKER"})
                changed.append(self.snapshot())
            return _FileHook(original_file(path), after_close=create)

        with patch.object(backup, "_private_file", create_after_pending_write):
            result = self.manager._record(self.valid())
        self.assertEqual(result["outcome_category"], "status_unavailable")
        self.assertEqual(self.snapshot(), changed[0])

    def test_read_detects_external_corruption_even_after_valid_cache(self):
        self.manager._record(self.valid())
        self.write(b"invalid-json")
        before = self.snapshot()
        self.assertEqual(self.manager.status()["outcome_category"], "status_unavailable")
        self.assertEqual(self.snapshot(), before)

    def test_status_schema_preserves_bounded_remote_uncertainty_and_cleanup_flag(self):
        value = self.valid(outcome_category="remote_unverified", successful_tiers=[], failed_tiers=["manual"],
                           retention_verified=False, download_verified=False, consecutive_failure_count=1,
                           cleanup_failed=True, tier_results={"manual": {"outcome": "remote_unverified",
                               "reason": "retention_too_short", "cleanup_failed": True}})
        self.assertEqual(self.manager._record(value), value)
        self.assertEqual(self.manager.status(), value)

    def test_ambiguous_owned_success_and_tier_outcomes_are_preserved(self):
        contradictory = [
            self.valid(cleanup_failed=True),
            self.valid(retention_verified=False),
            self.valid(download_verified=False),
            self.valid(consecutive_failure_count=2),
            self.valid(failed_tiers=["daily"]),
            self.valid(tier_results={"manual": {"outcome": "verified", "reason": "hash_mismatch"}}),
            self.valid(tier_results={"manual": {"outcome": "verified", "cleanup_failed": True}}),
            self.valid(tier_results={"manual": {"outcome": "remote_unverified", "reason": "hash_mismatch"}}),
            self.valid(outcome_category="partial_success", failed_tiers=[]),
            self.valid(outcome_category="remote_unverified"),
            self.valid(tier_results={}),
            self.valid(outcome_category="remote_unverified", successful_tiers=[], failed_tiers=["manual"],
                       retention_verified=False, download_verified=False,
                       tier_results={"manual": {"outcome": "remote_unverified", "reason": "success"}}),
        ]
        for value in contradictory:
            with self.subTest(value=value):
                self.write(value)
                self.assert_rejected_preserved()

    def test_verified_tiers_with_later_local_failure_remain_unambiguous(self):
        for category in ("cleanup_failure", "status_unavailable"):
            with self.subTest(category=category):
                value = self.valid(outcome_category=category, cleanup_failed=True,
                                   tier_results={"manual": {"outcome": "verified"}})
                self.write(value)
                self.assertEqual(self.manager.status(), value)

    def test_pending_cleanup_failure_is_bounded_and_primary_failure_preserved(self):
        original_unlink = Path.unlink
        created = []

        def deny_pending(path, *args, **kwargs):
            if path.name.startswith(".backup-status-"):
                created.append(path)
                raise PermissionError("SYNTHETIC_PRIVATE_MARKER/secret-temporary-name")
            return original_unlink(path, *args, **kwargs)

        with patch.object(backup.os, "replace", side_effect=OSError("SYNTHETIC_PRIVATE_MARKER")), patch.object(Path, "unlink", deny_pending):
            result = self.manager._record({"outcome_category": "timeout"})
        self.assertEqual(result["outcome_category"], "timeout")
        self.assertTrue(result["cleanup_failed"])
        self.assertNotIn("SYNTHETIC_PRIVATE_MARKER", repr(result))
        self.assertFalse(self.path.exists())
        for path in created:
            original_unlink(path, missing_ok=True)

    def test_status_close_only_failure_is_bounded_cleanup_failure_and_never_published(self):
        original_file = backup._private_file
        opened = []
        output, errors = io.StringIO(), io.StringIO()

        def broken_close(path):
            wrapped = _FileHook(original_file(path), close_error=PermissionError(
                "SYNTHETIC_PRIVATE_MARKER/temporary-status-name"))
            opened.append(wrapped)
            return wrapped

        with patch.object(backup, "_private_file", broken_close), patch.object(backup.os, "replace") as publish, self.assertNoLogs(), redirect_stdout(output), redirect_stderr(errors):
            result = self.manager._record(self.valid())
        self.assertEqual(result["outcome_category"], "cleanup_failure")
        self.assertTrue(result["cleanup_failed"])
        self.assertNotIn("last_successful_time", result)
        publish.assert_not_called()
        self.assertEqual(opened[0].close_calls, 1)
        self.assertTrue(opened[0].stream.closed)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.directory.glob(".backup-status-*.pending")), [])
        self.assertNotIn("SYNTHETIC_PRIVATE_MARKER", str(result) + repr(result) + output.getvalue() + errors.getvalue())

    def test_status_close_failure_cannot_advance_previous_success_time(self):
        prior = self.valid()
        self.write(prior)
        self.assertEqual(self.manager.status(), prior)
        before = self.snapshot()
        original_file = backup._private_file
        def broken_close(path):
            return _FileHook(original_file(path), close_error=OSError("SYNTHETIC_PRIVATE_MARKER"))
        with patch.object(backup, "_private_file", broken_close):
            result = self.manager._record(self.valid(last_attempt_time="2026-10-21T03:00:00Z",
                                                    last_successful_time="2026-10-21T03:00:00Z"))
        self.assertEqual(result["outcome_category"], "cleanup_failure")
        self.assertTrue(result["cleanup_failed"])
        self.assertEqual(result["last_successful_time"], prior["last_successful_time"])
        self.assertEqual(self.snapshot(), before)

    def test_status_write_and_close_failures_preserve_primary_and_clean_independently(self):
        original_file = backup._private_file
        for prior, expected in (("success", "status_unavailable"), ("timeout", "timeout")):
            with self.subTest(prior=prior):
                opened = []
                output, errors = io.StringIO(), io.StringIO()

                def broken_write_and_close(path):
                    wrapped = _FileHook(original_file(path),
                        write_error=OSError("SYNTHETIC_PRIVATE_MARKER/write-path"),
                        close_error=PermissionError("SYNTHETIC_PRIVATE_MARKER/close-path"))
                    opened.append(wrapped)
                    return wrapped

                with patch.object(backup, "_private_file", broken_write_and_close), patch.object(backup.os, "replace") as publish, self.assertNoLogs(), redirect_stdout(output), redirect_stderr(errors):
                    result = self.manager._record(self.valid(outcome_category=prior))
                self.assertEqual(result["outcome_category"], expected)
                self.assertTrue(result["cleanup_failed"])
                publish.assert_not_called()
                self.assertEqual(opened[0].close_calls, 1)
                self.assertTrue(opened[0].stream.closed)
                self.assertFalse(self.path.exists())
                self.assertEqual(list(self.directory.glob(".backup-status-*.pending")), [])
                self.assertNotIn("SYNTHETIC_PRIVATE_MARKER", str(result) + repr(result) + output.getvalue() + errors.getvalue())


if __name__ == "__main__":
    unittest.main()
