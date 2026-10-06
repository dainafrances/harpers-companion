from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_message_routing import FakeAuthor, FakeBot, FakeChannel, FakeMessage, main
from src.bedroom_exchange import ExchangeClient, GUILD_ID, CHANNEL_ID, PreparedReply


class BedroomRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.colin = FakeAuthor(1, "Colin", bot=True)
        self.ben = FakeAuthor(4, "Ben", bot=True)
        self.owner = FakeAuthor(2, "Daina")
        self.channel = FakeChannel(CHANNEL_ID, "the-bedroom")
        self.fake_bot = FakeBot(self.colin)
        self.client = ExchangeClient(
            user=lambda: self.colin, owner_id=2, peer_name="Ben", log=lambda text: None,
        )
        self.client.enabled = True
        self.client.secret = "invented-test-key-" * 3
        self.patches = [
            patch.object(main, "bot", self.fake_bot),
            patch.object(main, "bedroom", self.client),
            patch.object(main, "configured_guild_ids", set()),
            patch.object(main, "companion_channel_ids", set()),
        ]
        for active in self.patches:
            active.start()

    def tearDown(self):
        for active in reversed(self.patches):
            active.stop()

    def message(self, author=None, content="<@1> a question", mentions=None):
        message = FakeMessage(
            100, author or self.ben, content, channel=self.channel,
            mentions=[self.colin] if mentions is None else mentions,
        )
        message.guild = SimpleNamespace(id=GUILD_ID)
        return message

    async def test_admitted_peer_bypasses_the_legacy_one_exchange_latch(self):
        message = self.message()
        main.bot_to_bot_cooldowns.add(self.ben.id)
        with patch.object(self.client, "admit", AsyncMock(return_value=True)), patch.object(
            main, "handle_chat_message", AsyncMock()
        ) as handler:
            await main.on_message(message)
        handler.assert_awaited_once()
        self.assertEqual(handler.await_args.kwargs["source"], "companion-bot")

    async def test_rejected_peer_cannot_fall_through_to_mass_ping_or_reply_rules(self):
        message = self.message(content="<@1> @everyone question")
        message.mention_everyone = True
        with patch.object(self.client, "admit", AsyncMock(return_value=False)), patch.object(
            main, "save_observed_message"
        ), patch.object(main, "handle_chat_message", AsyncMock()) as handler:
            await main.on_message(message)
        handler.assert_not_awaited()

    async def test_unaddressed_owner_message_resets_before_normal_response_filter(self):
        message = self.message(author=self.owner, content="still here", mentions=[])
        with patch.object(self.client, "observe_owner", AsyncMock()) as observe, patch.object(
            main, "save_observed_message"
        ), patch.object(main, "handle_chat_message", AsyncMock()) as handler:
            await main.on_message(message)
        observe.assert_awaited_once_with(message)
        handler.assert_not_awaited()

    async def test_long_reply_pings_only_after_the_last_chunk_and_publishes_once(self):
        message = self.message(author=self.owner)
        self.channel.send = AsyncMock(side_effect=[
            SimpleNamespace(id=101), SimpleNamespace(id=102),
        ])
        prepared = PreparedReply("a" * 1900 + "\n\n<@4>", 4, "invented-grant")
        with patch.object(self.client, "prepare", AsyncMock(return_value=prepared)), patch.object(
            self.client, "publish", AsyncMock()
        ) as publish:
            await main.send_long_message(
                self.channel, "unused model text", trigger_message=message
            )
        calls = self.channel.send.await_args_list
        self.assertNotIn("<@4>", calls[0].args[0])
        self.assertTrue(calls[-1].args[0].endswith("<@4>"))
        publish.assert_awaited_once_with(prepared, [101, 102])

    async def test_scoped_reply_metadata_does_not_reinvite_the_peer(self):
        message = self.message()
        with patch.object(self.client, "prepare", AsyncMock(return_value=PreparedReply("answer"))):
            await main.send_long_message(self.channel, "answer", reply_to=message)
        kwargs = message.replies[0][1]
        self.assertFalse(kwargs["mention_author"])
        self.assertFalse(kwargs["allowed_mentions"].replied_user)

    async def test_unavailable_or_stale_grant_never_sends_error_text_to_the_room(self):
        message = self.message()
        with patch.object(self.client, "prepare", AsyncMock(return_value=PreparedReply(
            "answer", send=False
        ))), patch.object(main.memory, "try_claim_discord_message", return_value=True), patch.object(
            main, "_discord_retrieval_for_prompt", return_value=main.RecallPromptInputs()
        ), patch.object(main, "_build_continuity_prompt_inputs",
                        return_value=main.ContinuityPromptInputs()), patch.object(
            main, "_record_inbound_continuity_event"
        ), patch.object(main.memory, "save_message"), patch.object(
            main.memory, "get_recent_messages", return_value=[]
        ), patch.object(main, "_latest_journal_for_prompt", return_value=None), patch.object(
            main, "generate_companion_reply", AsyncMock(return_value=main.CompanionResponse(
                reply_text="answer", reaction_emojis=("💚",)
            ))
        ):
            await main.handle_chat_message(message, "question", is_dm=False,
                                           source="companion-bot", reply_to_trigger=True)
        self.assertEqual(self.channel.sent, [])
        self.assertEqual(message.replies, [])
        self.assertEqual(message.added_reactions, [])


class BedroomMentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.colin = SimpleNamespace(id=1)
        self.client = ExchangeClient(
            user=lambda: self.colin, owner_id=2, peer_name="Ben", log=lambda text: None,
        )
        self.client.enabled = True
        self.client.secret = "invented-test-key-" * 3

    async def test_colin_recognizes_deliberate_ben_tags_without_name_only_triggers(self):
        plain, intended = self.client.plain_text("Ben has an idea.", 4)
        self.assertFalse(intended)
        for text in ("@Ben, question?", "[PING: Ben] question?", "<@4> question?"):
            plain, intended = self.client.plain_text(text, 4)
            self.assertTrue(intended)
            self.assertNotIn("[PING:", plain)
            self.assertNotIn("<@4>", plain)

    async def test_remote_failure_preserves_human_text_and_suppresses_peer_ping(self):
        self.client.transport = AsyncMock(side_effect=OSError("invented outage"))
        message = SimpleNamespace(
            id=100, guild=SimpleNamespace(id=GUILD_ID),
            channel=SimpleNamespace(id=CHANNEL_ID), author=SimpleNamespace(id=2, bot=False),
        )
        prepared = await self.client.prepare(message, "@Ben, question?")
        self.assertTrue(prepared.send)
        self.assertIsNone(prepared.peer_id)

    async def test_wrong_guild_with_the_same_channel_id_is_out_of_scope(self):
        message = SimpleNamespace(
            id=100, guild=SimpleNamespace(id=99),
            channel=SimpleNamespace(id=CHANNEL_ID), author=SimpleNamespace(id=2, bot=False),
        )
        self.assertIsNone(await self.client.prepare(message, "@Ben, question?"))
