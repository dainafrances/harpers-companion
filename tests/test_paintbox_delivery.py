from __future__ import annotations

import importlib
import os
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
main = importlib.import_module("src.main")
paintbox = importlib.import_module("src.paintbox")


class Channel:
    id = 500
    name = "invented-room"

    def __init__(self):
        self.send = AsyncMock()

    def typing(self):
        class Typing:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *args):
                return False
        return Typing()


class PaintboxDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def deliver(self, response, post, *, bot_author=False, bedroom=False):
        channel = Channel()
        message = SimpleNamespace(id=10, author=SimpleNamespace(id=2, bot=bot_author), channel=channel,
                                  guild=SimpleNamespace(id=700), attachments=[], embeds=[])
        saved = Mock()
        written = AsyncMock()
        generate = AsyncMock(return_value=response)
        status = SimpleNamespace(update=AsyncMock())
        with ExitStack() as stack:
            replacements = {
                "bot": SimpleNamespace(user=SimpleNamespace(id=1)), "owner_id": 2,
                "_discord_retrieval_for_prompt": Mock(return_value=main.RecallPromptInputs()),
                "_build_continuity_prompt_inputs": Mock(return_value=main.ContinuityPromptInputs()),
                "_record_inbound_continuity_event": Mock(), "_record_outbound_continuity_event": Mock(),
                "_speaker_header": Mock(return_value=""), "_latest_journal_for_prompt": Mock(return_value=None),
                "in_bedroom": Mock(return_value=bedroom), "generate_companion_reply": generate,
                "send_long_message": written, "post_painted_image": post,
                "status_presence": status,
            }
            for key, value in replacements.items():
                stack.enter_context(patch.object(main, key, value))
            stack.enter_context(patch.object(main.memory, "try_claim_discord_message", return_value=True))
            stack.enter_context(patch.object(main.memory, "get_recent_messages", return_value=[]))
            stack.enter_context(patch.object(main.memory, "save_message", saved))
            stack.enter_context(patch.object(main.discord_recall, "index_message"))
            stack.enter_context(patch.object(main.bedroom, "prepare", AsyncMock(return_value=SimpleNamespace(send=True))))
            await main.handle_chat_message(message, "Paint the shore", is_dm=False, source="human-direct")
        return channel, saved, written, generate, status

    async def test_upload_failure_preserves_text_status_and_does_not_record_false_image(self):
        posted = paintbox.PostedImage(b"fake", "image.png", "The shore")
        response = main.CompanionResponse(reply_text="Written reply. 💚", status_text="At the shore", images=(posted,))
        post = AsyncMock(side_effect=RuntimeError("private provider body"))
        channel, saved, written, _, status = await self.deliver(response, post)
        written.assert_awaited_once()
        self.assertEqual(written.await_args.args[1], "Written reply. 💚")
        status.update.assert_awaited_once_with("At the shore")
        self.assertFalse(any("[IMAGE POSTED]" in item.kwargs["content"] for item in saved.call_args_list))
        channel.send.assert_not_awaited()

    async def test_successful_image_is_posted_after_text_and_then_recorded(self):
        posted = paintbox.PostedImage(b"fake", "image.png", "The shore")
        response = main.CompanionResponse(reply_text="Written reply", images=(posted,))
        post = AsyncMock()
        _, saved, written, _, _ = await self.deliver(response, post)
        self.assertEqual(written.await_args.args[1], "Written reply")
        post.assert_awaited_once()
        self.assertEqual(post.await_args.args[1], posted)
        self.assertEqual(saved.call_args_list[-1].kwargs["content"], "[IMAGE POSTED] The shore")

    async def test_one_upload_failure_does_not_prevent_other_selected_image(self):
        images = (paintbox.PostedImage(b"a", "a.png", "a"), paintbox.PostedImage(b"b", "b.png", "b"))
        post = AsyncMock(side_effect=[RuntimeError("invented error"), None])
        _, saved, _, _, _ = await self.deliver(main.CompanionResponse(reply_text="Written", images=images), post)
        self.assertEqual(post.await_count, 2)
        markers = [item.kwargs["content"] for item in saved.call_args_list if "[IMAGE POSTED]" in item.kwargs["content"]]
        self.assertEqual(markers, ["[IMAGE POSTED] b"])

    async def test_bot_and_bedroom_disallow_image_selection(self):
        for bot_author, bedroom in ((True, False), (False, True)):
            with self.subTest(bot_author=bot_author, bedroom=bedroom):
                _, _, _, generate, _ = await self.deliver(
                    main.CompanionResponse(reply_text="Written"), AsyncMock(),
                    bot_author=bot_author, bedroom=bedroom,
                )
                self.assertFalse(generate.await_args.kwargs["paint_allowed"])

    async def test_attachment_upload_uses_current_room_and_disables_mentions(self):
        channel = Channel()
        posted = paintbox.PostedImage(b"fake", "image.png", "@everyone")
        await main.post_painted_image(channel, posted)
        args = channel.send.await_args.kwargs
        self.assertEqual(args["content"], "@everyone")
        self.assertEqual(args["file"].filename, "image.png")
        self.assertFalse(args["allowed_mentions"].everyone)
        self.assertFalse(args["allowed_mentions"].users)
        self.assertFalse(args["allowed_mentions"].roles)


if __name__ == "__main__":
    unittest.main()
