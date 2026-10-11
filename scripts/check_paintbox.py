"""One fixed SFW provider check, sharing the daily cap and never posting an image.

An empty persistent marker is claimed before the request, preventing a paid
check from repeating after restarts. --start-bot is a temporary launch command
that starts the normal Discord bot while the independent check runs.
"""

import argparse
import asyncio
import importlib
import json
import os
import sys
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import paintbox

CHECK_PROMPT = "A watercolor painting of a lighthouse on a quiet coast at dusk, no people."
CHECK_MODEL = "sourceful/riverflow-v2.5-fast"
CHECK_TIMEOUT_GRACE_SECONDS = 10
_STATUSES = {
    "generated", "provider_error", "no_image", "cap_reached", "invalid_prompt",
    "limit_reached", "usage_error", "check_disabled", "configuration_error",
    "already_checked", "marker_error", "check_error", "timeout", "bot_start_failed",
}


def _result(status, *, outcome=None, image=None):
    result = {
        "status": status if status in _STATUSES else "check_error",
        "http_status": None, "error_code": None, "mime_type": None, "byte_count": 0,
    }
    for name in ("http_status", "error_code"):
        value = (outcome or {}).get(name)
        if type(value) is int and 100 <= value <= 599:
            result[name] = value
    if image is not None and image.mime_type in paintbox.RASTER_MIME_TYPES:
        result["mime_type"] = image.mime_type
        result["byte_count"] = len(image.data)
    return result


def _emit(result):
    print("[PAINTBOX CHECK] " + json.dumps(result, sort_keys=True), flush=True)


def _claim_marker(marker_path):
    try:
        descriptor = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
    except FileExistsError:
        return "already_checked"
    except (OSError, ValueError):
        return "marker_error"
    return None


async def check_once(api_key=None):
    if not paintbox.paintbox_enabled():
        result = _result("check_disabled")
        _emit(result)
        return result
    api_key = os.getenv("OPENROUTER_API_KEY", "") if api_key is None else api_key
    marker_path = os.getenv("PAINTBOX_CHECK_MARKER", "").strip()
    usage_path = os.getenv("PAINTBOX_USAGE_PATH", "").strip()
    if (
        not isinstance(api_key, str) or not api_key.strip()
        or paintbox.sfw_model() != CHECK_MODEL
        or not os.path.isabs(marker_path) or not os.path.isabs(usage_path)
        or os.path.realpath(marker_path) == os.path.realpath(usage_path)
    ):
        result = _result("configuration_error")
        _emit(result)
        return result
    marker_status = _claim_marker(marker_path)
    if marker_status:
        result = _result(marker_status)
        _emit(result)
        return result

    call = SimpleNamespace(
        id="paintbox-check",
        function=SimpleNamespace(
            name=paintbox.CREATE_TOOL_NAME,
            arguments=json.dumps({"prompt": CHECK_PROMPT, "aspect": "square"}),
        ),
    )
    images, outcomes = {}, {}
    try:
        _, images = await asyncio.wait_for(
            paintbox.run_create_calls(SimpleNamespace(api_key=api_key), [call], outcomes=outcomes),
            timeout=paintbox.generation_timeout_seconds() + CHECK_TIMEOUT_GRACE_SECONDS,
        )
        outcome = outcomes.get(call.id, {})
        image = next(iter(images.values()), None) if outcome.get("status") == "generated" else None
        result = _result(outcome.get("status"), outcome=outcome, image=image)
    except TimeoutError:
        result = _result("timeout")
    except Exception:
        result = _result("check_error")
    finally:
        images.clear()
    _emit(result)
    return result


async def start_bot_with_check(bot_module=None):
    try:
        app = bot_module if bot_module is not None else importlib.import_module("src.main")
        api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if not app.DISCORD_TOKEN or not api_key:
            _emit(_result("configuration_error"))
            return 2
        app.memory.init_db()
        async with app.bot:
            check_task = asyncio.create_task(check_once(api_key))
            try:
                await app.bot.start(app.DISCORD_TOKEN)
            finally:
                if not check_task.done():
                    check_task.cancel()
                with suppress(asyncio.CancelledError):
                    await check_task
    except Exception:
        _emit(_result("bot_start_failed"))
        return 2
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-bot", action="store_true")
    args = parser.parse_args(argv)
    if args.start_bot:
        return asyncio.run(start_bot_with_check())
    result = asyncio.run(check_once())
    return 0 if result["status"] in {"generated", "already_checked"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
