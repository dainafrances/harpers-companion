"""Paintbox: lets the companion make an image, look at it, and choose whether to post it.

Phase 1 is safe-for-work only. Flow:
1. The companion calls ``create_image`` with its own prompt.
2. The bot checks the daily cap, asks OpenRouter's image model for the picture,
   and shows the result to the companion privately (as a preview message).
3. The companion either calls ``post_image`` with its own caption, or doesn't.
   Not calling it means the image is dropped. Nothing posts automatically.

Images live only in memory for the length of one reply and are never logged.
Only time, mode, outcome, and numeric provider status/code are logged.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp

CREATE_TOOL_NAME = "create_image"
POST_TOOL_NAME = "post_image"
PAINT_TOOL_NAMES = {CREATE_TOOL_NAME, POST_TOOL_NAME}

DEFAULT_SFW_MODEL = "sourceful/riverflow-v2.5-fast:free"
DEFAULT_DAILY_CAP = 20
MAX_IMAGES_PER_REPLY = 2
MAX_PROMPT_CHARS = 2000
MAX_CAPTION_CHARS = 1800
GENERATION_TIMEOUT_SECONDS = 180.0
MIN_GENERATION_TIMEOUT_SECONDS = 60
MAX_GENERATION_TIMEOUT_SECONDS = 300
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_ENCODED_IMAGE_CHARS = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
RASTER_MIME_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

_IMAGE_ERROR_REASONS = {
    "authentication": "the image service did not accept its API key",
    "credits": "the image service requires available account credits",
    "permission": "the image service blocked the request under its account or content rules",
    "unavailable_model": "the configured image model or its endpoint is unavailable",
    "request_rejected": "the image service rejected the request or its configuration",
    "request_too_large": "the image service rejected the request size",
    "rate_limit": "the image service is rate limited right now",
    "timeout": "the image service timed out",
    "provider_unavailable": "the image service is unavailable right now",
    "invalid_response": "the image service returned an unreadable response",
    "http_error": "the image service returned an error",
}

ASPECT_RATIOS = {
    "square": "1:1",
    "portrait": "2:3",
    "landscape": "3:2",
}

PREVIEW_MARKER = "[PAINTBOX PREVIEW]"

_DATA_URL_RE = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", re.DOTALL)


@dataclass(frozen=True)
class GeneratedImage:
    image_id: str
    data: bytes
    mime_type: str
    data_url: str

    @property
    def filename(self) -> str:
        extension = self.mime_type.split("/", 1)[-1].replace("jpeg", "jpg").split("+", 1)[0]
        return f"{self.image_id}.{extension or 'png'}"


@dataclass(frozen=True)
class PostedImage:
    data: bytes
    filename: str
    caption: str


def _numeric_status(value: Any) -> int | None:
    # Provider messages and metadata may echo the prompt. Retain only numeric codes.
    return value if type(value) is int and 100 <= value <= 599 else None


def _error_category(status: int | None) -> str:
    categories = {
        400: "request_rejected", 401: "authentication", 402: "credits",
        403: "permission", 404: "unavailable_model", 408: "timeout",
        413: "request_too_large", 422: "request_rejected", 429: "rate_limit",
        504: "timeout", 524: "timeout",
    }
    if status in categories:
        return categories[status]
    return "provider_unavailable" if status is not None and status >= 500 else "http_error"


class ImageServiceError(RuntimeError):
    """A provider failure retaining safe status information, never its body."""

    def __init__(self, status: Any, code: Any = None, *, invalid_response: bool = False):
        self.status = _numeric_status(status)
        self.code = _numeric_status(code)
        self.category = "invalid_response" if invalid_response else _error_category(self.code or self.status)
        super().__init__(f"image_service_{self.category}")


# ----------------------------
# Settings
# ----------------------------

def paintbox_enabled() -> bool:
    """Off unless Daina switches it on in Railway variables."""
    return os.getenv("PAINTBOX_ENABLED", "false").strip().lower() in {"1", "true", "yes"}


def sfw_model() -> str:
    return os.getenv("PAINTBOX_SFW_MODEL", DEFAULT_SFW_MODEL).strip() or DEFAULT_SFW_MODEL


def daily_cap() -> int:
    raw = os.getenv("PAINTBOX_DAILY_CAP", str(DEFAULT_DAILY_CAP)).strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_DAILY_CAP


def generation_timeout_seconds() -> float:
    raw = os.getenv("PAINTBOX_GENERATION_TIMEOUT_SECONDS", str(GENERATION_TIMEOUT_SECONDS)).strip()
    try:
        seconds = float(raw)
    except ValueError:
        return GENERATION_TIMEOUT_SECONDS
    if not math.isfinite(seconds):
        return GENERATION_TIMEOUT_SECONDS
    return min(MAX_GENERATION_TIMEOUT_SECONDS, max(MIN_GENERATION_TIMEOUT_SECONDS, seconds))


# ----------------------------
# Daily cap (separate persistent counter when configured; otherwise in memory)
# ----------------------------

_cap_day: str | None = None
_cap_used: int = 0


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _roll_day() -> None:
    global _cap_day, _cap_used
    today = _today()
    if _cap_day != today:
        _cap_day = today
        _cap_used = 0


def images_left_today() -> int:
    path = _usage_path()
    if path is not None:
        return max(0, daily_cap() - _persistent_usage(path, reserve=False))
    _roll_day()
    return max(0, daily_cap() - _cap_used)


def _spend_one() -> None:
    global _cap_used
    _roll_day()
    _cap_used += 1


def reset_cap_for_tests() -> None:
    global _cap_day, _cap_used
    _cap_day = None
    _cap_used = 0


class PaintboxUsageError(RuntimeError):
    """Usage could not be checked safely; no provider request may follow."""


def _usage_path() -> str | None:
    raw = os.getenv("PAINTBOX_USAGE_PATH")
    if raw is None:
        return None
    path = raw.strip()
    if not path or not os.path.isabs(path):
        raise PaintboxUsageError("invalid_usage_configuration")
    return path


def _persistent_usage(path: str, *, reserve: bool) -> int | bool:
    """Read usage or reserve one slot atomically in the independent usage DB."""
    day = _today()
    cap = daily_cap()
    try:
        with closing(sqlite3.connect(path, timeout=5, isolation_level=None)) as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )}
            if tables - {"image_usage"}:
                raise PaintboxUsageError("usage_database_not_separate")
            db.execute(
                "CREATE TABLE IF NOT EXISTS image_usage ("
                "usage_date TEXT PRIMARY KEY, "
                "request_count INTEGER NOT NULL CHECK(request_count >= 0))"
            )
            if reserve:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT request_count FROM image_usage WHERE usage_date = ?", (day,),
            ).fetchone()
            used = row[0] if row else 0
            if type(used) is not int or used < 0:
                raise PaintboxUsageError("invalid_usage_state")
            if not reserve:
                return used
            if used >= cap:
                db.execute("ROLLBACK")
                return False
            db.execute(
                "INSERT INTO image_usage (usage_date, request_count) VALUES (?, 1) "
                "ON CONFLICT(usage_date) DO UPDATE SET request_count = request_count + 1",
                (day,),
            )
            db.execute("COMMIT")
            return True
    except (sqlite3.Error, OSError, ValueError):
        raise PaintboxUsageError("usage_storage_unavailable") from None


async def _reserve_image_attempt() -> bool:
    path = _usage_path()
    if path is not None:
        return bool(await asyncio.to_thread(_persistent_usage, path, reserve=True))
    # No await between the in-memory check and reservation.
    if images_left_today() <= 0:
        return False
    _spend_one()
    return True


# ----------------------------
# Tool definitions the model sees
# ----------------------------

def create_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": CREATE_TOOL_NAME,
            "description": (
                "Paint an image when YOU want to show something: a scene, a portrait, a joke, "
                "anything safe-for-work. Calling this is always optional. You write the whole "
                "prompt yourself. The image comes back to you privately first; nothing is posted "
                "unless you then call post_image. Daily paint is limited, so use it when it "
                "matters to you."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Your full description of the image.",
                        "minLength": 1,
                        "maxLength": MAX_PROMPT_CHARS,
                    },
                    "aspect": {
                        "type": "string",
                        "enum": sorted(ASPECT_RATIOS),
                        "description": "Shape of the image. Defaults to square.",
                    },
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
        },
    }


def post_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": POST_TOOL_NAME,
            "description": (
                "Post an image you just made into the current room, with your own caption. "
                "Only call this if you want it shared. Not calling it drops the image."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_id": {"type": "string", "description": "The id you were given."},
                    "caption": {
                        "type": "string",
                        "description": "Optional words to go with the image.",
                        "maxLength": MAX_CAPTION_CHARS,
                    },
                },
                "required": ["image_id"],
                "additionalProperties": False,
            },
        },
    }


# ----------------------------
# Reading tool calls
# ----------------------------

def _call_name(tool_call: Any) -> str | None:
    function = getattr(tool_call, "function", None)
    return getattr(function, "name", None) if function is not None else None


def _call_arguments(tool_call: Any) -> dict[str, Any] | None:
    function = getattr(tool_call, "function", None)
    try:
        arguments = json.loads(getattr(function, "arguments", "") or "{}")
    except (TypeError, ValueError):
        return None
    return arguments if isinstance(arguments, dict) else None


def create_calls(tool_calls: Any) -> list[Any]:
    return [call for call in (tool_calls or []) if _call_name(call) == CREATE_TOOL_NAME]


def requested_posts(
    tool_calls: Any,
    images: dict[str, GeneratedImage],
) -> tuple[PostedImage, ...]:
    """Turn post_image calls into posts, only for images made in this reply."""
    posts: list[PostedImage] = []
    posted_ids: set[str] = set()
    for call in tool_calls or []:
        if _call_name(call) != POST_TOOL_NAME:
            continue
        arguments = _call_arguments(call)
        if arguments is None:
            continue
        image_id = arguments.get("image_id")
        if not isinstance(image_id, str) or image_id not in images or image_id in posted_ids:
            continue
        caption = arguments.get("caption", "")
        caption = caption.strip()[:MAX_CAPTION_CHARS] if isinstance(caption, str) else ""
        image = images[image_id]
        posts.append(PostedImage(data=image.data, filename=image.filename, caption=caption))
        posted_ids.add(image_id)
    return tuple(posts)


# ----------------------------
# Making the image
# ----------------------------

def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    value = getattr(obj, name, None)
    if value is None:
        extra = getattr(obj, "model_extra", None)
        if isinstance(extra, dict):
            value = extra.get(name)
    return value


def _decode_image(data_url: str, image_id: str) -> GeneratedImage | None:
    if not isinstance(data_url, str) or len(data_url) > MAX_ENCODED_IMAGE_CHARS + 64:
        return None
    match = _DATA_URL_RE.match(data_url.strip())
    if not match:
        return None
    return _decode_raster(match.group(2), image_id, match.group(1).lower())


def _decode_raster(encoded: Any, image_id: str, mime_type: Any = None) -> GeneratedImage | None:
    """Decode either API's bounded raster, inferring an absent Image API MIME."""
    if not isinstance(encoded, str) or not encoded or len(encoded) > MAX_ENCODED_IMAGE_CHARS:
        return None
    if mime_type is not None:
        if not isinstance(mime_type, str) or mime_type.lower() not in RASTER_MIME_TYPES:
            return None
        mime_type = mime_type.lower()
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    if mime_type is None:
        mime_type = next((kind for kind in sorted(RASTER_MIME_TYPES) if _matches_raster_type(data, kind)), None)
    if mime_type is None or not _matches_raster_type(data, mime_type):
        return None
    return GeneratedImage(
        image_id=image_id,
        data=data,
        mime_type=mime_type,
        data_url=f"data:{mime_type};base64,{encoded}",
    )


