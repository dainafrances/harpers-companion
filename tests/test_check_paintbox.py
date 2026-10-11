import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from scripts import check_paintbox as check


class PaintboxCheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.marker = Path(temporary.name) / "probe.done"
        self.usage = Path(temporary.name) / "usage.sqlite"
        environment = patch.dict(os.environ, {
            "PAINTBOX_ENABLED": "true", "PAINTBOX_SFW_MODEL": check.CHECK_MODEL,
            "PAINTBOX_DAILY_CAP": "30", "PAINTBOX_USAGE_PATH": str(self.usage),
            "PAINTBOX_CHECK_MARKER": str(self.marker), "OPENROUTER_API_KEY": "invented-secret-key",
        })
        environment.start()
        self.addCleanup(environment.stop)

    async def test_one_fixed_request_and_marker_prevent_repeat(self):
        images = {"img-1": SimpleNamespace(data=b"invented-jpeg", mime_type="image/jpeg")}

        async def create(client, calls, *, outcomes):
            self.assertEqual(client.api_key, "invented-secret-key")
            self.assertTrue(self.marker.exists())
            self.assertEqual(len(calls), 1)
            self.assertEqual(json.loads(calls[0].function.arguments)["prompt"], check.CHECK_PROMPT)
            outcomes[calls[0].id] = {"status": "generated", "attempted": True}
            return {}, images

        output = io.StringIO()
        with patch.object(check.paintbox, "run_create_calls", side_effect=create) as run, redirect_stdout(output):
            first = await check.check_once()
            second = await check.check_once()
        self.assertEqual(first["status"], "generated")
        self.assertEqual(first["mime_type"], "image/jpeg")
        self.assertEqual(first["byte_count"], len(b"invented-jpeg"))
        self.assertEqual(second["status"], "already_checked")
        run.assert_awaited_once()
        self.assertEqual(images, {})
        self.assertNotIn("invented-secret-key", output.getvalue())
        self.assertNotIn(check.CHECK_PROMPT, output.getvalue())
        self.assertNotIn("invented-jpeg", output.getvalue())

    async def test_disabled_or_invalid_configuration_never_claims_or_calls(self):
        for changes, expected in (
            ({"PAINTBOX_ENABLED": "false"}, "check_disabled"),
            ({"PAINTBOX_SFW_MODEL": "unapproved-model"}, "configuration_error"),
            ({"OPENROUTER_API_KEY": ""}, "configuration_error"),
            ({"PAINTBOX_USAGE_PATH": "relative.sqlite"}, "configuration_error"),
            ({"PAINTBOX_CHECK_MARKER": str(self.usage)}, "configuration_error"),
        ):
            with self.subTest(changes=changes), patch.dict(os.environ, changes):
                with patch.object(check.paintbox, "run_create_calls", new_callable=AsyncMock) as run, patch("builtins.print"):
                    result = await check.check_once()
                self.assertEqual(result["status"], expected)
                run.assert_not_awaited()
                self.assertFalse(self.marker.exists())

    async def test_actual_zero_daily_cap_stops_before_http(self):
        with patch.dict(os.environ, {"PAINTBOX_DAILY_CAP": "0"}):
            with patch.object(check.paintbox.aiohttp, "ClientSession") as session, patch("builtins.print"):
                result = await check.check_once()
        self.assertEqual(result["status"], "cap_reached")
        session.assert_not_called()

    async def test_timeout_or_exception_never_retries_or_leaks(self):
        for error, expected in ((TimeoutError("invented-secret-key"), "timeout"), (RuntimeError(check.CHECK_PROMPT), "check_error")):
            with self.subTest(expected=expected):
                output = io.StringIO()
                with patch.object(check.paintbox, "run_create_calls", new=AsyncMock(side_effect=error)) as run, redirect_stdout(output):
                    first = await check.check_once()
                    second = await check.check_once()
                self.assertEqual(first["status"], expected)
                self.assertEqual(second["status"], "already_checked")
                run.assert_awaited_once()
                self.assertNotIn("invented-secret-key", output.getvalue())
                self.assertNotIn(check.CHECK_PROMPT, output.getvalue())
                self.marker.unlink()

    async def test_probe_deadline_tracks_http_deadline(self):
        async def create(client, calls, *, outcomes):
            outcomes[calls[0].id] = {"status": "no_image", "attempted": True}
            return {}, {}

        for configured, expected in (("240", 250), ("999", 310), ("invalid", 190)):
            async def wait_for(awaitable, *, timeout):
                self.assertEqual(timeout, expected)
                return await awaitable

            with patch.dict(os.environ, {"PAINTBOX_GENERATION_TIMEOUT_SECONDS": configured}):
                with patch.object(check.paintbox, "run_create_calls", side_effect=create), patch.object(check.asyncio, "wait_for", side_effect=wait_for), patch("builtins.print"):
                    result = await check.check_once()
            self.assertEqual(result["status"], "no_image")
            self.marker.unlink()

    async def test_launch_runs_bot_concurrently_and_initializes_normal_memory(self):
        probe_entered, release_probe = asyncio.Event(), asyncio.Event()

        async def probe(api_key):
            self.assertEqual(api_key, "invented-secret-key")
            probe_entered.set()
            await release_probe.wait()
            return check._result("provider_error")

        async def start(token):
            self.assertEqual(token, "invented-discord-token")
            await asyncio.wait_for(probe_entered.wait(), timeout=1)
            self.assertFalse(release_probe.is_set())
            release_probe.set()
            await asyncio.sleep(0)

        bot = AsyncMock()
        bot.start.side_effect = start
        app = SimpleNamespace(bot=bot, DISCORD_TOKEN="invented-discord-token", memory=SimpleNamespace(init_db=Mock()))
        with patch.object(check, "check_once", side_effect=probe) as run:
            self.assertEqual(await check.start_bot_with_check(app), 0)
        app.memory.init_db.assert_called_once_with()
        bot.start.assert_awaited_once()
        run.assert_awaited_once()

    def test_summary_only_accepts_fixed_status_numeric_codes_and_raster_metadata(self):
        result = check._result("private-provider-body", outcome={"http_status": "401:secret", "error_code": True}, image=SimpleNamespace(mime_type="image/svg+xml", data=b"private-data"))
        self.assertEqual(result, {"status": "check_error", "http_status": None, "error_code": None, "mime_type": None, "byte_count": 0})


if __name__ == "__main__":
    unittest.main()
