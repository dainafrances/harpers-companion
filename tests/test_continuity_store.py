from __future__ import annotations

import importlib
import tempfile
import unittest
from pathlib import Path

memory = importlib.import_module("src.memory")


class ContinuityStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = memory.DB_PATH
        memory.DB_PATH = Path(self.tempdir.name) / "test.sqlite3"
        memory.init_db()

    def tearDown(self) -> None:
        memory.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def save_event(
        self,
        event_id: str,
        event_timestamp: str,
        content: str,
        *,
        guild_id: str = "nest-guild",
        guild_name: str = "The Nest",
        channel_id: str = "nest-everyone",
        channel_name: str = "everyone",
        continuity_zone: str = "nest",
        speaker_user_id: str = "daina-id",
        speaker_name: str = "Daina",
        speaker_is_bot: bool = False,
        role: str = "user",
        source: str = "observed-human",
    ) -> bool:
        return memory.save_continuity_event(
            event_id=event_id,
            guild_id=guild_id,
            guild_name=guild_name,
            channel_id=channel_id,
            channel_name=channel_name,
            continuity_zone=continuity_zone,
            speaker_user_id=speaker_user_id,
            speaker_name=speaker_name,
            speaker_is_bot=speaker_is_bot,
            role=role,
            content=content,
            event_timestamp=event_timestamp,
            source=source,
        )

    def test_save_is_idempotent_and_preserves_verbatim_content(self) -> None:
        content = "Keep this exactly:\n  spacing, ‘quotes’, emoji 🫎 — and <@42>."

        self.assertTrue(self.save_event("event-1", "2026-10-02T21:18:00+00:00", content))
        self.assertFalse(
            self.save_event(
                "event-1",
                "2026-10-02T21:19:00+00:00",
                "A duplicate must not replace the original.",
            )
        )

        event = memory.get_latest_continuity_event_in_channel_before(
            guild_id="nest-guild",
            channel_id="nest-everyone",
            before_timestamp="2026-10-02T21:20:00+00:00",
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["content"], content)
        self.assertEqual(event["event_timestamp"], "2026-10-02T21:18:00+00:00")
        self.assertIs(event["speaker_is_bot"], False)

        with memory.connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM continuity_events").fetchone()[0]
        self.assertEqual(count, 1)

    def test_same_channel_name_in_different_guilds_is_separated_by_ids(self) -> None:
        self.save_event(
            "cabin-bedroom-event",
            "2026-10-02T21:20:00+00:00",
            "Cabin context",
            guild_id="cabin-guild",
            guild_name="The Cabin",
            channel_id="cabin-bedroom-id",
            channel_name="the-bedroom",
            continuity_zone="cabin",
        )
        self.save_event(
            "harpers-bedroom-event",
            "2026-10-02T21:21:00+00:00",
            "Harpers context",
            guild_id="harpers-guild",
            guild_name="The Harpers",
            channel_id="harpers-bedroom-id",
            channel_name="the-bedroom",
            continuity_zone="harpers",
        )

        cabin_event = memory.get_latest_continuity_event_in_channel_before(
            guild_id="cabin-guild",
            channel_id="cabin-bedroom-id",
            before_timestamp="2026-10-02T21:30:00+00:00",
        )
        harpers_event = memory.get_latest_continuity_event_in_channel_before(
            guild_id="harpers-guild",
            channel_id="harpers-bedroom-id",
            before_timestamp="2026-10-02T21:30:00+00:00",
        )

        assert cabin_event is not None
        assert harpers_event is not None
        self.assertEqual(cabin_event["content"], "Cabin context")
        self.assertEqual(cabin_event["guild_id"], "cabin-guild")
        self.assertEqual(harpers_event["content"], "Harpers context")
        self.assertEqual(harpers_event["guild_id"], "harpers-guild")

    def test_latest_queries_use_event_timestamp_and_strict_cutoff(self) -> None:
        self.save_event("newest-inserted-first", "2026-10-02T21:30:00+00:00", "Thirty")
        self.save_event("oldest-inserted-last", "2026-10-02T21:10:00+00:00", "Ten")
        self.save_event(
            "different-room",
            "2026-10-02T21:20:00+00:00",
            "Twenty",
            guild_id="cabin-guild",
            guild_name="The Cabin",
            channel_id="cabin-fire",
            channel_name="beside-the-fire",
            continuity_zone="cabin",
        )

        channel_event = memory.get_latest_continuity_event_in_channel_before(
            guild_id="nest-guild",
            channel_id="nest-everyone",
            before_timestamp="2026-10-02T21:30:00+00:00",
        )
        speaker_event = memory.get_latest_continuity_event_by_speaker_before(
            speaker_user_id="daina-id",
            before_timestamp="2026-10-02T21:30:00+00:00",
        )

        assert channel_event is not None
        assert speaker_event is not None
        self.assertEqual(channel_event["event_id"], "oldest-inserted-last")
        self.assertEqual(speaker_event["event_id"], "different-room")

        outside_nest = memory.get_latest_continuity_event_by_speaker_before(
            speaker_user_id="daina-id",
            before_timestamp="2026-10-02T22:00:00+00:00",
            exclude_guild_id="nest-guild",
            exclude_channel_id="nest-everyone",
        )
        assert outside_nest is not None
        self.assertEqual(outside_nest["event_id"], "different-room")

        self.assertIsNone(
            memory.get_latest_continuity_event_in_channel_before(
                guild_id="nest-guild",
                channel_id="nest-everyone",
                before_timestamp="2026-10-02T21:10:00+00:00",
            )
        )

    def test_recent_channel_window_is_bounded_and_chronological(self) -> None:
        self.save_event("event-40", "2026-10-02T21:40:00+00:00", "Forty")
        self.save_event("event-10", "2026-10-02T21:10:00+00:00", "Ten")
        self.save_event("event-30", "2026-10-02T21:30:00+00:00", "Thirty")
        self.save_event("event-20", "2026-10-02T21:20:00+00:00", "Twenty")

        events = memory.get_recent_continuity_events_from_channel_before(
            guild_id="nest-guild",
            channel_id="nest-everyone",
            before_timestamp="2026-10-02T21:40:00+00:00",
            limit=2,
        )

        self.assertEqual([event["event_id"] for event in events], ["event-20", "event-30"])
        self.assertEqual(
            [event["event_timestamp"] for event in events],
            ["2026-10-02T21:20:00+00:00", "2026-10-02T21:30:00+00:00"],
        )
        self.assertEqual(
            memory.get_recent_continuity_events_from_channel_before(
                guild_id="nest-guild",
                channel_id="nest-everyone",
                before_timestamp="2026-10-02T22:00:00+00:00",
                limit=0,
            ),
            [],
        )

    def test_recent_guild_awareness_uses_only_approved_routes_and_time_window(self) -> None:
        self.save_event("approved-old", "2026-10-02T20:00:00+00:00", "Too old")
        self.save_event("approved-a", "2026-10-02T21:10:00+00:00", "Approved A")
        self.save_event(
            "approved-b",
            "2026-10-02T21:20:00+00:00",
            "Approved B",
            channel_id="nest-colin",
            channel_name="colin",
        )
        self.save_event(
            "unapproved",
            "2026-10-02T21:30:00+00:00",
            "Must stay out",
            channel_id="nest-secret",
            channel_name="secret",
        )

        events = memory.get_recent_continuity_events_from_guild_before(
            guild_id="nest-guild",
            approved_channel_ids={"nest-everyone", "nest-colin"},
            before_timestamp="2026-10-02T22:00:00+00:00",
            after_timestamp="2026-10-02T21:00:00+00:00",
            limit=5,
            exclude_channel_id="nest-everyone",
        )

        self.assertEqual([event["event_id"] for event in events], ["approved-b"])

    def test_expected_chronology_indexes_exist(self) -> None:
        with memory.connect() as conn:
            indexes = {
                row["name"]
                for row in conn.execute("PRAGMA index_list('continuity_events')").fetchall()
            }

        self.assertTrue(
            {
                "idx_continuity_channel_chronology",
                "idx_continuity_speaker_chronology",
                "idx_continuity_zone_chronology",
            }.issubset(indexes)
        )

    def test_reinitializing_adds_schema_without_rewriting_existing_data(self) -> None:
        memory.save_message(
            channel_id=101,
            user_id=42,
            role="user",
            content="Existing memory stays exactly where it is.",
            source="pre-continuity",
        )

        memory.init_db()

        self.assertEqual(
            memory.get_recent_messages(channel_id=101),
            [
                {
                    "role": "user",
                    "content": "Existing memory stays exactly where it is.",
                    "source": "pre-continuity",
                }
            ],
        )
        timestamped = memory.get_recent_messages(
            channel_id=101,
            include_created_at=True,
        )
        self.assertIn("created_at", timestamped[0])
        self.assertTrue(timestamped[0]["created_at"])


if __name__ == "__main__":
    unittest.main()
