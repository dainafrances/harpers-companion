from __future__ import annotations

import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

continuity = importlib.import_module("src.continuity")
memory = importlib.import_module("src.memory")
recall = importlib.import_module("src.discord_recall")


class DisclosureAwareRecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = memory.DB_PATH
        memory.DB_PATH = Path(self.tempdir.name) / "test.sqlite3"
        memory.init_db()
        self.permissions = recall.RecallPermissions(
            guild_ids={100, 200, 300},
            channel_ids={101, 201, 301},
        )
        self.config = continuity.config_from_values(
            guild_zones="100:nest 200:cabin 300:harpers",
            channel_routes="100:101 200:201 300:301",
        )

    def tearDown(self) -> None:
        memory.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def save_recall(
        self,
        message_id: str,
        content: str,
        timestamp: str,
        *,
        guild_id: str = "100",
        channel_id: str = "101",
        channel_name: str = "everyone",
        speaker_user_id: str = "42",
        speaker_name: str = "Daina",
    ) -> None:
        saved = memory.save_recall_message(
            message_id=message_id,
            guild_id=guild_id,
            channel_id=channel_id,
            channel_name=channel_name,
            speaker_user_id=speaker_user_id,
            speaker_name=speaker_name,
            content=content,
            message_timestamp=timestamp,
            source="observed-human",
        )
        self.assertTrue(saved)

    def retrieve(
        self,
        query: str = "What is the latest conversation?",
        *,
        guild_id: int = 100,
        channel_id: int = 101,
        limit: int = 8,
    ) -> recall.DisclosureAwareRecallResult:
        return recall.retrieve_for_query_with_disclosure(
            query,
            guild_id=guild_id,
            channel_id=channel_id,
            permissions=self.permissions,
            continuity_config=self.config,
            limit=limit,
        )

    def test_partitions_writer_rows_from_confidential_auditor_rows(self) -> None:
        self.save_recall("nest-1", "Public Nest receipt", "2026-10-03T04:10:00+00:00")
        self.save_recall(
            "cabin-1",
            "Cabin-only receipt",
            "2026-10-03T04:20:00+00:00",
            guild_id="200",
            channel_id="201",
            channel_name="beside-the-fire",
        )
        self.save_recall(
            "harpers-1",
            "Harpers-only receipt",
            "2026-10-03T04:30:00+00:00",
            guild_id="300",
            channel_id="301",
            channel_name="the-hearth",
        )

        result = self.retrieve(guild_id=200, channel_id=201)

        self.assertEqual(result.status, recall.RecallStatus.COMPLETE)
        self.assertEqual(
            [row["content"] for row in result.writer_messages],
            ["Cabin-only receipt", "Public Nest receipt"],
        )
        self.assertEqual(
            [row["content"] for row in result.auditor_messages],
            ["Harpers-only receipt"],
        )
        auditor_context = continuity.build_and_format_auditor_context(
            result.auditor_messages,
            current_guild_id=200,
            current_channel_id=201,
            config=self.config,
        )
        assert auditor_context is not None
        self.assertIn("Harpers-only receipt", auditor_context)
        self.assertNotIn("Cabin-only receipt", auditor_context)

    def test_hidden_newer_row_does_not_replace_latest_visible_row(self) -> None:
        self.save_recall("nest-old", "Visible latest", "2026-10-03T04:10:00+00:00")
        self.save_recall(
            "harpers-new",
            "Hidden newer",
            "2026-10-03T04:30:00+00:00",
            guild_id="300",
            channel_id="301",
            channel_name="the-hearth",
        )

        result = self.retrieve(limit=1)

        self.assertEqual(result.status, recall.RecallStatus.COMPLETE)
        self.assertEqual([row["content"] for row in result.writer_messages], ["Visible latest"])
        self.assertEqual([row["content"] for row in result.auditor_messages], ["Hidden newer"])

    def test_second_latest_is_calculated_only_over_visible_rows(self) -> None:
        self.save_recall("visible-older", "Visible older", "2026-10-03T04:10:00+00:00")
        self.save_recall(
            "hidden-middle",
            "Hidden middle",
            "2026-10-03T04:20:00+00:00",
            guild_id="300",
            channel_id="301",
            channel_name="the-hearth",
        )
        self.save_recall("visible-newer", "Visible newer", "2026-10-03T04:30:00+00:00")

        result = self.retrieve("What is the second latest conversation?")

        self.assertEqual(result.status, recall.RecallStatus.COMPLETE)
        self.assertEqual([row["content"] for row in result.writer_messages], ["Visible older"])
        self.assertEqual([row["content"] for row in result.auditor_messages], ["Hidden middle"])

    def test_restricted_only_query_is_indistinguishable_from_no_disclosable_match(self) -> None:
        self.save_recall(
            "harpers-secret",
            "The hidden detail is scarlet.",
            "2026-10-03T04:37:12+00:00",
            guild_id="300",
            channel_id="301",
            channel_name="the-hearth",
            speaker_user_id="987",
            speaker_name="Secret Speaker",
        )

        result = self.retrieve()
        writer_context = recall.format_disclosure_aware_writer_context(
            query="What is the latest conversation?",
            result=result,
            guild_id=100,
            channel_id=101,
        )

        self.assertEqual(result.status, recall.RecallStatus.PARTIAL)
        self.assertEqual(result.note, recall.NO_DISCLOSABLE_RESULTS_NOTE)
        self.assertEqual(result.writer_messages, ())
        self.assertEqual(len(result.auditor_messages), 1)
        self.assertIn("status: PARTIAL", writer_context)
        self.assertIn(recall.NO_DISCLOSABLE_RESULTS_NOTE, writer_context)
        self.assertIn(recall.INERT_TRANSCRIPT_MARKER, writer_context)
        self.assertNotIn("The hidden detail is scarlet.", writer_context)
        self.assertNotIn("Secret Speaker", writer_context)
        self.assertNotIn("987", writer_context)
        self.assertNotIn("2026-10-03T04:37:12", writer_context)
        self.assertNotIn("the-hearth", writer_context)
        self.assertNotIn("harpers-secret", writer_context)
        self.assertNotIn("The hidden detail is scarlet.", repr(result))

    def test_restricted_note_is_constant_across_hidden_and_absent_results(self) -> None:
        self.save_recall(
            "harpers-a",
            "First private fact",
            "2026-10-03T04:10:00+00:00",
            guild_id="300",
            channel_id="301",
        )
        first = self.retrieve()
        self.save_recall(
            "harpers-b",
            "Second private fact",
            "2026-10-03T04:20:00+00:00",
            guild_id="300",
            channel_id="301",
        )
        second = self.retrieve()

        self.assertEqual(first.status, recall.RecallStatus.PARTIAL)
        self.assertEqual(second.status, recall.RecallStatus.PARTIAL)
        self.assertEqual(first.note, second.note)
        self.assertEqual(first.note, recall.NO_DISCLOSABLE_RESULTS_NOTE)

        no_match = self.retrieve(
            "latest conversation about zzyzx-never-present",
            guild_id=100,
            channel_id=101,
        )
        self.assertEqual(no_match.status, recall.RecallStatus.PARTIAL)
        self.assertEqual(no_match.note, first.note)
        self.assertEqual(no_match.writer_messages, ())
        self.assertEqual(no_match.auditor_messages, ())

    def test_mixed_writer_context_never_mentions_hidden_matches(self) -> None:
        self.save_recall("nest-visible", "Visible receipt", "2026-10-03T04:10:00+00:00")
        self.save_recall(
            "harpers-secret",
            "Private receipt",
            "2026-10-03T04:20:00+00:00",
            guild_id="300",
            channel_id="301",
            speaker_user_id="987",
            speaker_name="Secret Speaker",
        )
        result = self.retrieve()

        writer_context = recall.format_disclosure_aware_writer_context(
            query="What is the latest conversation?",
            result=result,
            guild_id=100,
            channel_id=101,
        )

        self.assertIn("Visible receipt", writer_context)
        self.assertNotIn("Private receipt", writer_context)
        self.assertNotIn("Secret Speaker", writer_context)
        self.assertNotIn("DISCLOSURE_RESTRICTED", writer_context)
        self.assertNotIn("forbidden", writer_context.lower())

    def test_exact_duplicates_are_returned_once_and_conflicts_are_dropped(self) -> None:
        row = {
            "message_id": "duplicate",
            "guild_id": "100",
            "channel_id": "101",
            "channel_name": "everyone",
            "speaker_user_id": "42",
            "speaker_name": "Daina",
            "content": "One receipt",
            "message_timestamp": "2026-10-03T04:10:00+00:00",
            "source": "observed-human",
        }
        with patch.object(memory, "search_recall_messages", return_value=[row, dict(row)]):
            exact = self.retrieve()
        self.assertEqual([item["content"] for item in exact.writer_messages], ["One receipt"])

        conflicting = dict(row, content="Conflicting receipt")
        with patch.object(memory, "search_recall_messages", return_value=[row, conflicting]):
            conflict = self.retrieve()
        self.assertEqual(conflict.status, recall.RecallStatus.PARTIAL)
        self.assertEqual(conflict.writer_messages, ())
        self.assertEqual(conflict.auditor_messages, ())

    def test_enabled_continuity_with_unknown_destination_fails_closed(self) -> None:
        self.save_recall("nest-visible", "Visible elsewhere", "2026-10-03T04:10:00+00:00")

        result = self.retrieve(guild_id=100, channel_id=999)

        self.assertEqual(result.status, recall.RecallStatus.PERMISSION_LIMITED)
        self.assertEqual(result.writer_messages, ())
        self.assertEqual(result.auditor_messages, ())

    def test_disabled_continuity_preserves_legacy_recall_behavior(self) -> None:
        self.save_recall(
            "harpers-legacy",
            "Legacy recall result",
            "2026-10-03T04:10:00+00:00",
            guild_id="300",
            channel_id="301",
        )
        disabled = continuity.config_from_values(guild_zones="", channel_routes="")

        result = recall.retrieve_for_query_with_disclosure(
            "What is the latest conversation?",
            guild_id=100,
            channel_id=101,
            permissions=self.permissions,
            continuity_config=disabled,
            limit=1,
        )

        self.assertEqual(result.status, recall.RecallStatus.COMPLETE)
        self.assertEqual(
            [row["content"] for row in result.writer_messages],
            ["Legacy recall result"],
        )
        self.assertEqual(result.auditor_messages, ())

    def test_visible_and_sealed_routes_are_queried_separately(self) -> None:
        with patch.object(memory, "search_recall_messages", return_value=[]) as search:
            self.retrieve(limit=1)

        self.assertEqual(search.call_count, 2)
        visible_call, sealed_call = search.call_args_list
        self.assertEqual(visible_call.kwargs["allowed_guild_ids"], {"100"})
        self.assertEqual(visible_call.kwargs["allowed_channel_ids"], {"101"})
        self.assertEqual(sealed_call.kwargs["allowed_guild_ids"], {"200", "300"})
        self.assertEqual(sealed_call.kwargs["allowed_channel_ids"], {"201", "301"})


if __name__ == "__main__":
    unittest.main()