def _matches_raster_type(data: bytes, mime_type: str) -> bool:
    """Reject mislabeled or active files without decoding image pixels."""
    if mime_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if mime_type == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if mime_type == "image/webp":
        return data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    return False


def extract_image(response: Any, image_id: str) -> GeneratedImage | None:
    """Read the dedicated Image API or legacy SDK/chat image response."""
    image_data = _field(response, "data")
    if isinstance(image_data, list):
        for item in image_data:
            decoded = _decode_raster(_field(item, "b64_json"), image_id, _field(item, "media_type"))
            if decoded is not None:
                return decoded
    choices = _field(response, "choices")
    if not isinstance(choices, list) or not choices:
        return None
    message = _field(choices[0], "message")
    image_items = _field(message, "images")
    if not isinstance(image_items, list):
        return None
    for item in image_items:
        image_url = _field(item, "image_url")
        url = _field(image_url, "url") if image_url is not None else None
        if isinstance(url, str):
            decoded = _decode_image(url, image_id)
            if decoded is not None:
                return decoded
    return None


async def request_image(client: Any, prompt: str, aspect_ratio: str) -> Any:
    """Use one dedicated Riverflow request, preserving other models' SDK path."""
    model = sfw_model()
    if model != "sourceful/riverflow-v2.5-fast":
        try:
            return await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                extra_body={
                    "modalities": ["image", "text"],
                    "image_config": {"aspect_ratio": aspect_ratio},
                },
                timeout=generation_timeout_seconds(),
            )
        except Exception as error:
            status = _numeric_status(getattr(error, "status_code", None))
            if status is not None:
                raise ImageServiceError(status) from None
            raise
    api_key = getattr(client, "api_key", None)
    if not isinstance(api_key, str) or not api_key.strip():
        api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key.strip():
        raise ImageServiceError(401)
    payload = {
        "model": model, "prompt": prompt, "resolution": "1K",
        "aspect_ratio": aspect_ratio, "n": 1, "output_format": "jpeg",
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://discord.com",
        "X-Title": "Harper's Companion",
    }
    timeout = aiohttp.ClientTimeout(total=generation_timeout_seconds())
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            "https://openrouter.ai/api/v1/images", headers=headers, json=payload,
        ) as response:
            try:
                response_json = await response.json()
            except (aiohttp.ContentTypeError, ValueError, UnicodeDecodeError):
                raise ImageServiceError(
                    response.status, invalid_response=response.status == 200,
                ) from None
            provider_error = response_json.get("error") if isinstance(response_json, dict) else None
            if response.status != 200 or provider_error is not None:
                code = provider_error.get("code") if isinstance(provider_error, dict) else None
                raise ImageServiceError(response.status, code)
            return response_json


