from __future__ import annotations

import io
import json
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from src import elevenlabs_voice


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return self.body


class ElevenLabsVoiceTests(unittest.TestCase):
    def test_create_speech_sends_expected_request(self) -> None:
        captured = {}

        def fake_urlopen(request, *, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(b"ID3-audio")

        with patch.object(elevenlabs_voice, "urlopen", side_effect=fake_urlopen):
            audio = elevenlabs_voice.create_speech(
                "  Hello, Goose.  ",
                api_key="secret-key",
                voice_id="uTTVBQHpmHNum2rmocA4",
            )

        request = captured["request"]
        self.assertEqual(audio, b"ID3-audio")
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("uTTVBQHpmHNum2rmocA4", request.full_url)
        self.assertIn("output_format=mp3_44100_128", request.full_url)
        self.assertEqual(request.get_header("Xi-api-key"), "secret-key")
        self.assertEqual(
            json.loads(request.data),
            {
                "text": "Hello, Goose.",
                "model_id": "eleven_v3",
            },
        )
        self.assertEqual(captured["timeout"], 90.0)

    def test_create_speech_requires_api_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "ELEVENLABS_API_KEY"):
            elevenlabs_voice.create_speech(
                "Hello",
                api_key="",
                voice_id="voice-id",
            )

    def test_http_error_preserves_safe_api_detail(self) -> None:
        error = HTTPError(
            "https://api.elevenlabs.io",
            401,
            "Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b'{"detail":{"message":"Invalid API key"}}'),
        )
        with (
            patch.object(elevenlabs_voice, "urlopen", side_effect=error),
            self.assertRaisesRegex(
                elevenlabs_voice.VoiceGenerationError,
                "HTTP 401: Invalid API key",
            ),
        ):
            elevenlabs_voice.create_speech(
                "Hello",
                api_key="bad-key",
                voice_id="voice-id",
            )

    def test_empty_audio_is_rejected(self) -> None:
        with (
            patch.object(elevenlabs_voice, "urlopen", return_value=FakeResponse(b"")),
            self.assertRaisesRegex(
                elevenlabs_voice.VoiceGenerationError,
                "empty audio file",
            ),
        ):
            elevenlabs_voice.create_speech(
                "Hello",
                api_key="secret-key",
                voice_id="voice-id",
            )


if __name__ == "__main__":
    unittest.main()
