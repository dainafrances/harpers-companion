from __future__ import annotations

import importlib
import unittest
from types import SimpleNamespace

room_context = importlib.import_module("src.room_context")


class RoomContextTests(unittest.TestCase):
    def message(self, *, guild_id=700, guild_name="Nest", channel_id=500, channel_name="banter"):
        return SimpleNamespace(
            guild=None if guild_id is None else SimpleNamespace(id=guild_id, name=guild_name),
            channel=SimpleNamespace(id=channel_id, name=channel_name),
        )

    def test_dm_gets_private_dm_mode(self) -> None:
        context = room_context.build_room_context(
            self.message(guild_id=None, channel_id=900, channel_name=None),
            is_dm=True,
            config=room_context.RoomContextConfig(guild_labels={}, channel_labels={}),
        )

        self.assertEqual(context.room_mode, room_context.RoomMode.PRIVATE_DM)
        self.assertEqual(context.room_label, "Direct Message")
        self.assertEqual(context.label_source, "dm")
        formatted = room_context.format_room_context(context)
        self.assertIn("room_mode: private_dm", formatted)
        self.assertIn("is_dm: true", formatted)

    def test_channel_label_overrides_guild_label(self) -> None:
        config = room_context.RoomContextConfig(
            guild_labels={700: room_context.RoomLabel(room_context.RoomMode.PUBLIC_COMMUNITY, "The Nest")},
            channel_labels={500: room_context.RoomLabel(room_context.RoomMode.PRIVATE_HOME, "Cottage Home")},
        )

        context = room_context.build_room_context(
            self.message(),
            is_dm=False,
            config=config,
        )

        self.assertEqual(context.room_mode, room_context.RoomMode.PRIVATE_HOME)
        self.assertEqual(context.room_label, "Cottage Home")
        self.assertEqual(context.label_source, "channel")

    def test_guild_label_is_used_when_channel_has_no_label(self) -> None:
        config = room_context.RoomContextConfig(
            guild_labels={700: room_context.RoomLabel(room_context.RoomMode.PUBLIC_COMMUNITY, "The Nest")},
            channel_labels={},
        )

        context = room_context.build_room_context(
            self.message(),
            is_dm=False,
            config=config,
        )

        self.assertEqual(context.room_mode, room_context.RoomMode.PUBLIC_COMMUNITY)
        self.assertEqual(context.room_label, "The Nest")
        self.assertEqual(context.label_source, "guild")

    def test_unknown_room_does_not_guess_from_participants(self) -> None:
        context = room_context.build_room_context(
            self.message(),
            is_dm=False,
            config=room_context.RoomContextConfig(guild_labels={}, channel_labels={}),
        )

        self.assertEqual(context.room_mode, room_context.RoomMode.UNKNOWN)
        self.assertEqual(context.room_label, "unknown")
        self.assertEqual(context.label_source, "none")
        self.assertIn("do not assume", context.privacy_note)

    def test_parse_label_entries(self) -> None:
        parsed = room_context.parse_label_entries(
            "500:private_home:Cottage Home; 700:public_community:The Nest"
        )

        self.assertEqual(parsed[500].room_mode, room_context.RoomMode.PRIVATE_HOME)
        self.assertEqual(parsed[500].room_label, "Cottage Home")
        self.assertEqual(parsed[700].room_mode, room_context.RoomMode.PUBLIC_COMMUNITY)
        self.assertEqual(parsed[700].room_label, "The Nest")


if __name__ == "__main__":
    unittest.main()
