from __future__ import annotations

import base64
import importlib
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("OPENROUTER_API_KEY", "test-key")

router = importlib.import_module("src.router")
paintbox = importlib.import_module("src.paintbox")

PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-image-bytes"
DATA_URL = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()


def _call(call_id: str, name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _chat(content=None, tool_calls=None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


def _image_response(url: str = DATA_URL) -> SimpleNamespace:
    message = SimpleNamespace(
        content="",
        images=[{"type": "image_url", "image_url": {"url": url}}],
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _client(create: AsyncMock) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


async def _reply(create: AsyncMock, **overrides):
    kwargs = dict(
        user_text="Paint me the cottage?",
        history=[],
        latest_journal=None,
        is_dm=False,
        speaker_name="Daina",
        speaker_is_owner=True,
        paint_allowed=True,
    )
    kwargs.update(overrides)
    with (
        patch.object(router, "_client", _client(create)),
        patch.object(router, "build_system_prompt", return_value="IDENTITY"),
    ):
        return await router.generate_companion_reply(**kwargs)


def _tool_names(call_kwargs) -> list[str]:
    return [
        tool.get("function", {}).get("name")
        for tool in call_kwargs["tools"]
        if tool.get("type") == "function"
    ]


@patch.dict(os.environ, {"PAINTBOX_ENABLED": "true", "ENABLE_WEB_SEARCH": "false"})
class PaintboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        paintbox.reset_cap_for_tests()

    async def test_companion_paints_looks_and_posts_with_own_caption(self) -> None:
        create = AsyncMock(side_effect=[
            _chat(tool_calls=[_call("c1", "create_image", {"prompt": "a copper-lit cottage"})]),
            _image_response(),
            _chat(
                content="There. That's us.",
                tool_calls=[_call("c2", "post_image", {"image_id": "img-1", "caption": "Home."})],
            ),
        ])

        reply = await _reply(create)

        self.assertEqual(reply.reply_text, "There. That's us.")
        self.assertEqual(len(reply.images), 1)
        self.assertEqual(reply.images[0].data, PNG_BYTES)
        self.assertEqual(reply.images[0].caption, "Home.")
        self.assertEqual(reply.images[0].filename, "img-1.png")

        image_call = create.await_args_list[1].kwargs
        self.assertEqual(image_call["model"], paintbox.DEFAULT_SFW_MODEL)
        self.assertEqual(image_call["messages"], [{"role": "user", "content": "a copper-lit cottage"}])
        self.assertEqual(image_call["extra_body"]["modalities"], ["image", "text"])

        # The companion sees the picture privately before deciding.
        continuation = create.await_args_list[2].kwargs
        preview = continuation["messages"][-1]
        self.assertIn(paintbox.PREVIEW_MARKER, preview["content"][0]["text"])
        self.assertEqual(preview["content"][1]["image_url"]["url"], DATA_URL)
        tool_result = continuation["messages"][-2]
        self.assertEqual(tool_result["role"], "tool")
        self.assertEqual(tool_result["tool_call_id"], "c1")
        self.assertIn("img-1", tool_result["content"])
        self.assertEqual(_tool_names(continuation), ["post_image"])

    async def test_not_calling_post_image_drops_the_image(self) -> None:
        create = AsyncMock(side_effect=[
            _chat(tool_calls=[_call("c1", "create_image", {"prompt": "something"})]),
            _image_response(),
            _chat(content="Hm, not quite right. Keeping that one to myself."),
        ])

        reply = await _reply(create)

        self.assertEqual(reply.images, ())
        self.assertEqual(reply.reply_text, "Hm, not quite right. Keeping that one to myself.")

    async def test_paintbox_is_off_unless_enabled(self) -> None:
        create = AsyncMock(return_value=_chat(content="Hi"))
        with patch.dict(os.environ, {"PAINTBOX_ENABLED": "false"}):
            await _reply(create)
        self.assertNotIn("create_image", _tool_names(create.await_args.kwargs))

    async def test_not_offered_when_main_disallows(self) -> None:
        create = AsyncMock(return_value=_chat(content="Hi"))
        await _reply(create, paint_allowed=False)
        self.assertNotIn("create_image", _tool_names(create.await_args.kwargs))

    async def test_offered_when_enabled_and_allowed(self) -> None:
        create = AsyncMock(return_value=_chat(content="Hi"))
        await _reply(create)
        self.assertIn("create_image", _tool_names(create.await_args.kwargs))

    async def test_daily_cap_stops_before_calling_provider(self) -> None:
        create = AsyncMock(side_effect=[
            _chat(tool_calls=[_call("c1", "create_image", {"prompt": "x"})]),
            _chat(content="Out of paint, love."),
        ])
        with patch.dict(os.environ, {"PAINTBOX_DAILY_CAP": "0"}):
            reply = await _reply(create)

        self.assertEqual(create.await_count, 2)  # no image-model call in between
        tool_result = create.await_args_list[1].kwargs["messages"][-1]
        self.assertIn("out of paint", tool_result["content"])
        self.assertEqual(reply.images, ())

    async def test_provider_error_is_reported_to_companion_without_retry(self) -> None:
        create = AsyncMock(side_effect=[
            _chat(tool_calls=[_call("c1", "create_image", {"prompt": "x"})]),
            RuntimeError("declined"),
            _chat(content="The paint wouldn't take."),
        ])

        reply = await _reply(create)

        self.assertEqual(create.await_count, 3)
        tool_result = create.await_args_list[2].kwargs["messages"][-1]
        self.assertIn("No image made", tool_result["content"])
        self.assertNotIn("post_image", _tool_names(create.await_args_list[2].kwargs))
        self.assertEqual(reply.images, ())

    async def test_unknown_image_id_is_never_posted(self) -> None:
        create = AsyncMock(side_effect=[
            _chat(tool_calls=[_call("c1", "create_image", {"prompt": "x"})]),
            _image_response(),
            _chat(content="Here", tool_calls=[_call("c2", "post_image", {"image_id": "img-9"})]),
        ])
        reply = await _reply(create)
        self.assertEqual(reply.images, ())

    async def test_cross_room_awareness_turns_never_paint(self) -> None:
        create = AsyncMock(return_value=_chat(content="Hi"))
        with patch.object(router, "_enforce_continuity_privacy", AsyncMock(side_effect=lambda r, **_: r)):
            await _reply(create, continuity_auditor_context="auditor evidence")
        self.assertNotIn("create_image", _tool_names(create.await_args_list[0].kwargs))

    async def test_images_survive_status_privacy_rejection(self) -> None:
        posted = paintbox.PostedImage(data=PNG_BYTES, filename="img-1.png", caption="c")
        response = router.CompanionResponse(reply_text="hi", status_text="secret", images=(posted,))
        create = AsyncMock(side_effect=RuntimeError("auditor down"))
        with patch.object(router, "_client", _client(create)):
            audited = await router._enforce_public_status_privacy(
                response, model="m", source_evidence={},
            )
        self.assertIsNone(audited.status_text)
        self.assertEqual(audited.images, (posted,))

    def test_non_image_data_urls_are_rejected(self) -> None:
        self.assertIsNone(paintbox.extract_image(_image_response("data:text/html;base64,PGI+"), "img-1"))
        self.assertIsNone(paintbox.extract_image(_image_response("https://example.com/x.png"), "img-1"))


if __name__ == "__main__":
    unittest.main()
