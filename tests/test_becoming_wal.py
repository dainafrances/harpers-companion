"""Non-mutating preflight against committed SQLite recovery state."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from src import becoming as room


SCOPE = room.RoomScope("10", "20", "30")
SIDECARS = ("", "-wal", "-shm", "-journal")


def file_signatures(path: Path):
    """Read original bytes directly; never ask SQLite to inspect the source."""
    return {
        suffix or "main": {
            "exists": candidate.exists(),
            "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest() if candidate.exists() else None,
        }
        for suffix in SIDECARS
        for candidate in (Path(str(path) + suffix),)
    }


def independent_records(path: Path):
    """Recover only a separate verification copy and query it through raw SQL."""
    with tempfile.TemporaryDirectory(prefix="room-independent-sql-") as temporary:
        copy = Path(temporary) / "verify.sqlite3"
        for suffix in ("", "-wal", "-journal"):
            source = Path(str(path) + suffix)
            if source.exists():
                shutil.copyfile(source, Path(str(copy) + suffix))
        with sqlite3.connect(copy) as connection:
            records = {
                "entries": connection.execute("SELECT * FROM entries ORDER BY entry_id").fetchall(),
                "revisions": connection.execute("SELECT * FROM revisions ORDER BY entry_id,revision_number").fetchall(),
                "invocations": connection.execute("SELECT * FROM invocations ORDER BY invocation_id").fetchall(),
                "columns": connection.execute("PRAGMA table_info(entries)").fetchall(),
            }
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='synthetic_payload'").fetchone():
                records["payload"] = connection.execute("SELECT * FROM synthetic_payload ORDER BY id").fetchall()
            return records


class WALPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="room-wal-preflight-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def fixture(self, name="fixture"):
        path = self.directory / name / "room.sqlite3"
        store = room.RoomStore(path)
        store.initialize()
        invocation, fresh = store.begin_invocation(
            operation_key="synthetic-original-operation", trigger_type="becoming_want",
            scope=SCOPE, requested_model="synthetic/model",
        )
        self.assertTrue(fresh)
        receipt = store.write_revision(
            invocation_id=invocation["invocation_id"], scope=SCOPE,
            result=room.RoomModelResult(
                action="create", title="Toy windmill sketch",
                content="I want to sketch a toy windmill for an imaginary garden.",
            ),
        )
        return path, store, receipt["entry_id"]

    def leave_committed_wal(self, path: Path, *, alter_schema: bool):
        # os._exit intentionally prevents the final connection close/checkpoint.
        # All files belong exclusively to this external disposable fixture.
        code = (
            "import os,sqlite3,sys;"
            "c=sqlite3.connect(sys.argv[1]);"
            "c.execute('PRAGMA journal_mode=WAL');"
            "c.execute('PRAGMA wal_autocheckpoint=0');"
            "c.execute(\"UPDATE entries SET status='fulfilled'\");"
            + ("c.execute('ALTER TABLE entries ADD COLUMN unexpected TEXT');" if alter_schema else "")
            + "c.commit();os._exit(0)"
        )
        subprocess.run(
            [sys.executable, "-B", "-c", code, str(path)], check=True,
            env={"PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True,
        )
        self.assertTrue(Path(str(path) + "-wal").is_file())

    def assert_rejection_preserves_every_file(self, path, operation):
        before_files = file_signatures(path)
        before_records = independent_records(path)
        actual_connect = sqlite3.connect
        with patch.object(room.sqlite3, "connect", wraps=actual_connect) as connections:
            with self.assertRaisesRegex(room.RoomError, "schema_mismatch"):
                operation()
        # Rejection must not have opened the actual database through SQLite,
        # even in read-only mode: that could still alter shared memory.
        self.assertFalse(any(str(call.args[0]).startswith(path.as_uri()) for call in connections.call_args_list))
        self.assertEqual(file_signatures(path), before_files)
        self.assertEqual(independent_records(path), before_records)
        self.assertEqual(file_signatures(path), before_files)
        return before_files

    def test_committed_schema_alteration_in_wal_rejected_without_any_file_change(self):
        for keep_shm in (True, False):
            with self.subTest(existing_shm=keep_shm):
                path, store, _ = self.fixture(str(keep_shm))
                self.leave_committed_wal(path, alter_schema=True)
                if not keep_shm:
                    Path(str(path) + "-shm").unlink()
                # Also preserve an existing non-hot journal byte-for-byte.
                Path(str(path) + "-journal").write_bytes(b"Synthetic non-hot rollback journal fixture")
                records = independent_records(path)
                self.assertIn("unexpected", [row[1] for row in records["columns"]])
                self.assertEqual(records["entries"][0][-1], None)  # Added column is committed in WAL.
                self.assertIn("fulfilled", records["entries"][0])
                self.assertEqual([len(records[key]) for key in ("entries", "revisions", "invocations")], [1, 1, 1])
                before = self.assert_rejection_preserves_every_file(path, store.initialize)
                self.assertEqual(before["-shm"]["exists"], keep_shm)
                self.assertTrue(before["-journal"]["exists"])

    def test_invalid_wal_is_rejected_before_every_original_connection_path(self):
        path, store, entry_id = self.fixture()
        self.leave_committed_wal(path, alter_schema=True)
        for operation in (
            lambda: store.get_entry(entry_id, SCOPE),
            lambda: store.get_history(entry_id, SCOPE),
            lambda: store.begin_invocation(
                operation_key="must-not-be-created", trigger_type="becoming_want",
                scope=SCOPE, requested_model="synthetic/model",
            ),
        ):
            self.assert_rejection_preserves_every_file(path, operation)

    def test_invalid_wal_service_fails_before_provider_and_creates_no_receipt(self):
        path, store, _ = self.fixture()
        self.leave_committed_wal(path, alter_schema=True)
        generator = AsyncMock()
        service = room.RoomService(store, generator, "synthetic/model")
        self.assert_rejection_preserves_every_file(
            path, lambda: asyncio.run(service.create(SCOPE, "must-not-be-created", "Synthetic attempt.")),
        )
        generator.assert_not_awaited()
        records = independent_records(path)
        self.assertEqual([len(records[key]) for key in ("entries", "revisions", "invocations")], [1, 1, 1])

    def test_valid_outstanding_wal_remains_usable_and_repeated_initialization_is_idempotent(self):
        path, store, entry_id = self.fixture()
        self.leave_committed_wal(path, alter_schema=False)
        committed = independent_records(path)
        self.assertIn("fulfilled", committed["entries"][0])
        store.initialize()
        self.assertEqual(store.get_entry(entry_id, SCOPE)["status"], "fulfilled")
        self.assertEqual(independent_records(path), committed)
        after_first = file_signatures(path)
        store.initialize()
        self.assertEqual(file_signatures(path), after_first)

    def test_rollback_journal_schema_rejection_preserves_files_and_records(self):
        path, store, _ = self.fixture()
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            connection.execute("ALTER TABLE entries ADD COLUMN unexpected TEXT")
        self.assert_rejection_preserves_every_file(path, store.initialize)

    def test_hot_rollback_journal_recovers_only_in_snapshot_and_originals_are_unchanged(self):
        path, store, _ = self.fixture()
        committed = "Committed synthetic payload " * 256
        with sqlite3.connect(path) as connection:
            connection.execute("ALTER TABLE entries ADD COLUMN unexpected TEXT")
            connection.execute("CREATE TABLE synthetic_payload(id INTEGER PRIMARY KEY, content TEXT)")
            connection.executemany("INSERT INTO synthetic_payload VALUES (?,?)", [(index, committed) for index in range(100)])
        code = (
            "import os,sqlite3,sys;c=sqlite3.connect(sys.argv[1]);"
            "c.execute('PRAGMA cache_size=1');c.execute('BEGIN IMMEDIATE');"
            "c.execute(\"UPDATE synthetic_payload SET content=replace(content,'Committed','Uncommitted')\");"
            "os._exit(0)"
        )
        subprocess.run([sys.executable, "-B", "-c", code, str(path)], check=True,
                       env={"PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True)
        self.assertTrue(Path(str(path) + "-journal").exists())
        self.assertGreater(Path(str(path) + "-journal").stat().st_size, 512)
        records = independent_records(path)
        self.assertEqual(len(records["payload"]), 100)
        self.assertTrue(all(row[1] == committed for row in records["payload"]))
        self.assert_rejection_preserves_every_file(path, store.initialize)

    def test_existing_empty_unmarked_database_is_not_adopted(self):
        for name, contents in (("zero.sqlite3", b""), ("header.sqlite3", None)):
            with self.subTest(name=name):
                path = self.directory / name
                if contents is not None:
                    path.write_bytes(contents)
                else:
                    with sqlite3.connect(path) as connection:
                        connection.execute("VACUUM")
                before = file_signatures(path)
                with self.assertRaisesRegex(room.RoomError, "schema_mismatch"):
                    room.RoomStore(path).initialize()
                self.assertEqual(file_signatures(path), before)

    def test_committed_wal_that_clears_schema_is_not_reinitialized(self):
        path, store, _ = self.fixture()
        code = (
            "import os,sqlite3,sys;c=sqlite3.connect(sys.argv[1]);"
            "c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA wal_autocheckpoint=0');"
            "c.execute('PRAGMA foreign_keys=OFF');"
            "[c.execute('DROP TRIGGER '+row[0]) for row in c.execute(\"SELECT name FROM sqlite_master WHERE type='trigger'\").fetchall()];"
            "[c.execute('DROP TABLE '+name) for name in ('revisions','entries','invocations')];"
            "c.execute('PRAGMA application_id=0');c.execute('PRAGMA user_version=0');"
            "c.commit();os._exit(0)"
        )
        subprocess.run([sys.executable, "-B", "-c", code, str(path)], check=True,
                       env={"PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True)
        self.assertTrue(Path(str(path) + "-wal").exists())
        before = file_signatures(path)
        with self.assertRaisesRegex(room.RoomError, "schema_mismatch"):
            store.initialize()
        self.assertEqual(file_signatures(path), before)

    def test_malformed_existing_database_rejected_without_rewriting_bytes(self):
        path = self.directory / "malformed.sqlite3"
        path.write_bytes(b"Synthetic malformed database fixture")
        before = file_signatures(path)
        with self.assertRaisesRegex(room.RoomError, "schema_mismatch"):
            room.RoomStore(path).initialize()
        self.assertEqual(file_signatures(path), before)

    def test_fresh_creation_and_existing_valid_database_are_supported(self):
        path = self.directory / "fresh.sqlite3"
        self.assertFalse(path.exists())
        store = room.RoomStore(path)
        store.initialize()
        before = file_signatures(path)
        store.initialize()
        self.assertEqual(file_signatures(path), before)
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute("PRAGMA application_id").fetchone()[0], room.APPLICATION_ID)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
