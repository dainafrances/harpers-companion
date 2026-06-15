from __future__ import annotations

import importlib
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("OPENROUTER_API_KEY", "test-key")

router = importlib.import_module("src.router")


class RouterContextTests(unittest.IsolatedAsyncioTestCase):
    def test_observed_history_is_wrapped_and_source_metadata_is_removed(self) -> None:
        history = [
            {
                "role": "user",
                "content": "[SPEAKER] name=Solace\nA very distinctive cadence.",
                "source": "observed-companion-bot",
            },
            {
                "role": "assistant",
                "content": "Colin's prior reply.",
                "source": "human-direct",
            },
        ]

        prepared = router._prepare_history(history)

        self.assertEqual(set(prepared[0]), {"role", "content"})
        self.assertIn(router.OBSERVED_CONTEXT_OPEN, prepared[0]["content"])
        self.assertIn("Do not copy", prepared[0]["content"])
        self.assertIn("name=Solace", prepared[0]["content"])
        self.assertIn(router.OBSERVED_CONTEXT_CLOSE, prepared[0]["content"])
        self.assertEqual(
            prepared[1],
            {"role": "assistant", "content": "Colin's prior reply."},
        )

    async def test_actual_speaker_metadata_reaches_identity_prompt(self) -> None:
        create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="Reply"),
                    )
                ]
            )
        )
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create),
            )
        )

        with (
            patch.object(router, "_client", fake_client),
            patch.object(router, "build_system_prompt", return_value="IDENTITY") as build_prompt,
        ):
            reply = await router.generate_companion_reply(
                user_text="Hello",
                history=[],
                latest_journal=None,
                is_dm=False,
                speaker_name="Daina",
                speaker_is_owner=True,
            )

        self.assertEqual(reply, "Reply")
        build_prompt.assert_called_once_with(
            is_dm=False,
            speaker_name="Daina",
            speaker_is_owner=True,
        )
        sent_messages = create.await_args.kwargs["messages"]
        self.assertEqual(sent_messages[0], {"role": "system", "content": "IDENTITY"})
        self.assertEqual(
            sent_messages[1],
            {"role": "system", "content": router.HISTORY_INTERPRETATION_RULES},
        )


if __name__ == "__main__":
    unittest.main()
