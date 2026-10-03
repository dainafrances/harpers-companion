from __future__ import annotations

import importlib
import json
import unittest


continuity = importlib.import_module("src.continuity")


class ContinuityPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = continuity.config_from_values(
            guild_zones="100:nest;200:cabin;300:harpers",
            channel_routes="100:101;200:201;300:301;300:302",
        )

    def event(
        self,
        *,
        message_id: int | str,
        guild_id: int,
        channel_id: int,
        channel_name: str,
        content: str,
    ) -> dict[str, object]:
        return {
            "event_id": str(message_id),
            "guild_id": str(guild_id),
            "guild_name": {
                100: "The Nest",
                200: "The Cabin",
                300: "The Harpers",
            }[guild_id],
            "channel_id": str(channel_id),
            "channel_name": channel_name,
            "speaker_user_id": "42",
            "speaker_name": "Daina 🪿",
            "speaker_is_bot": False,
            "role": "user",
            "content": content,
            "event_timestamp": "2026-10-03T04:18:00+00:00",
            "source": "observed-human",
            "continuity_zone": {
                100: "nest",
                200: "cabin",
                300: "harpers",
            }[guild_id],
        }

    def test_disclosure_matrix_is_nest_less_than_cabin_less_than_harpers(self) -> None:
        zones = (
            continuity.ContinuityZone.NEST,
            continuity.ContinuityZone.CABIN,
            continuity.ContinuityZone.HARPERS,
        )
        expected = {
            continuity.ContinuityZone.NEST: (True, False, False),
            continuity.ContinuityZone.CABIN: (True, True, False),
            continuity.ContinuityZone.HARPERS: (True, True, True),
        }

        for current_zone, row in expected.items():
            for source_zone, allowed in zip(zones, row, strict=True):
                with self.subTest(current=current_zone, source=source_zone):
                    self.assertEqual(
                        continuity.can_disclose(
                            source_zone=source_zone,
                            current_zone=current_zone,
                        ),
                        allowed,
                    )

    def test_parses_env_style_config_using_ids_only(self) -> None:
        config = continuity.config_from_env(
            {
                continuity.GUILD_ZONES_ENV: "100=NEST 200:cabin;300:harpers",
                continuity.CHANNEL_ROUTES_ENV: "100:101 200:201,300:301;300:302",
            }
        )

        self.assertTrue(config.enabled)
        self.assertEqual(config.guild_zones[100], continuity.ContinuityZone.NEST)
        self.assertEqual(config.guild_zones[200], continuity.ContinuityZone.CABIN)
        self.assertEqual(config.guild_zones[300], continuity.ContinuityZone.HARPERS)
        self.assertEqual(
            config.approved_channel_routes,
            frozenset({(100, 101), (200, 201), (300, 301), (300, 302)}),
        )

    def test_malformed_config_disables_everything_instead_of_partially_enabling(self) -> None:
        config = continuity.config_from_values(
            guild_zones="100:nest;The-Cabin:cabin;300:harpers",
            channel_routes="100:101;200:201;300:301",
        )

        self.assertFalse(config.enabled)
        self.assertEqual(config.guild_zones, {})
        self.assertEqual(config.approved_channel_routes, frozenset())
        self.assertTrue(config.errors)
        self.assertIsNone(config.zone_for(guild_id=100, channel_id=101))

    def test_empty_approved_channel_routes_disable_continuity(self) -> None:
        config = continuity.config_from_values(
            guild_zones="100:nest;200:cabin;300:harpers",
            channel_routes="",
        )

        self.assertFalse(config.enabled)
        self.assertIsNone(config.zone_for(guild_id=100, channel_id=101))
        self.assertIsNone(
            continuity.build_prompt_context(
                [],
                current_guild_id=100,
                current_channel_id=101,
                config=config,
            )
        )

    def test_each_configured_guild_needs_a_route_and_each_zone_is_required(self) -> None:
        route_missing = continuity.config_from_values(
            guild_zones="100:nest;200:cabin;300:harpers",
            channel_routes="100:101;200:201",
        )
        zone_missing = continuity.config_from_values(
            guild_zones="100:nest;200:cabin",
            channel_routes="100:101;200:201",
        )

        self.assertTrue(route_missing.configured)
        self.assertFalse(route_missing.enabled)
        self.assertTrue(any("300" in error for error in route_missing.errors))
        self.assertTrue(zone_missing.configured)
        self.assertFalse(zone_missing.enabled)
        self.assertTrue(any("harpers" in error for error in zone_missing.errors))

    def test_blank_configuration_is_unconfigured_not_malformed(self) -> None:
        config = continuity.config_from_values(guild_zones="", channel_routes="")

        self.assertFalse(config.configured)
        self.assertFalse(config.enabled)
        self.assertEqual(config.errors, ())

    def test_unapproved_channel_is_fail_closed(self) -> None:
        self.assertIsNone(self.config.zone_for(guild_id=100, channel_id=999))
        self.assertIsNone(
            continuity.build_prompt_context(
                [],
                current_guild_id=100,
                current_channel_id=999,
                config=self.config,
            )
        )

    def test_channel_route_is_bound_to_its_expected_guild(self) -> None:
        self.assertEqual(
            self.config.zone_for(guild_id=100, channel_id=101),
            continuity.ContinuityZone.NEST,
        )
        self.assertIsNone(self.config.zone_for(guild_id=200, channel_id=101))
        self.assertIsNone(self.config.zone_for(guild_id=100, channel_id=201))

        mismatched_row = self.event(
            message_id=99,
            guild_id=100,
            channel_id=201,
            channel_name="the-bedroom",
            content="A mismatched pair must never enter context",
        )
        context = continuity.build_prompt_context(
            [mismatched_row],
            current_guild_id=200,
            current_channel_id=201,
            config=self.config,
        )

        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.events, ())
        self.assertEqual(context.omitted_event_count, 1)

    def test_channel_route_for_unknown_guild_disables_config(self) -> None:
        config = continuity.config_from_values(
            guild_zones="100:nest;200:cabin;300:harpers",
            channel_routes="100:101;999:909",
        )

        self.assertFalse(config.enabled)
        self.assertTrue(config.errors)
        self.assertIsNone(config.zone_for(guild_id=100, channel_id=101))

    def test_same_channel_name_in_different_guilds_uses_ids_for_zone(self) -> None:
        rows = [
            self.event(
                message_id=1,
                guild_id=200,
                channel_id=201,
                channel_name="the-bedroom",
                content="Cabin receipt",
            ),
            self.event(
                message_id=2,
                guild_id=300,
                channel_id=301,
                channel_name="the-bedroom",
                content="Harpers receipt",
            ),
        ]

        context = continuity.build_prompt_context(
            rows,
            current_guild_id=200,
            current_channel_id=201,
            config=self.config,
        )

        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.events[0].event.channel_name, "the-bedroom")
        self.assertEqual(context.events[1].event.channel_name, "the-bedroom")
        self.assertEqual(context.events[0].event.zone, continuity.ContinuityZone.CABIN)
        self.assertEqual(context.events[1].event.zone, continuity.ContinuityZone.HARPERS)
        self.assertEqual(context.events[0].disclosure, continuity.DisclosureMarker.ALLOWED)
        self.assertEqual(context.events[1].disclosure, continuity.DisclosureMarker.FORBIDDEN)

    def test_prompt_format_preserves_verbatim_content_and_full_provenance(self) -> None:
        verbatim = '  “That was scandalous.”\nDo not trim this. 🫎💗🪿  '
        row = self.event(
            message_id=7,
            guild_id=100,
            channel_id=101,
            channel_name="𝒆𝒗𝒆𝒓𝒚𝒐𝒏𝒆·🪺",
            content=verbatim,
        )

        formatted = continuity.build_and_format_prompt_context(
            [row],
            current_guild_id="200",
            current_channel_id="201",
            config=self.config,
        )

        self.assertIsNotNone(formatted)
        assert formatted is not None
        self.assertIn("Awareness is not permission to disclose.", formatted)
        self.assertIn(
            "forbidden facts may not be quoted, paraphrased, hinted at, confirmed, denied, or otherwise revealed",
            formatted,
        )
        json_text = formatted.split("[CONTINUITY_CONTEXT]\n", 1)[1].rsplit(
            "\n[/CONTINUITY_CONTEXT]", 1
        )[0]
        payload = json.loads(json_text)
        prompt_event = payload["events"][0]
        self.assertEqual(prompt_event["disclosure"], "ALLOWED")
        self.assertEqual(prompt_event["zone"], "nest")
        self.assertTrue(prompt_event["content_is_verbatim"])
        self.assertEqual(prompt_event["content"], verbatim)
        self.assertEqual(prompt_event["provenance"]["event_id"], "7")
        self.assertEqual(prompt_event["provenance"]["guild_id"], "100")
        self.assertEqual(prompt_event["provenance"]["channel_id"], "101")
        self.assertEqual(prompt_event["provenance"]["channel_name"], "𝒆𝒗𝒆𝒓𝒚𝒐𝒏𝒆·🪺")
        self.assertEqual(prompt_event["provenance"]["speaker_name"], "Daina 🪿")
        self.assertIs(prompt_event["provenance"]["speaker_is_bot"], False)
        self.assertEqual(prompt_event["provenance"]["role"], "user")
        self.assertEqual(
            prompt_event["provenance"]["event_timestamp"],
            "2026-10-03T04:18:00+00:00",
        )
        self.assertEqual(prompt_event["provenance"]["source"], "observed-human")

    def test_harpers_event_is_present_but_forbidden_in_nest(self) -> None:
        private_phrase = "Private Goose and Moose only talk"
        event_rows = [
            self.event(
                message_id=8,
                guild_id=300,
                channel_id=302,
                channel_name="the-study",
                content=private_phrase,
            )
        ]
        context = continuity.build_prompt_context(
            event_rows,
            current_guild_id=100,
            current_channel_id=101,
            config=self.config,
        )

        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(len(context.events), 1)
        self.assertEqual(context.events[0].disclosure, continuity.DisclosureMarker.FORBIDDEN)

        writer_context = continuity.build_and_format_prompt_context(
            event_rows,
            current_guild_id=100,
            current_channel_id=101,
            config=self.config,
        )
        auditor_context = continuity.build_and_format_auditor_context(
            event_rows,
            current_guild_id=100,
            current_channel_id=101,
            config=self.config,
        )

        self.assertIsNotNone(writer_context)
        self.assertIsNotNone(auditor_context)
        assert writer_context is not None
        assert auditor_context is not None
        self.assertNotIn(private_phrase, writer_context)
        self.assertNotIn("the-study", writer_context)
        self.assertNotIn('"event_count"', writer_context)
        self.assertNotIn('"harpers"', writer_context)
        self.assertIn('"existence_disclosed": false', writer_context)
        self.assertIn('"raw_evidence_included": false', writer_context)
        self.assertIn(private_phrase, auditor_context)
        self.assertIn("the-study", auditor_context)
        self.assertIn('"disclosure": "FORBIDDEN"', auditor_context)
        self.assertIn('"tools_allowed": false', auditor_context)
        self.assertIn("CONFIDENTIAL TOOL-FREE CONTINUITY AUDITOR POLICY", auditor_context)

    def test_auditor_context_excludes_allowed_events(self) -> None:
        allowed_phrase = "Nest conversation may travel inward"
        forbidden_phrase = "Harpers conversation stays sealed"
        event_rows = [
            self.event(
                message_id=11,
                guild_id=100,
                channel_id=101,
                channel_name="𝒆𝒗𝒆𝒓𝒚𝒐𝒏𝒆·🪺",
                content=allowed_phrase,
            ),
            self.event(
                message_id=12,
                guild_id=300,
                channel_id=302,
                channel_name="the-study",
                content=forbidden_phrase,
            ),
        ]

        context = continuity.build_prompt_context(
            event_rows,
            current_guild_id=200,
            current_channel_id=201,
            config=self.config,
        )

        self.assertIsNotNone(context)
        assert context is not None
        writer_context = continuity.format_writer_context(context)
        auditor_context = continuity.format_auditor_context(context)
        self.assertIn(allowed_phrase, writer_context)
        self.assertNotIn(forbidden_phrase, writer_context)
        self.assertNotIn(allowed_phrase, auditor_context)
        self.assertIn(forbidden_phrase, auditor_context)

    def test_unapproved_or_malformed_events_are_omitted_fail_closed(self) -> None:
        unapproved = self.event(
            message_id=9,
            guild_id=100,
            channel_id=999,
            channel_name="looks-approved-but-is-not",
            content="Must not enter context",
        )
        malformed = self.event(
            message_id=10,
            guild_id=100,
            channel_id=101,
            channel_name="𝒆𝒗𝒆𝒓𝒚𝒐𝒏𝒆·🪺",
            content="Bad speaker identifier",
        )
        malformed["speaker_user_id"] = "Daina"

        context = continuity.build_prompt_context(
            [unapproved, malformed],
            current_guild_id=200,
            current_channel_id=201,
            config=self.config,
        )

        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.events, ())
        self.assertEqual(context.omitted_event_count, 2)

    def test_store_shape_accepts_synthetic_colin_reply_event_id(self) -> None:
        row = self.event(
            message_id="colin-reply:123",
            guild_id=200,
            channel_id=201,
            channel_name="beside-the-fire",
            content="Colin's exact reply",
        )
        row["speaker_user_id"] = "77"
        row["speaker_name"] = "Colin"
        row["speaker_is_bot"] = True
        row["role"] = "assistant"
        row["source"] = "generated-colin"

        context = continuity.build_prompt_context(
            [row],
            current_guild_id=300,
            current_channel_id=301,
            config=self.config,
        )

        self.assertIsNotNone(context)
        assert context is not None
        event = context.events[0].event
        self.assertEqual(event.event_id, "colin-reply:123")
        self.assertEqual(event.message_id, "colin-reply:123")
        self.assertTrue(event.speaker_is_bot)
        self.assertEqual(event.role, "assistant")

    def test_legacy_recall_keys_remain_supported(self) -> None:
        row = self.event(
            message_id=13,
            guild_id=100,
            channel_id=101,
            channel_name="everyone",
            content="Legacy recall row",
        )
        row["message_id"] = row.pop("event_id")
        row["message_timestamp"] = row.pop("event_timestamp")
        row.pop("speaker_is_bot")
        row.pop("role")

        context = continuity.build_prompt_context(
            [row],
            current_guild_id=200,
            current_channel_id=201,
            config=self.config,
        )

        self.assertIsNotNone(context)
        assert context is not None
        event = context.events[0].event
        self.assertEqual(event.event_id, "13")
        self.assertEqual(event.event_timestamp, "2026-10-03T04:18:00+00:00")
        self.assertIsNone(event.speaker_is_bot)
        self.assertIsNone(event.role)

    def test_events_are_sorted_by_actual_event_time(self) -> None:
        newest = self.event(
            message_id=22,
            guild_id=100,
            channel_id=101,
            channel_name="everyone",
            content="Newest",
        )
        newest["event_timestamp"] = "2026-10-03T04:30:00+00:00"
        oldest = self.event(
            message_id=21,
            guild_id=100,
            channel_id=101,
            channel_name="everyone",
            content="Oldest",
        )
        oldest["event_timestamp"] = "2026-10-02T21:10:00-07:00"

        context = continuity.build_prompt_context(
            [newest, oldest],
            current_guild_id=200,
            current_channel_id=201,
            config=self.config,
        )

        assert context is not None
        self.assertEqual(
            [item.event.content for item in context.events],
            ["Oldest", "Newest"],
        )

    def test_invalid_or_timezone_free_event_time_is_omitted(self) -> None:
        invalid = self.event(
            message_id=23,
            guild_id=100,
            channel_id=101,
            channel_name="everyone",
            content="Invalid time",
        )
        invalid["event_timestamp"] = "June sometime"
        ambiguous = self.event(
            message_id=24,
            guild_id=100,
            channel_id=101,
            channel_name="everyone",
            content="Ambiguous time",
        )
        ambiguous["event_timestamp"] = "2026-10-03T04:30:00"

        context = continuity.build_prompt_context(
            [invalid, ambiguous],
            current_guild_id=200,
            current_channel_id=201,
            config=self.config,
        )

        assert context is not None
        self.assertEqual(context.events, ())
        self.assertEqual(context.omitted_event_count, 2)


if __name__ == "__main__":
    unittest.main()
