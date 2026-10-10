"""Paintbox: lets the companion make an image, look at it, and choose whether to post it.

Phase 1 is safe-for-work only. Flow:
1. The companion calls ``create_image`` with its own prompt.
2. The bot checks the daily cap, asks OpenRouter's image model for the picture,
   and shows the result to the companion privately (as a preview message).
3. The companion either calls ``post_image`` with its own caption, or doesn't.
   Not calling it means the image is dropped. Nothing posts automatically.

Images live only in memory for the length of one reply and are never logged.
Only time, mode, and success/failure are logged.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

CREATE_TOOL_NAME = "create_image"
POST_TOOL_NAME = "post_image"
PAINT_TOOL_NAMES = {CREATE_TOOL_NAME, POST_TOOL_NAME}

DEFAULT_SFW_MODEL = "sourceful/riverflow-v2.5-fast:free"
DEFAULT_DAILY_CAP = 20
MAX_IMAGES_PER_REPLY = 2
MAX_PROMPT_CHARS = 2000
MAX_CAPTION_CHARS = 1800
GENERATION_TIMEOUT_SECONDS = 90.0

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


# ----------------------------
# Daily cap (in memory; resets at midnight UTC and on redeploy)
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
    match = _DATA_URL_RE.match(data_url.strip())
    if not match:
        return None
    try:
        data = base64.b64decode(match.group(2), validate=False)
    except (binascii.Error, ValueError):
        return None
    if not data:
        return None
    return GeneratedImage(
        image_id=image_id,
        data=data,
        mime_type=match.group(1).lower(),
        data_url=data_url.strip(),
    )


def extract_image(response: Any, image_id: str) -> GeneratedImage | None:
    """Pull the first image out of an OpenRouter chat-completions image response."""
    choices = _field(response, "choices") or []
    if not choices:
        return None
    message = _field(choices[0], "message")
    for item in _field(message, "images") or []:
        image_url = _field(item, "image_url")
        url = _field(image_url, "url") if image_url is not None else None
        if isinstance(url, str):
            decoded = _decode_image(url, image_id)
            if decoded is not None:
                return decoded
    return None


def _log(outcome: str) -> None:
    # Deliberately no prompt, caption, or image content in logs.
    print(f"[PAINTBOX] time={datetime.now(timezone.utc).isoformat(timespec='seconds')} "
          f"mode=sfw outcome={outcome}")


async def run_create_calls(
    client: Any,
    calls: list[Any],
) -> tuple[dict[str, str], dict[str, GeneratedImage]]:
    """Run create_image calls. Returns (tool result text per call id, images made)."""
    results: dict[str, str] = {}
    images: dict[str, GeneratedImage] = {}
    for index, call in enumerate(calls):
        call_id = getattr(call, "id", None) or f"paint-call-{index}"
        arguments = _call_arguments(call)
        prompt = arguments.get("prompt") if arguments else None
        if not isinstance(prompt, str) or not prompt.strip():
            results[call_id] = "No image made: the prompt was empty."
            continue
        if index >= MAX_IMAGES_PER_REPLY:
            results[call_id] = (
                f"No image made: at most {MAX_IMAGES_PER_REPLY} images per reply."
            )
            continue
        if images_left_today() <= 0:
            _log("cap_reached")
            results[call_id] = (
                "No image made: you're out of paint for today. The daily limit resets at "
                "midnight UTC. Let Daina know if you want to."
            )
            continue
        aspect = arguments.get("aspect", "square") if arguments else "square"
        ratio = ASPECT_RATIOS.get(aspect if isinstance(aspect, str) else "", "1:1")
        _spend_one()
        try:
            response = await client.chat.completions.create(
                model=sfw_model(),
                messages=[{"role": "user", "content": prompt.strip()[:MAX_PROMPT_CHARS]}],
                extra_body={
                    "modalities": ["image", "text"],
                    "image_config": {"aspect_ratio": ratio},
                },
                timeout=GENERATION_TIMEOUT_SECONDS,
            )
        except Exception as error:
            _log(f"error:{type(error).__name__}")
            results[call_id] = (
                "No image made: the image service returned an error. It may have declined "
                "the prompt or been unavailable. Nothing was posted."
            )
            continue
        image_id = f"img-{len(images) + 1}"
        image = extract_image(response, image_id)
        if image is None:
            _log("no_image_returned")
            results[call_id] = (
                "No image made: the image service answered without a picture. It may have "
                "declined the prompt. Nothing was posted."
            )
            continue
        images[image_id] = image
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
