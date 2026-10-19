from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from src import becoming as room


SCOPE = room.RoomScope("123", "456", "789")
OTHER_SCOPE = room.RoomScope("124", "456", "789")


def result(action="create", content="A synthetic want.\nKept exactly. ", **kwargs):
    return room.RoomModelResult(
        action, title="Synthetic fixture", content=content, reason="A short change summary.",
        requested_model="requested/model", returned_model="actual/model",
        returned_provider="actual provider", provider_response_id="response-123",
        prompt_digest="a" * 64, **kwargs,
    )


class ConfigTests(unittest.TestCase):
    def env(self, **changes):
        return {"BECOMING_ENABLED": "true", "BOT_OWNER_DISCORD_ID": "123",
                "BECOMING_CHANNEL_ID": "456", "BECOMING_GUILD_ID": "789", **changes}

    def test_explicit_guild_route_requires_owner_channel_and_guild(self):
        config = room.RoomConfig.from_env(self.env())
        self.assertTrue(config.authorized(user_id=123, channel_id=456, guild_id=789, is_dm=False))
        for field, value in (("user_id", 9), ("channel_id", 9), ("guild_id", 9), ("is_dm", True)):
            fields = dict(user_id=123, channel_id=456, guild_id=789, is_dm=False)
            fields[field] = value
            self.assertFalse(config.authorized(**fields))

    def test_no_guild_requires_actual_dm_and_no_guild(self):
        config = room.RoomConfig.from_env(self.env(BECOMING_GUILD_ID=""))
        self.assertTrue(config.authorized(user_id=123, channel_id=456, guild_id=None, is_dm=True))
        self.assertFalse(config.authorized(user_id=123, channel_id=456, guild_id=None, is_dm=False))
        self.assertFalse(config.authorized(user_id=123, channel_id=456, guild_id=789, is_dm=True))

    def test_missing_invalid_or_disabled_configuration_fails_closed(self):
        for key in ("BOT_OWNER_DISCORD_ID", "BECOMING_CHANNEL_ID", "BECOMING_GUILD_ID"):
            for value in ("bogus", "0", "-1", "18446744073709551616", "12,34"):
                config = room.RoomConfig.from_env(self.env(**{key: value}))
                self.assertFalse(config.valid)
        for key in ("BOT_OWNER_DISCORD_ID", "BECOMING_CHANNEL_ID", "BECOMING_DB_PATH"):
            self.assertFalse(room.RoomConfig.from_env(self.env(**{key: ""})).valid)
        for value in ("false", "yes", "1", ""):
            self.assertFalse(room.RoomConfig.from_env(self.env(BECOMING_ENABLED=value)).enabled)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "room.sqlite3"
        self.store = room.RoomStore(self.path)
        self.store.initialize()

    def begin(self, key="op-1", trigger="becoming_want", scope=SCOPE):
        return self.store.begin_invocation(operation_key=key, trigger_type=trigger,
                                           scope=scope, requested_model="requested/model")[0]

    def create(self, key="op-1"):
        invocation = self.begin(key)
        return self.store.write_revision(invocation_id=invocation["invocation_id"], scope=SCOPE, result=result())

    def rows(self, table):
        with sqlite3.connect(self.path) as conn:
            return conn.execute(f"SELECT * FROM {table}").fetchall()

    def test_schema_marker_version_and_private_file_permissions(self):
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA application_id").fetchone()[0], room.APPLICATION_ID)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_create_retrieve_and_reinitialization_preserve_exact_content(self):
        receipt = self.create()
        self.store.initialize()
        entry = self.store.get_entry(receipt["entry_id"], SCOPE)
        self.assertEqual(entry["content"], result().content)
        self.assertEqual(entry["current_revision"], 1)
        self.assertEqual(entry["author_classification"], "colin_model")
        self.assertEqual(entry["status"], "active")
        self.assertEqual(entry["returned_provider"], "actual provider")

    def test_retrieve_in_fresh_interpreter(self):
        receipt = self.create()
        code = (
            "import json,sys;from src.becoming import RoomStore,RoomScope;"
            "s=RoomStore(sys.argv[1]);s.initialize();"
            "print(json.dumps(s.get_entry(sys.argv[2],RoomScope('123','456','789'))))"
        )
        child = subprocess.run([sys.executable, "-B", "-c", code, str(self.path), receipt["entry_id"]],
                               text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(child.stdout)["content"], result().content)

    def test_second_revision_preserves_first_verbatim_and_parent(self):
        first = self.create()
        before = self.store.get_history(first["entry_id"], SCOPE)[0]
        invocation = self.begin("op-2", "becoming_revise")
        self.store.write_revision(invocation_id=invocation["invocation_id"], scope=SCOPE,
                                  entry_id=first["entry_id"], expected_revision=1,
                                  result=result("revise", "A changed synthetic want."))
        history = self.store.get_history(first["entry_id"], SCOPE)
        self.assertEqual(history[0], before)
        self.assertEqual(history[1]["parent_revision_number"], 1)
        self.assertEqual(self.store.get_entry(first["entry_id"], SCOPE)["current_revision"], 2)

    def test_revisions_and_finalized_receipts_reject_mutation(self):
        self.create()
        with sqlite3.connect(self.path) as conn:
            for statement in ("UPDATE revisions SET content='changed'", "DELETE FROM revisions",
                              "UPDATE invocations SET outcome='failed'", "DELETE FROM invocations"):
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(statement)

    def test_duplicate_operation_returns_original_receipt(self):
        first = self.create()
        duplicate, fresh = self.store.begin_invocation(operation_key="op-1", trigger_type="becoming_want",
                                                      scope=SCOPE, requested_model="different")
        self.assertFalse(fresh)
        self.assertEqual(duplicate, first)
        self.assertEqual(len(self.rows("entries")), 1)

    def test_operation_key_cannot_be_reused_by_another_scope(self):
        self.create()
        with self.assertRaisesRegex(room.RoomError, "operation_conflict"):
            self.begin(scope=OTHER_SCOPE)

    def test_reads_cannot_cross_scope(self):
        first = self.create()
        self.assertIsNone(self.store.get_entry(first["entry_id"], OTHER_SCOPE))
        self.assertEqual(self.store.get_history(first["entry_id"], OTHER_SCOPE), [])

    def test_stale_revision_has_no_partial_write(self):
        first = self.create()
        invocation = self.begin("op-2", "becoming_revise")
        with self.assertRaisesRegex(room.RoomError, "stale_revision"):
            self.store.write_revision(invocation_id=invocation["invocation_id"], scope=SCOPE,
                                      entry_id=first["entry_id"], expected_revision=2,
                                      result=result("revise"))
        self.assertEqual(len(self.rows("revisions")), 1)

    def test_write_failure_rolls_back_entry_revision_and_success_receipt(self):
        invocation = self.begin()
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TRIGGER inject_failure BEFORE UPDATE ON invocations BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaisesRegex(room.RoomError, "storage_failure"):
            self.store.write_revision(invocation_id=invocation["invocation_id"], scope=SCOPE, result=result())
        self.assertEqual(self.rows("entries"), [])
        self.assertEqual(self.rows("revisions"), [])
        with sqlite3.connect(self.path) as conn:
            self.assertIsNone(conn.execute("SELECT outcome FROM invocations").fetchone()[0])

    def test_initialization_refuses_foreign_database_without_changes(self):
        path = Path(self.temp.name) / "other.sqlite3"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE unrelated(secret TEXT)")
            conn.execute("INSERT INTO unrelated VALUES ('synthetic sentinel')")
        before = path.read_bytes()
        with self.assertRaisesRegex(room.RoomError, "database_collision"):
            room.RoomStore(path).initialize()
        self.assertEqual(path.read_bytes(), before)

    def test_unknown_schema_version_is_not_migrated(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("PRAGMA user_version=2")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(room.RoomError, "schema_mismatch"):
            self.store.initialize()
        self.assertEqual(self.path.read_bytes(), before)

    def test_known_application_path_and_hardlink_collisions_rejected(self):
        legacy = Path(self.temp.name) / "legacy.sqlite3"
        legacy.write_bytes(b"synthetic protected data")
        before = legacy.read_bytes()
        for candidate in (legacy, Path(self.temp.name) / "alias.sqlite3"):
            if candidate != legacy:
                os.link(legacy, candidate)
            with self.assertRaisesRegex(room.RoomError, "database_collision"):
                room.RoomStore(candidate, protected_paths=(legacy,))
        self.assertEqual(legacy.read_bytes(), before)
        for path in (Path("data/colin_memory.sqlite3"), Path("data/paintbox-usage.sqlite")):
            with self.assertRaisesRegex(room.RoomError, "database_collision"):
                room.RoomStore(path)

    def test_configured_paintbox_path_is_protected(self):
        path = str(Path(self.temp.name) / "custom-images.db")
        with patch.dict(os.environ, {"PAINTBOX_USAGE_PATH": path}):
            with self.assertRaisesRegex(room.RoomError, "database_collision"):
                room.RoomStore(path)

    def test_reserved_database_sidecar_paths_cannot_be_used_as_room_database(self):
        legacy = Path(self.temp.name) / "legacy.sqlite3"
        for suffix in ("-wal", "-shm", "-journal"):
            candidate = Path(str(legacy) + suffix)
            with self.assertRaisesRegex(room.RoomError, "database_collision"):
                room.RoomStore(candidate, protected_paths=(legacy,))
            self.assertFalse(candidate.exists())

    def test_invalid_expansion_is_a_bounded_configuration_error(self):
        with self.assertRaisesRegex(room.RoomError, "invalid_input"):
            room.RoomStore("~room_becoming_missing_user/database.sqlite3")

    def test_room_sidecar_cannot_overlap_a_configured_application_database(self):
        candidate = Path(self.temp.name) / "candidate"
        with patch.dict(os.environ, {"PAINTBOX_USAGE_PATH": str(candidate) + "-wal"}):
            with self.assertRaisesRegex(room.RoomError, "database_collision"):
                room.RoomStore(candidate)

    def test_missing_immutability_trigger_is_rejected_without_repair(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("DROP TRIGGER immutable_revision_update")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(room.RoomError, "schema_mismatch"):
            self.store.initialize()
        self.assertEqual(self.path.read_bytes(), before)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "room.sqlite3"
        self.store = room.RoomStore(self.path)
        self.model = AsyncMock(return_value=result())
        self.service = room.RoomService(self.store, self.model, "requested/model")

    async def test_duplicate_delivery_calls_model_once(self):
        first, second = await asyncio.gather(self.service.create(SCOPE, "op-1"), self.service.create(SCOPE, "op-1"))
        self.assertEqual(self.model.await_count, 1)
        self.assertEqual(first["invocation_id"], second["invocation_id"])
        self.assertIn("created", {first["outcome"], second["outcome"]})

    async def test_pending_operation_is_not_retried_or_claimed_successful(self):
        self.store.initialize()
        self.store.begin_invocation(operation_key="op-1", trigger_type="becoming_want", scope=SCOPE,
                                    requested_model="requested/model")
        receipt = await self.service.create(SCOPE, "op-1")
        self.assertIsNone(receipt["outcome"])
        self.assertIsNone(receipt["completed_at"])
        self.model.assert_not_awaited()

    async def test_abstention_creates_receipt_without_entry(self):
        self.model.return_value = room.RoomModelResult("abstain", reason="No want to record in this invocation.")
        receipt = await self.service.create(SCOPE, "op-1")
        self.assertEqual(receipt["outcome"], "abstained")
        self.assertIsNone(receipt["entry_id"])
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM entries").fetchone()[0], 0)

    async def test_malformed_output_and_provider_failure_have_no_entry(self):
        for index, failure in enumerate((room.RoomModelResult("wrong"), room.RoomModelError("provider_failure"))):
            if isinstance(failure, Exception):
                self.model.side_effect = failure
            else:
                self.model.return_value = failure
            receipt = await self.service.create(SCOPE, f"op-{index}")
            self.assertEqual(receipt["outcome"], "failed")
            self.assertIsNone(receipt["entry_id"])
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM entries").fetchone()[0], 0)

    async def test_unknown_provider_metadata_is_never_invented(self):
        self.model.return_value = room.RoomModelResult("create", title="Synthetic", content="Synthetic")
        receipt = await self.service.create(SCOPE, "op-1")
        for field in ("returned_model", "returned_provider", "provider_response_id"):
            self.assertEqual(receipt[field], "UNKNOWN")
        self.assertEqual(receipt["requested_model"], "requested/model")

    async def test_cancellation_preserves_honest_incomplete_operation_without_retry(self):
        started = asyncio.Event()

        async def interrupted(**kwargs):
            started.set()
            await asyncio.Event().wait()

        self.service.generator = interrupted
        task = asyncio.create_task(self.service.create(SCOPE, "op-1"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.service.generator = self.model
        receipt = await self.service.create(SCOPE, "op-1")
        self.assertIsNone(receipt["outcome"])
        self.assertIsNone(receipt["entry_id"])
        self.model.assert_not_awaited()

    async def test_stale_revision_is_denied_before_calling_model(self):
        created = await self.service.create(SCOPE, "op-1")
        self.model.reset_mock()
        receipt = await self.service.revise(SCOPE, "op-2", created["entry_id"], 2, "Synthetic instruction")
        self.assertEqual(receipt["outcome"], "denied")
        self.assertEqual(receipt["error_category"], "stale_revision")
        self.model.assert_not_awaited()

    async def test_concurrent_revisions_only_one_can_append(self):
        created = await self.service.create(SCOPE, "op-1")
        barrier = asyncio.Event()
        arrivals = 0

        async def generate(**kwargs):
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                barrier.set()
            await barrier.wait()
            return result("revise", "A concurrent synthetic revision.")

        self.service.generator = generate
        receipts = await asyncio.gather(*[
            self.service.revise(SCOPE, key, created["entry_id"], 1, "Synthetic instruction")
            for key in ("op-2", "op-3")
        ])
        self.assertEqual(sorted(row["outcome"] for row in receipts), ["denied", "revised"])
        self.assertEqual(len(await self.service.history(SCOPE, created["entry_id"])), 2)

    async def test_revision_abstention_preserves_current_revision(self):
        created = await self.service.create(SCOPE, "op-1")
        self.model.return_value = room.RoomModelResult("abstain", reason="Keep the existing synthetic want.")
        receipt = await self.service.revise(SCOPE, "op-2", created["entry_id"], 1, "Synthetic instruction")
        self.assertEqual(receipt["outcome"], "abstained")
        self.assertEqual((await self.service.show(SCOPE, created["entry_id"]))["current_revision"], 1)

    async def test_storage_failure_never_returns_created(self):
        with patch.object(self.store, "write_revision", side_effect=room.RoomError("storage_failure")):
            receipt = await self.service.create(SCOPE, "op-1")
        self.assertEqual(receipt["outcome"], "failed")
        with patch.object(self.store, "initialize", side_effect=room.RoomError("storage_failure")):
            with self.assertRaisesRegex(room.RoomError, "storage_failure"):
                await self.service.create(SCOPE, "op-2")

    async def test_existing_application_databases_remain_byte_identical(self):
        sentinel_paths = []
        for name in ("legacy-memory.sqlite3", "paintbox.sqlite"):
            path = Path(self.temp.name) / name
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE sentinel(content TEXT)")
                conn.execute("INSERT INTO sentinel VALUES ('synthetic sentinel')")
            sentinel_paths.append(path)
        before = {path: hashlib.sha256(path.read_bytes()).digest() for path in sentinel_paths}
        created = await self.service.create(SCOPE, "op-1")
        self.model.return_value = result("revise")
        await self.service.revise(SCOPE, "op-2", created["entry_id"], 1, "Synthetic instruction")
        await self.service.show(SCOPE, created["entry_id"])
        await self.service.history(SCOPE, created["entry_id"])
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).digest() for path in sentinel_paths})


if __name__ == "__main__":
    unittest.main()
