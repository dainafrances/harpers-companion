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

        def fail_completion(conn, *args, **kwargs):
            # Fail with real SQLite constraint enforcement after entry/revision
            # inserts, without introducing a now-forbidden schema alteration.
            conn.execute("INSERT INTO invocations(invocation_id) VALUES (NULL)")

        with patch.object(self.store, "_complete", side_effect=fail_completion):
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

    def test_altered_schema_definitions_rejected_without_changing_bytes_or_data(self):
        first = self.create()
        expected_history = self.store.get_history(first["entry_id"], SCOPE)
        modifications = {
            "same_named_noop_update_trigger": (
                "DROP TRIGGER immutable_revision_update",
                "CREATE TRIGGER immutable_revision_update BEFORE UPDATE ON revisions BEGIN SELECT 1; END",
            ),
            "same_named_noop_delete_trigger": (
                "DROP TRIGGER immutable_revision_delete",
                "CREATE TRIGGER immutable_revision_delete BEFORE DELETE ON revisions BEGIN SELECT 1; END",
            ),
            "required_table_changed": ("ALTER TABLE entries ADD COLUMN unexpected TEXT",),
            "required_index_changed": (
                "DROP INDEX idx_entries_scope",
                "CREATE INDEX idx_entries_scope ON entries(channel_id)",
            ),
            "required_index_missing": ("DROP INDEX idx_entries_scope",),
            "unexpected_view": ("CREATE VIEW unexpected AS SELECT * FROM entries",),
            "unexpected_index": ("CREATE INDEX unexpected ON entries(status)",),
            # The previous LIKE 'sqlite_%' filter mistook '_' for a wildcard.
            "sqlite_prefix_lookalike": ("CREATE TABLE sqliteXunexpected(content TEXT)",),
        }
        for label, statements in modifications.items():
            with self.subTest(label=label):
                candidate = Path(self.temp.name) / (label + ".sqlite3")
                with sqlite3.connect(self.path) as source, sqlite3.connect(candidate) as target:
                    source.backup(target)
                with sqlite3.connect(candidate) as conn:
                    for statement in statements:
                        conn.execute(statement)
                    before_history = conn.execute("SELECT * FROM revisions ORDER BY revision_number").fetchall()
                before_bytes = candidate.read_bytes()
                with self.assertRaisesRegex(room.RoomError, "schema_mismatch"):
                    room.RoomStore(candidate).initialize()
                self.assertEqual(candidate.read_bytes(), before_bytes)
                with sqlite3.connect(candidate) as conn:
                    self.assertEqual(conn.execute("SELECT * FROM revisions ORDER BY revision_number").fetchall(), before_history)
        self.assertEqual(self.store.get_history(first["entry_id"], SCOPE), expected_history)

    def test_analyze_statistics_are_legitimate_internal_objects(self):
        first = self.create()
        with sqlite3.connect(self.path) as conn:
            conn.execute("ANALYZE")
        before = self.path.read_bytes()
        self.store.initialize()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.get_entry(first["entry_id"], SCOPE)["content"], result().content)

    def test_schema_normalization_preserves_quoted_literals(self):
        self.assertEqual(room._normalized_schema_sql("CREATE\n TABLE x(a TEXT)"), "CREATE TABLE x(a TEXT)")
        self.assertNotEqual(room._normalized_schema_sql("DEFAULT 'two  spaces'"),
                            room._normalized_schema_sql("DEFAULT 'two spaces'"))


class PrivatePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="becoming-private-path-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "checkout"
        self.root.mkdir()
        self.private = self.root / room.PRIVATE_STORAGE_DIRECTORY
        self.root_patch = patch.object(room, "REPOSITORY_ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def test_default_and_nested_paths_are_inside_the_private_directory(self):
        config = room.RoomConfig.from_env({})
        self.assertEqual(config.db_path, Path("data/room-of-becoming/room_of_becoming.sqlite3"))
        paths = (self.root / config.db_path, self.private / "nested" / "extensionless")
        for path in paths:
            with self.subTest(path=path.relative_to(self.root)):
                store = room.RoomStore(path)
                store.initialize()
                self.assertTrue(path.is_file())
                self.assertTrue(store.path.is_relative_to(self.private))

    def case_alias(self, path):
        alias = path.with_name(path.name.swapcase())
        if not alias.exists() or not os.path.samefile(alias, path):
            self.skipTest("Case aliases require a case-insensitive test filesystem")
        return alias

    def test_repository_case_alias_write_is_rejected_before_sqlite_or_provider(self):
        alias = self.case_alias(self.root)
        candidate = alias / "private-room.sqlite3"
        model = AsyncMock(return_value=result())

        async def attempted_write():
            service = room.RoomService(room.RoomStore(candidate), model, "synthetic-model")
            return await service.create(SCOPE, "synthetic-case-alias")

        with patch.object(room.sqlite3, "connect", wraps=sqlite3.connect) as connect:
            with self.assertRaisesRegex(room.RoomError, "invalid_input"):
                asyncio.run(attempted_write())
            connect.assert_not_called()
        model.assert_not_awaited()
        self.assertFalse(candidate.exists())
        self.assertEqual(list(self.root.rglob("*")), [])

    def test_existing_protected_case_aliases_are_allowed_by_filesystem_identity(self):
        self.private.mkdir(parents=True)
        nested = self.private / "nested"
        nested.mkdir()
        root_alias = self.case_alias(self.root)
        data_alias = self.case_alias(self.private.parent)
        private_alias = self.case_alias(self.private)
        nested_alias = self.case_alias(nested)
        candidates = (
            root_alias / room.PRIVATE_STORAGE_DIRECTORY / "root-case.sqlite3",
            data_alias / self.private.name / "intermediate-case.sqlite3",
            private_alias / "private-case.sqlite3",
            root_alias / self.private.parent.name.upper() / self.private.name.upper() / "combined-case.sqlite3",
            nested_alias / "nested-case.sqlite3",
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                store = room.RoomStore(candidate)
                store.initialize()
                canonical = (nested if candidate.parent.name.casefold() == "nested" else self.private) / candidate.name
                self.assertTrue(canonical.exists())
                self.assertTrue(os.path.samefile(store.path, canonical))

    def test_case_alias_of_unprotected_intermediate_directory_is_rejected(self):
        unprotected = self.root / "data" / "nested"
        unprotected.mkdir(parents=True)
        alias = self.case_alias(unprotected)
        with self.assertRaisesRegex(room.RoomError, "invalid_input"):
            room.RoomStore(alias / "room.sqlite3")
        self.assertEqual(list(unprotected.iterdir()), [])

    def test_uppercase_and_mixed_case_reserved_sidecar_suffixes_are_rejected(self):
        for suffix in ("-WAL", "-WaL", "-SHM", "-sHm", "-JOURNAL", "-JoUrNaL"):
            with self.subTest(suffix=suffix):
                candidate = self.private / ("room.sqlite3" + suffix)
                with patch.object(room.sqlite3, "connect", wraps=sqlite3.connect) as connect:
                    with self.assertRaisesRegex(room.RoomError, "database_collision"):
                        room.RoomStore(candidate)
                    connect.assert_not_called()
                self.assertFalse(candidate.exists())
        self.assertEqual(list(self.root.rglob("*")), [])

    def test_case_aliased_sqlite_sidecar_escape_is_rejected(self):
        self.private.mkdir(parents=True)
        alias = self.case_alias(self.private)
        candidate = alias / "room.sqlite3"
        for suffix in ("-WaL", "-ShM", "-JoUrNaL"):
            with self.subTest(suffix=suffix):
                target = Path(self.temp.name).resolve() / ("outside" + suffix)
                sidecar = Path(str(candidate) + suffix)
                sidecar.symlink_to(target)
                try:
                    with self.assertRaisesRegex(room.RoomError, "invalid_input"):
                        room.RoomStore(candidate)
                    self.assertFalse(candidate.exists())
                    self.assertFalse(target.exists())
                finally:
                    sidecar.unlink()

    def test_nonexistent_application_file_case_alias_collision_is_rejected(self):
        external = Path(self.temp.name).resolve() / "external"
        external.mkdir()
        alias = self.case_alias(external)
        protected = external / "legacy.sqlite3"
        candidate = alias / "LEGACY.SQLITE3"
        with self.assertRaisesRegex(room.RoomError, "database_collision"):
            room.RoomStore(candidate, protected_paths=(protected,))
        self.assertFalse(protected.exists())
        self.assertFalse(candidate.exists())

    def test_external_directory_case_alias_remains_supported(self):
        external = Path(self.temp.name).resolve() / "external"
        external.mkdir()
        alias = self.case_alias(external)
        store = room.RoomStore(alias / "room.sqlite3")
        store.initialize()
        self.assertTrue(os.path.samefile(store.path, external / "room.sqlite3"))
        self.assertEqual(list(self.root.rglob("*")), [])

    def test_other_in_repository_paths_are_rejected_before_creation(self):
        candidates = (
            self.root / "private-room.sqlite3",
            self.root / "data/nested/room.sqlite3",
            self.root / "data/private-room",
            self.root / "room.sqlite3-wal",
            self.private,
        )
        for path in candidates:
            with self.subTest(path=path):
                with self.assertRaises(room.RoomError):
                    room.RoomStore(path)
                self.assertFalse(path.exists())
        self.assertEqual(list(self.root.rglob("*")), [])

    def test_traversal_is_rejected_even_when_resolution_would_be_allowed(self):
        for path in (self.private / ".." / "outside.sqlite3",
                     self.private / "nested" / ".." / "room.sqlite3",
                     self.root / ".." / "external.sqlite3"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(room.RoomError, "invalid_input"):
                    room.RoomStore(path)
        self.assertEqual(list(self.root.rglob("*")), [])

    def test_private_directory_and_descendant_symlink_escapes_rejected(self):
        external = Path(self.temp.name) / "external"
        external.mkdir()
        self.private.parent.mkdir(parents=True)
        self.private.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(room.RoomError, "invalid_input"):
            room.RoomStore(self.private / "room.sqlite3")
        self.private.unlink()
        self.private.mkdir()
        (self.private / "nested").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(room.RoomError, "invalid_input"):
            room.RoomStore(self.private / "nested" / "room.sqlite3")
        (self.root / "alias").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(room.RoomError, "invalid_input"):
            room.RoomStore(self.root / "alias" / "room.sqlite3")
        self.assertEqual(list(external.iterdir()), [])

    def test_sidecar_escape_rejected_before_database_or_target_created(self):
        self.private.mkdir(parents=True)
        for suffix in ("-journal", "-wal", "-shm"):
            with self.subTest(suffix=suffix):
                path = self.private / "room.sqlite3"
                target = Path(self.temp.name) / ("external" + suffix)
                sidecar = Path(str(path) + suffix)
                sidecar.symlink_to(target)
                try:
                    with self.assertRaisesRegex(room.RoomError, "invalid_input"):
                        room.RoomStore(path)
                    self.assertFalse(path.exists())
                    self.assertFalse(target.exists())
                finally:
                    sidecar.unlink()

    def test_checkout_alias_cannot_hide_a_private_directory_symlink_escape(self):
        external = Path(self.temp.name).resolve() / "external"
        external.mkdir()
        self.private.parent.mkdir(parents=True)
        self.private.symlink_to(external, target_is_directory=True)
        alias = Path(self.temp.name).resolve() / "checkout-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(room.RoomError, "invalid_input"):
            room.RoomStore(alias / room.DEFAULT_DB_PATH)
        self.assertEqual(list(external.iterdir()), [])

    def test_paths_are_revalidated_after_construction_before_opening_sqlite(self):
        self.private.mkdir(parents=True)
        path = self.private / "room.sqlite3"
        store = room.RoomStore(path)
        external = Path(self.temp.name) / "external-wal"
        Path(str(path) + "-wal").symlink_to(external)
        with self.assertRaisesRegex(room.RoomError, "invalid_input"):
            store.initialize()
        self.assertFalse(path.exists())
        self.assertFalse(external.exists())

    def test_valid_external_directory_remains_supported(self):
        path = Path(self.temp.name) / "external" / "room.sqlite3"
        store = room.RoomStore(path)
        store.initialize()
        self.assertTrue(path.exists())
        self.assertEqual(list(self.root.rglob("*")), [])

    def test_external_hardlink_to_stageable_in_repository_artifact_rejected(self):
        inside = self.root / "stageable.sqlite3"
        inside.touch()
        outside = Path(self.temp.name) / "external.sqlite3"
        os.link(inside, outside)
        with self.assertRaisesRegex(room.RoomError, "database_collision"):
            room.RoomStore(outside)
        self.assertEqual(inside.read_bytes(), b"")

    def test_dedicated_directory_and_arbitrary_sidecars_are_gitignored(self):
        # This read-only Git check belongs to tests; application path validation
        # does not invoke Git or initialize the actual private data directory.
        actual_root = Path(room.__file__).resolve().parents[1]
        candidates = [str(room.PRIVATE_STORAGE_DIRECTORY / name) for name in (
            "room_of_becoming.sqlite3", "room_of_becoming.sqlite3-wal",
            "room_of_becoming.sqlite3-shm", "room_of_becoming.sqlite3-journal",
            "nested/extensionless", "nested/extensionless-wal",
        )]
        checked = subprocess.run(["git", "check-ignore", "--stdin"], cwd=actual_root,
                                 input="\n".join(candidates) + "\n", text=True, capture_output=True, check=True)
        self.assertEqual(checked.stdout.splitlines(), candidates)

    def test_private_path_fixtures_are_removed_without_stageable_artifacts(self):
        with tempfile.TemporaryDirectory(dir=self.private.parent if self.private.parent.exists() else self.root) as sandbox:
            root = Path(sandbox)
            with patch.object(room, "REPOSITORY_ROOT", root):
                path = root / room.DEFAULT_DB_PATH
                room.RoomStore(path).initialize()
                self.assertTrue(path.exists())
        self.assertFalse(root.exists())
        self.assertEqual(list(self.root.rglob("*.sqlite*")), [])


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