def _log(outcome: str) -> None:
    # Never include prompts, captions, image content, or raw provider messages.
    print(f"[PAINTBOX] time={datetime.now(timezone.utc).isoformat(timespec='seconds')} "
          f"mode=sfw outcome={outcome}")


def _record_outcome(
    outcomes: dict[str, dict[str, Any]] | None,
    call_id: str,
    status: str,
    error: ImageServiceError | None = None,
    *,
    attempted: bool = False,
) -> None:
    if outcomes is None:
        return
    result: dict[str, Any] = {"status": status, "attempted": attempted}
    if error is not None:
        if error.status is not None:
            result["http_status"] = error.status
        if error.code is not None:
            result["error_code"] = error.code
    outcomes[call_id] = result


async def run_create_calls(
    client: Any,
    calls: list[Any],
    *,
    outcomes: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, str], dict[str, GeneratedImage]]:
    """Return tool results/images, optionally recording safe status per call id."""
    results: dict[str, str] = {}
    images: dict[str, GeneratedImage] = {}
    for index, call in enumerate(calls):
        call_id = getattr(call, "id", None) or f"paint-call-{index}"
        arguments = _call_arguments(call)
        prompt = arguments.get("prompt") if arguments else None
        if not isinstance(prompt, str) or not prompt.strip():
            _record_outcome(outcomes, call_id, "invalid_prompt")
            results[call_id] = "No image made: the prompt was empty."
            continue
        if index >= MAX_IMAGES_PER_REPLY:
            _record_outcome(outcomes, call_id, "limit_reached")
            results[call_id] = (
                f"No image made: at most {MAX_IMAGES_PER_REPLY} images per reply."
            )
            continue
        try:
            reserved = await _reserve_image_attempt()
        except PaintboxUsageError:
            _record_outcome(outcomes, call_id, "usage_error")
            _log("usage_storage_error")
            results[call_id] = (
                "No image made: today's paint allowance could not be safely checked. "
                "Nothing was posted."
            )
            continue
        if not reserved:
            _record_outcome(outcomes, call_id, "cap_reached")
            _log("cap_reached")
            results[call_id] = (
                "No image made: you're out of paint for today. The daily limit resets at "
                "midnight UTC. Let Daina know if you want to."
            )
            continue
        aspect = arguments.get("aspect", "square") if arguments else "square"
        ratio = ASPECT_RATIOS.get(aspect if isinstance(aspect, str) else "", "1:1")
        image_id = f"img-{len(images) + 1}"
        try:
            response = await request_image(client, prompt.strip()[:MAX_PROMPT_CHARS], ratio)
            image = extract_image(response, image_id)
        except ImageServiceError as error:
            _record_outcome(outcomes, call_id, "provider_error", error, attempted=True)
            _log(f"error:{error.category} http_status={error.status or 'unknown'} "
                 f"provider_code={error.code or 'none'}")
            results[call_id] = f"No image made: {_IMAGE_ERROR_REASONS[error.category]}. Nothing was posted."
            continue
        except Exception as error:
            _record_outcome(outcomes, call_id, "provider_error", attempted=True)
            _log(f"error:{type(error).__name__}")
            results[call_id] = (
                "No image made: the image service failed before returning a usable picture. "
                "Nothing was posted."
            )
            continue
        if image is None:
            _record_outcome(outcomes, call_id, "no_image", attempted=True)
            _log("no_image_returned")
            results[call_id] = (
                "No image made: the image service answered without a usable picture. "
                "Nothing was posted."
            )
            continue
        images[image_id] = image
        _record_outcome(outcomes, call_id, "generated", attempted=True)
        _log("created")
        results[call_id] = (
            f"Image made, id {image_id}. You can see it in the next message; nobody else "
            f"has. To share it, call post_image with image_id {image_id} and your caption. "
            "If you don't want to share it, just don't call post_image and it's dropped."
        )
    return results, images


def preview_message(images: dict[str, GeneratedImage]) -> dict[str, Any] | None:
    """A private look at the images, shown only to the companion before it decides."""
    if not images:
        return None
    ids = ", ".join(images)
    content: list[dict[str, Any]] = [{
        "type": "text",
        "text": (
            f"{PREVIEW_MARKER} This is the bot's tool output, not a message from anyone. "
            f"Here is what you just painted ({ids}). Only you can see it. "
            "Post it with post_image or let it go, then give your normal written reply."
        ),
    }]
    for image in images.values():
        content.append({"type": "image_url", "image_url": {"url": image.data_url}})
    return {"role": "user", "content": content}
