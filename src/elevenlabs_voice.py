from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


DEFAULT_MODEL_ID = "eleven_multilingual_v2"
DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"


class VoiceGenerationError(RuntimeError):
    """Raised when ElevenLabs cannot create a voice recording."""


def _error_detail(payload: bytes) -> str:
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""

    detail = decoded.get("detail") if isinstance(decoded, dict) else None
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("detail")
    return str(detail)[:300] if detail else ""


def create_speech(
    text: str,
    *,
    api_key: str,
    voice_id: str,
    model_id: str = DEFAULT_MODEL_ID,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
    timeout_seconds: float = 90.0,
) -> bytes:
    """Convert text to MP3 bytes with ElevenLabs' synchronous TTS endpoint."""
    cleaned_text = text.strip()
    if not cleaned_text:
        raise ValueError("Text is required for voice generation.")
    if not api_key.strip():
        raise ValueError("ELEVENLABS_API_KEY is required for voice generation.")
    if not voice_id.strip():
        raise ValueError("ELEVENLABS_VOICE_ID is required for voice generation.")

    query = urlencode({"output_format": output_format})
    url = (
        "https://api.elevenlabs.io/v1/text-to-speech/"
        f"{quote(voice_id, safe='')}?{query}"
    )
    body = json.dumps(
        {
            "text": cleaned_text,
            "model_id": model_id,
        }
    ).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
            "xi-api-key": api_key,
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            audio = response.read()
    except HTTPError as error:
        detail = _error_detail(error.read())
        suffix = f": {detail}" if detail else ""
        raise VoiceGenerationError(
            f"ElevenLabs returned HTTP {error.code}{suffix}"
        ) from error
    except (URLError, TimeoutError) as error:
        raise VoiceGenerationError(f"Could not reach ElevenLabs: {error}") from error

    if not audio:
        raise VoiceGenerationError("ElevenLabs returned an empty audio file.")
    return audio
