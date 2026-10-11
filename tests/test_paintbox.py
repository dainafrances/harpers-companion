from __future__ import annotations

import asyncio
import base64
import importlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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


def _image_session(status, payload=None, json_error=None):
    response = SimpleNamespace(status=status, json=AsyncMock(return_value=payload, side_effect=json_error))
    request_context = AsyncMock()
    request_context.__aenter__.return_value = response
    session = SimpleNamespace(post=Mock(return_value=request_context))
    session_context = AsyncMock()
    session_context.__aenter__.return_value = session
    return session_context, session


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
        patch.object(router, "_audit_paintbox_action", AsyncMock(return_value=True)),
    ):
        return await router.generate_companion_reply(**kwargs)


def _tool_names(call_kwargs) -> list[str]:
    return [
        tool.get("function", {}).get("name")
        for tool in call_kwargs.get("tools") or []
        if tool.get("type") == "function"
    ]


@patch.dict(os.environ, {"PAINTBOX_ENABLED": "true", "ENABLE_WEB_SEARCH": "false"})
class PaintboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(os.environ)
        self.environment.start()
        os.environ.pop("PAINTBOX_USAGE_PATH", None)
        os.environ.pop("PAINTBOX_SFW_MODEL", None)
        self.addCleanup(self.environment.stop)
        paintbox.reset_cap_for_tests()

    async def test_companion_paints_looks_and_posts_with_own_caption(self) -> None:
        create = AsyncMock(side_effect=[
            _chat(content="There. That's us."),
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

        self.assertNotIn("create_image", _tool_names(create.await_args_list[0].kwargs))
        self.assertEqual(_tool_names(create.await_args_list[1].kwargs), ["create_image"])
        image_call = create.await_args_list[2].kwargs
        self.assertEqual(image_call["model"], paintbox.DEFAULT_SFW_MODEL)
        self.assertEqual(image_call["messages"], [{"role": "user", "content": "a copper-lit cottage"}])
        self.assertEqual(image_call["extra_body"]["modalities"], ["image", "text"])

        # The companion sees the picture privately before deciding.
        continuation = create.await_args_list[3].kwargs
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
            _chat(content="Hm, not quite right. Keeping that one to myself."),
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
        self.assertNotIn("create_image", _tool_names(create.await_args_list[0].kwargs))
        self.assertIn("create_image", _tool_names(create.await_args_list[1].kwargs))

    async def test_daily_cap_stops_before_calling_provider(self) -> None:
        create = AsyncMock(side_effect=[
            _chat(content="A lighthouse would suit this."),
            _chat(tool_calls=[_call("c1", "create_image", {"prompt": "x"})]),
        ])
        with patch.dict(os.environ, {"PAINTBOX_DAILY_CAP": "0"}):
            reply = await _reply(create)

        self.assertEqual(create.await_count, 2)  # no image-model call in between
        self.assertIn("A lighthouse would suit this.", reply.reply_text)
        self.assertIn("No image was created.", reply.reply_text)
        self.assertEqual(reply.images, ())

    async def test_provider_error_is_reported_to_companion_without_retry(self) -> None:
        create = AsyncMock(side_effect=[
            _chat(content="The paint wouldn't take."),
            _chat(tool_calls=[_call("c1", "create_image", {"prompt": "x"})]),
            RuntimeError("declined"),
        ])

        reply = await _reply(create)

        self.assertEqual(create.await_count, 3)
        self.assertIn("The paint wouldn't take.", reply.reply_text)
        self.assertIn("No image was created.", reply.reply_text)
        self.assertNotIn("post_image", _tool_names(create.await_args_list[2].kwargs))
        self.assertEqual(reply.images, ())

    async def test_unknown_image_id_is_never_posted(self) -> None:
        create = AsyncMock(side_effect=[
            _chat(content="An invented landscape."),
            _chat(tool_calls=[_call("c1", "create_image", {"prompt": "x"})]),
            _image_response(),
            _chat(content="Here", tool_calls=[_call("c2", "post_image", {"image_id": "img-9"})]),
        ])
        reply = await _reply(create)
        self.assertEqual(reply.images, ())

    async def test_cross_room_awareness_keeps_main_writer_separate_from_image_selection(self) -> None:
        create = AsyncMock(return_value=_chat(content="Hi"))
        with patch.object(router, "_enforce_continuity_privacy", AsyncMock(side_effect=lambda r, **_: r)):
            await _reply(create, continuity_auditor_context="auditor evidence")
        self.assertNotIn("create_image", _tool_names(create.await_args_list[0].kwargs))
        self.assertIn("create_image", _tool_names(create.await_args_list[1].kwargs))
        selector_messages = create.await_args_list[1].kwargs["messages"]
        self.assertNotIn("auditor evidence", json.dumps(selector_messages))

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


class PaintboxBackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ)
        self.environment.start()
        for name in ("PAINTBOX_USAGE_PATH", "PAINTBOX_SFW_MODEL", "PAINTBOX_DAILY_CAP",
                     "PAINTBOX_GENERATION_TIMEOUT_SECONDS"):
            os.environ.pop(name, None)
        self.addCleanup(self.environment.stop)
        paintbox.reset_cap_for_tests()

    def test_unconfigured_model_and_cap_are_preserved(self):
        self.assertEqual(paintbox.sfw_model(), "sourceful/riverflow-v2.5-fast:free")
        self.assertEqual(paintbox.daily_cap(), 20)
        self.assertEqual(paintbox.images_left_today(), 20)

    def test_generation_deadline_defaults_invalid_values_and_bounds(self):
        self.assertEqual(paintbox.generation_timeout_seconds(), 180)
        for value in ("", "invalid", "nan", "NaN", "inf", "-inf", "Infinity", "1e309"):
            with self.subTest(value=value), patch.dict(os.environ, {"PAINTBOX_GENERATION_TIMEOUT_SECONDS": value}):
                self.assertEqual(paintbox.generation_timeout_seconds(), 180)
        for value, expected in (("0", 60), ("59.9", 60), ("60", 60), ("240.5", 240.5),
                                (" 250 ", 250), ("300", 300), ("999", 300)):
            with self.subTest(value=value), patch.dict(os.environ, {"PAINTBOX_GENERATION_TIMEOUT_SECONDS": value}):
                self.assertEqual(paintbox.generation_timeout_seconds(), expected)

    async def test_paid_riverflow_uses_one_dedicated_request_and_client_key(self):
        create = AsyncMock()
        client = _client(create)
        client.api_key = "invented-client-key"
        payload = {"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode()}]}
        context, session = _image_session(200, payload)
        with (
            patch.dict(os.environ, {"PAINTBOX_SFW_MODEL": "sourceful/riverflow-v2.5-fast",
                                   "PAINTBOX_GENERATION_TIMEOUT_SECONDS": "240"}),
            patch.object(paintbox.aiohttp, "ClientSession", return_value=context) as factory,
        ):
            actual = await paintbox.request_image(client, "an invented lighthouse", "3:2")
        self.assertIs(actual, payload)
        create.assert_not_awaited()
        session.post.assert_called_once()
        self.assertEqual(session.post.call_args.args, ("https://openrouter.ai/api/v1/images",))
        self.assertEqual(session.post.call_args.kwargs["json"], {
            "model": "sourceful/riverflow-v2.5-fast", "prompt": "an invented lighthouse",
            "resolution": "1K", "aspect_ratio": "3:2", "n": 1, "output_format": "jpeg",
        })
        self.assertEqual(session.post.call_args.kwargs["headers"]["Authorization"], "Bearer invented-client-key")
        self.assertEqual(factory.call_args.kwargs["timeout"].total, 240)

    async def test_paid_riverflow_key_falls_back_to_environment(self):
        context, session = _image_session(200, {"data": []})
        with (
            patch.dict(os.environ, {"PAINTBOX_SFW_MODEL": "sourceful/riverflow-v2.5-fast",
                                   "OPENROUTER_API_KEY": "invented-environment-key"}),
            patch.object(paintbox.aiohttp, "ClientSession", return_value=context),
        ):
            await paintbox.request_image(SimpleNamespace(), "invented coast", "1:1")
        self.assertEqual(session.post.call_args.kwargs["headers"]["Authorization"], "Bearer invented-environment-key")

    async def test_paid_riverflow_missing_key_stops_before_http(self):
        with (
            patch.dict(os.environ, {"PAINTBOX_SFW_MODEL": "sourceful/riverflow-v2.5-fast", "OPENROUTER_API_KEY": ""}),
            patch.object(paintbox.aiohttp, "ClientSession") as session,
        ):
            with self.assertRaises(paintbox.ImageServiceError) as caught:
                await paintbox.request_image(SimpleNamespace(), "invented coast", "1:1")
        self.assertEqual(caught.exception.status, 401)
        session.assert_not_called()

    async def test_other_models_keep_legacy_sdk_shape_and_bounded_deadline(self):
        create = AsyncMock(return_value=_image_response())
        with patch.dict(os.environ, {"PAINTBOX_SFW_MODEL": "invented/native-image-model",
                                    "PAINTBOX_GENERATION_TIMEOUT_SECONDS": "999"}):
            await paintbox.request_image(_client(create), "invented scene", "2:3")
        self.assertEqual(create.await_args.kwargs, {
            "model": "invented/native-image-model", "messages": [{"role": "user", "content": "invented scene"}],
            "extra_body": {"modalities": ["image", "text"], "image_config": {"aspect_ratio": "2:3"}},
            "timeout": 300,
        })

    async def test_provider_http_failure_retains_only_safe_numeric_codes(self):
        private = "invented-private-prompt-echo"
        context, session = _image_session(404, {"error": {"code": 404, "message": private,
                                                        "metadata": {"raw": private}}})
        outcomes = {}
        with (
            patch.dict(os.environ, {"PAINTBOX_SFW_MODEL": "sourceful/riverflow-v2.5-fast"}),
            patch.object(paintbox.aiohttp, "ClientSession", return_value=context),
            patch("builtins.print") as logged,
        ):
            results, images = await paintbox.run_create_calls(SimpleNamespace(api_key="invented-key"), [
                _call("p1", "create_image", {"prompt": "invented lighthouse"}),
            ], outcomes=outcomes)
        self.assertEqual(outcomes, {"p1": {"status": "provider_error", "attempted": True,
                                          "http_status": 404, "error_code": 404}})
        self.assertEqual(images, {})
        self.assertIn("configured image model", results["p1"])
        self.assertNotIn(private, str(results) + str(outcomes) + str(logged.call_args_list))
        self.assertNotIn("invented lighthouse", str(logged.call_args_list))
        session.post.assert_called_once()
        self.assertEqual(paintbox.images_left_today(), 19)

    async def test_non_json_error_success_error_envelope_and_invalid_json_are_classified(self):
        cases = [
            (502, None, ValueError("invented private HTML"), "provider_unavailable", None),
            (200, {"error": {"code": 429, "message": "invented private echo"}}, None, "rate_limit", 429),
            (200, None, ValueError("invented private malformed JSON"), "invalid_response", None),
            (400, {"error": {"code": "invented private code"}}, None, "request_rejected", None),
        ]
        for status, payload, error, category, code in cases:
            context, _ = _image_session(status, payload, error)
            with (
                self.subTest(status=status, category=category),
                patch.dict(os.environ, {"PAINTBOX_SFW_MODEL": "sourceful/riverflow-v2.5-fast"}),
                patch.object(paintbox.aiohttp, "ClientSession", return_value=context),
            ):
                with self.assertRaises(paintbox.ImageServiceError) as caught:
                    await paintbox.request_image(SimpleNamespace(api_key="invented-key"), "invented coast", "1:1")
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.category, category)
                self.assertNotIn("private", str(caught.exception))

    async def test_legacy_sdk_status_error_is_sanitized(self):
        error = RuntimeError("invented private provider body")
        error.status_code = 402
        create = AsyncMock(side_effect=error)
        outcomes = {}
        with patch("builtins.print") as logged:
            results, _ = await paintbox.run_create_calls(_client(create), [
                _call("p1", "create_image", {"prompt": "invented scene"}),
            ], outcomes=outcomes)
        self.assertEqual(outcomes["p1"]["http_status"], 402)
        self.assertIn("credits", results["p1"])
        self.assertNotIn("private provider body", str(results) + str(logged.call_args_list))
        create.assert_awaited_once()

    async def test_malformed_responses_become_plain_no_image_results(self):
        for response in (None, [], "invented", {"choices": {}}, {"choices": [None]},
                         {"choices": [{"message": {"images": {"url": DATA_URL}}}]},
                         {"data": [None, {"b64_json": []}, {"media_type": 1, "b64_json": "abc"}]}):
            with self.subTest(response=response):
                create = AsyncMock(return_value=response)
                outcomes = {}
                results, images = await paintbox.run_create_calls(_client(create), [
                    _call("p1", "create_image", {"prompt": "invented lighthouse"}),
                ], outcomes=outcomes)
                self.assertIn("without a usable picture", results["p1"])
                self.assertEqual(images, {})
                self.assertEqual(outcomes, {"p1": {"status": "no_image", "attempted": True}})
                create.assert_awaited_once()

    async def test_unexpected_extraction_failure_is_a_plain_failure(self):
        with patch.object(paintbox, "extract_image", side_effect=ValueError("invented private parsing text")):
            results, images = await paintbox.run_create_calls(_client(AsyncMock(return_value=_image_response())), [
                _call("p1", "create_image", {"prompt": "invented scene"}),
            ])
        self.assertEqual(images, {})
        self.assertIn("No image made", results["p1"])
        self.assertNotIn("private parsing text", results["p1"])

    def test_dedicated_image_api_infers_jpeg_and_skips_malformed_items(self):
        jpeg = b"\xff\xd8\xffinvented-jpeg-bytes"
        response = {"data": [None, {}, {"b64_json": base64.b64encode(jpeg).decode()}]}
        image = paintbox.extract_image(response, "img-1")
        self.assertEqual(image.data, jpeg)
        self.assertEqual(image.mime_type, "image/jpeg")
        self.assertEqual(image.filename, "img-1.jpg")
        self.assertTrue(image.data_url.startswith("data:image/jpeg;base64,"))

    def test_legacy_sdk_model_extra_images_still_work(self):
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(model_extra={
            "images": [{"image_url": {"url": DATA_URL}}],
        }))])
        self.assertEqual(paintbox.extract_image(response, "img-1").data, PNG_BYTES)

    def test_invalid_base64_active_files_and_mime_mismatch_are_rejected(self):
        for url in ("data:image/png;base64,invalid!", "data:image/png;base64,",
                    "data:image/svg+xml;base64," + base64.b64encode(b"<svg/>").decode(),
                    "data:image/jpeg;base64," + base64.b64encode(PNG_BYTES).decode(),
                    "data:image/png;base64," + base64.b64encode(b"not a raster").decode()):
            with self.subTest(url=url):
                self.assertIsNone(paintbox.extract_image(_image_response(url), "img-1"))

    def test_encoded_limit_is_checked_before_base64_decode(self):
        with patch.object(paintbox, "MAX_ENCODED_IMAGE_CHARS", 16), patch.object(paintbox.base64, "b64decode") as decode:
            self.assertIsNone(paintbox.extract_image({"data": [{"b64_json": "a" * 17}]}, "img-1"))
        decode.assert_not_called()

    def test_decoded_byte_limit_is_checked(self):
        with patch.object(paintbox, "MAX_IMAGE_BYTES", len(PNG_BYTES) - 1):
            self.assertIsNone(paintbox.extract_image(_image_response(), "img-1"))

    async def test_outcomes_cover_invalid_prompt_limit_and_cap_without_provider_attempt(self):
        outcomes = {}
        create = AsyncMock()
        with patch.dict(os.environ, {"PAINTBOX_DAILY_CAP": "0"}):
            results, images = await paintbox.run_create_calls(_client(create), [
                _call("invalid", "create_image", {"prompt": " "}),
                _call("capped", "create_image", {"prompt": "invented scene"}),
                _call("limited", "create_image", {"prompt": "invented scene"}),
            ], outcomes=outcomes)
        self.assertEqual(outcomes, {
            "invalid": {"status": "invalid_prompt", "attempted": False},
            "capped": {"status": "cap_reached", "attempted": False},
            "limited": {"status": "limit_reached", "attempted": False},
        })
        self.assertEqual(images, {})
        self.assertEqual(len(results), 3)
        create.assert_not_awaited()

    async def test_generated_outcome_and_aspect_normalization(self):
        create = AsyncMock(return_value=_image_response())
        outcomes = {"p1": {"http_status": 404, "error_code": 404}}
        results, images = await paintbox.run_create_calls(_client(create), [
            _call("p1", "create_image", {"prompt": " invented scene ", "aspect": {"malformed": True}}),
        ], outcomes=outcomes)
        self.assertEqual(outcomes, {"p1": {"status": "generated", "attempted": True}})
        self.assertIn("img-1", results["p1"])
        self.assertEqual(images["img-1"].data, PNG_BYTES)
        self.assertEqual(create.await_args.kwargs["extra_body"]["image_config"]["aspect_ratio"], "1:1")

    async def test_persistent_cap_survives_memory_reset_and_counts_provider_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary) / "usage.sqlite")
            outcomes = {}
            with patch.dict(os.environ, {"PAINTBOX_USAGE_PATH": path, "PAINTBOX_DAILY_CAP": "1"}):
                failing = AsyncMock(side_effect=RuntimeError("invented private failure"))
                await paintbox.run_create_calls(_client(failing), [
                    _call("p1", "create_image", {"prompt": "invented scene"}),
                ], outcomes=outcomes)
                paintbox.reset_cap_for_tests()
                self.assertEqual(paintbox.images_left_today(), 0)
                succeeding = AsyncMock(return_value=_image_response())
                await paintbox.run_create_calls(_client(succeeding), [
                    _call("p2", "create_image", {"prompt": "invented scene"}),
                ], outcomes=outcomes)
                succeeding.assert_not_awaited()
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT usage_date, request_count FROM image_usage").fetchall(), [(paintbox._today(), 1)])
                self.assertEqual([row[1] for row in db.execute("PRAGMA table_info(image_usage)")], ["usage_date", "request_count"])
            self.assertTrue(outcomes["p1"]["attempted"])
            self.assertEqual(outcomes["p2"], {"status": "cap_reached", "attempted": False})

    async def test_persistent_cap_reservations_are_atomic_for_concurrent_requests(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {
            "PAINTBOX_USAGE_PATH": str(Path(temporary) / "usage.sqlite"), "PAINTBOX_DAILY_CAP": "3",
        }):
            create = AsyncMock(return_value=_image_response())
            replies = await asyncio.gather(*[
                paintbox.run_create_calls(_client(create), [
                    _call(f"p{index}", "create_image", {"prompt": "invented scene"}),
                ]) for index in range(8)
            ])
            self.assertEqual(create.await_count, 3)
            self.assertEqual(sum(bool(images) for _, images in replies), 3)
            self.assertEqual(paintbox.images_left_today(), 0)

    async def test_persistent_cap_rolls_at_utc_day_boundary(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {
            "PAINTBOX_USAGE_PATH": str(Path(temporary) / "usage.sqlite"), "PAINTBOX_DAILY_CAP": "1",
        }):
            with patch.object(paintbox, "_today", return_value="2026-10-10"):
                self.assertTrue(await paintbox._reserve_image_attempt())
                self.assertEqual(paintbox.images_left_today(), 0)
            with patch.object(paintbox, "_today", return_value="2026-10-11"):
                self.assertEqual(paintbox.images_left_today(), 1)
                self.assertTrue(await paintbox._reserve_image_attempt())

    async def test_unusable_usage_configuration_fails_closed_before_provider(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = ["", "relative.sqlite", temporary, str(Path(temporary) / "absent" / "usage.sqlite")]
            for path in paths:
                create = AsyncMock()
                outcomes = {}
                with self.subTest(path=path), patch.dict(os.environ, {"PAINTBOX_USAGE_PATH": path}):
                    results, images = await paintbox.run_create_calls(_client(create), [
                        _call("p1", "create_image", {"prompt": "invented scene"}),
                    ], outcomes=outcomes)
                self.assertEqual(outcomes, {"p1": {"status": "usage_error", "attempted": False}})
                self.assertIn("could not be safely checked", results["p1"])
                if path:
                    self.assertNotIn(path, results["p1"])
                self.assertEqual(images, {})
                create.assert_not_awaited()

    async def test_usage_ledger_refuses_other_database_tables_without_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary) / "other.sqlite")
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE unrelated (value TEXT)")
                db.execute("INSERT INTO unrelated VALUES ('invented private source')")
            create = AsyncMock()
            with patch.dict(os.environ, {"PAINTBOX_USAGE_PATH": path}):
                results, _ = await paintbox.run_create_calls(_client(create), [
                    _call("p1", "create_image", {"prompt": "invented scene"}),
                ])
            create.assert_not_awaited()
            self.assertIn("could not be safely checked", results["p1"])
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("unrelated",)])
                self.assertEqual(db.execute("SELECT value FROM unrelated").fetchall(), [("invented private source",)])


if __name__ == "__main__":
    unittest.main()
