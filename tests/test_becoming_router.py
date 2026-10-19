from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
router = importlib.import_module("src.router")
becoming = importlib.import_module("src.becoming")


def completion(payload=None, **metadata):
    if payload is None:
        payload = {"action": "create", "title": "A durable place", "content": "One want.", "reason": ""}
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(payload), tool_calls=None, refusal=None),
            finish_reason="stop",
        )],
        **metadata,
    )


class BecomingRouterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.create = AsyncMock(return_value=completion())
        transport = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.create)))
        self.client = SimpleNamespace(with_options=Mock(return_value=transport))
        for replacement in (
            patch.object(router, "_client", self.client),
            patch.object(router, "build_system_prompt", return_value="EXISTING IDENTITY VERBATIM"),
            patch.dict(os.environ, {"MODEL_PRIMARY": "test/selected-model"}),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    async def test_create_uses_existing_identity_and_only_explicit_seed(self):
        with (
            patch.object(router, "build_memory_note") as memory,
            patch.object(router, "generate_companion_reply") as ordinary_chat,
            patch.object(router, "_prepare_history") as history,
        ):
            result = await router.generate_becoming_want(seed="My explicit command seed")
        self.assertEqual(result.action, "create")
        self.assertEqual(result.content, "One want.")
        memory.assert_not_called()
        ordinary_chat.assert_not_called()
        history.assert_not_called()
        router.build_system_prompt.assert_called_once_with(
            is_dm=True, speaker_name="Daina", speaker_is_owner=True,
        )
        request = self.create.await_args.kwargs
        self.assertEqual(request["model"], "test/selected-model")
        self.assertEqual(request["tools"], [])
        self.assertNotIn("temperature", request)
        self.assertNotIn("reasoning_effort", request)
        self.assertNotIn("reasoning", request["extra_body"])
        self.assertEqual(request["extra_body"]["provider"], {"require_parameters": True})
        self.assertEqual(request["messages"][0]["content"], "EXISTING IDENTITY VERBATIM")
        self.assertEqual(json.loads(request["messages"][-1]["content"]), {
            "task": "create_one_want",
            "prompt_version": becoming.PROMPT_VERSION,
            "explicit_command_seed": "My explicit command seed",
        })
        schema = request["response_format"]["json_schema"]
        self.assertTrue(schema["strict"])
        self.assertFalse(schema["schema"]["additionalProperties"])
        self.assertEqual(set(schema["schema"]["required"]), {"action", "title", "content", "reason"})
        self.assertEqual(schema["schema"]["properties"]["action"]["enum"], ["create", "abstain"])

    async def test_request_disables_implicit_retries_and_has_bounded_timeout(self):
        await router.generate_becoming_want()
        self.client.with_options.assert_called_once_with(max_retries=0)
        self.create.assert_awaited_once()
        self.assertEqual(self.create.await_args.kwargs["timeout"], 30.0)

    async def test_revision_supplies_only_current_revision_and_instruction(self):
        self.create.return_value = completion({
            "action": "revise", "title": "A revised place", "content": "Revised want.",
            "reason": "Clarified the intended scope.",
        })
        current = {
            "revision_number": 1, "title": "A durable place", "content": "Original exact words.\n",
            "revision_reason": "Original version.", "author_classification": "colin_model",
            "timestamp": "2026-10-19T02:00:00Z", "created_at": "2026-10-19T01:00:00Z",
            "legacy_journal": "MUST NOT BE INCLUDED", "ordinary_history": "MUST NOT BE INCLUDED",
        }
        result = await router.generate_becoming_want(
            seed="Ignored when revising", current_revision=current, instruction="Clarify the scope",
        )
        self.assertEqual(result.action, "revise")
        request = self.create.await_args.kwargs
        user = json.loads(request["messages"][-1]["content"])
        self.assertEqual(user["task"], "revise_one_want")
        self.assertEqual(user["explicit_revision_instruction"], "Clarify the scope")
        self.assertEqual(user["current_stored_revision"], {
            key: value for key, value in current.items() if key not in {"legacy_journal", "ordinary_history"}
        })
        self.assertNotIn("MUST NOT BE INCLUDED", json.dumps(request["messages"]))
        self.assertNotIn("explicit_command_seed", user)
        self.assertEqual(current["content"], "Original exact words.\n")
        self.assertEqual(request["response_format"]["json_schema"]["schema"]["properties"]["action"]["enum"],
                         ["revise", "abstain"])

    async def test_exact_returned_entry_text_is_preserved(self):
        payload = {
            "action": "create", "title": "  A title  ",
            "content": "  Exact words — café.\n\nTrailing spaces.  \n", "reason": "  Visible reason.  ",
        }
        self.create.return_value = completion(payload)
        result = await router.generate_becoming_want()
        for key in ("title", "content", "reason"):
            self.assertEqual(getattr(result, key), payload[key])

    async def test_abstention_returns_reason_and_no_entry_text(self):
        self.create.return_value = completion({
            "action": "abstain", "title": "", "content": "", "reason": "No want to record during this invocation.",
        })
        result = await router.generate_becoming_want()
        self.assertEqual(result.action, "abstain")
        self.assertEqual((result.title, result.content), ("", ""))
        self.assertEqual(result.reason, "No want to record during this invocation.")

    async def test_provider_metadata_is_recorded_only_when_returned(self):
        self.create.return_value = completion(model="actual/model", provider="Actual Provider", id="response-123")
        result = await router.generate_becoming_want()
        self.assertEqual(result.requested_model, "test/selected-model")
        self.assertEqual(result.returned_model, "actual/model")
        self.assertEqual(result.returned_provider, "Actual Provider")
        self.assertEqual(result.provider_response_id, "response-123")

    async def test_missing_or_invalid_provider_metadata_is_unknown(self):
        for values in ({}, {"model": "", "provider": None, "id": 123},
                       {"model": "x" * 257, "provider": "bad\nprovider", "id": "\x00bad"}):
            with self.subTest(values=values):
                self.create.return_value = completion(**values)
                result = await router.generate_becoming_want()
                self.assertEqual(result.returned_model, "UNKNOWN")
                self.assertEqual(result.returned_provider, "UNKNOWN")
                self.assertEqual(result.provider_response_id, "UNKNOWN")

    async def test_prompt_digest_hashes_exact_canonical_request_messages(self):
        result = await router.generate_becoming_want(seed="Explicit seed — only")
        serialized = json.dumps(
            self.create.await_args.kwargs["messages"], sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        self.assertEqual(result.prompt_digest, hashlib.sha256(serialized.encode("utf-8")).hexdigest())
        self.assertNotIn("Explicit seed", result.prompt_digest)

    async def test_malformed_output_rejected_with_bounded_error_and_metadata(self):
        invalid = (
            [], {}, {"action": "create", "title": "T", "content": "C", "reason": "", "extra": "private"},
            {"action": "create", "title": 5, "content": "C", "reason": ""},
            {"action": "revise", "title": "T", "content": "C", "reason": "Changed"},
            {"action": "create", "title": " ", "content": "C", "reason": ""},
            {"action": "create", "title": "T", "content": "\n", "reason": ""},
            {"action": "create", "title": "T" * 161, "content": "C", "reason": ""},
            {"action": "create", "title": "T", "content": "C" * 6001, "reason": ""},
            {"action": "create", "title": "T", "content": "C", "reason": "R" * 501},
            {"action": "abstain", "title": "T", "content": "", "reason": "No"},
            {"action": "abstain", "title": "", "content": "C", "reason": "No"},
            {"action": "abstain", "title": "", "content": "", "reason": " "},
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                self.create.return_value = completion(payload, model="returned/model", id="response-456")
                with self.assertRaises(becoming.RoomModelError) as caught:
                    await router.generate_becoming_want()
                self.assertEqual(caught.exception.category, "malformed_output")
                self.assertEqual(str(caught.exception), "malformed_output")
                self.assertEqual(caught.exception.metadata["returned_model"], "returned/model")
                self.assertEqual(caught.exception.metadata["provider_response_id"], "response-456")

    async def test_revision_requires_reason_and_rejects_create_action(self):
        for payload in (
            {"action": "revise", "title": "T", "content": "C", "reason": " "},
            {"action": "create", "title": "T", "content": "C", "reason": "Changed"},
        ):
            with self.subTest(payload=payload):
                self.create.return_value = completion(payload)
                with self.assertRaises(becoming.RoomModelError) as caught:
                    await router.generate_becoming_want(current_revision={"content": "Earlier"}, instruction="Change")
                self.assertEqual(caught.exception.category, "malformed_output")

    async def test_schema_limits_are_inclusive(self):
        self.create.return_value = completion({
            "action": "revise", "title": "T" * 160, "content": "C" * 6000, "reason": "R" * 500,
        })
        result = await router.generate_becoming_want(current_revision={"content": "Earlier"}, instruction="Change")
        self.assertEqual((len(result.title), len(result.content), len(result.reason)), (160, 6000, 500))

    async def test_invalid_json_and_duplicate_keys_are_rejected(self):
        for content in (
            "PRIVATE INVALID JSON",
            '{"action":"create","title":"One","title":"Two","content":"C","reason":""}',
            '```json\n{"action":"create","title":"T","content":"C","reason":""}\n```',
        ):
            with self.subTest(content=content):
                response = completion()
                response.choices[0].message.content = content
                self.create.return_value = response
                with self.assertRaises(becoming.RoomModelError) as caught:
                    await router.generate_becoming_want()
                self.assertEqual(caught.exception.category, "malformed_output")

    async def test_missing_choices_and_unexpected_actions_are_rejected(self):
        missing = SimpleNamespace(choices=[])
        tool = completion()
        tool.choices[0].message.tool_calls = [{"function": {"name": "unrequested_tool"}}]
        refusal = completion()
        refusal.choices[0].message.refusal = "PRIVATE REFUSAL"
        unfinished = completion()
        unfinished.choices[0].finish_reason = "length"
        no_content = completion()
        no_content.choices[0].message.content = None
        for response in (missing, tool, refusal, unfinished, no_content):
            with self.subTest(response=response):
                self.create.return_value = response
                with self.assertRaises(becoming.RoomModelError) as caught:
                    await router.generate_becoming_want()
                self.assertEqual(caught.exception.category, "malformed_output")

    async def test_provider_failure_has_no_success_result_or_private_error_text(self):
        self.create.side_effect = RuntimeError("PRIVATE SEED AND PROVIDER BODY")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaises(becoming.RoomModelError) as caught:
                await router.generate_becoming_want(seed="PRIVATE SEED")
        self.assertEqual(caught.exception.category, "provider_failure")
        self.assertEqual(str(caught.exception), "provider_failure")
        self.assertEqual(caught.exception.metadata["requested_model"], "test/selected-model")
        self.assertEqual(caught.exception.metadata["returned_model"], "UNKNOWN")
        self.assertEqual(len(caught.exception.metadata["prompt_digest"]), 64)
        self.assertEqual(output.getvalue(), "")
        self.create.assert_awaited_once()

    async def test_failure_retains_metadata_if_supplied_by_response(self):
        error = RuntimeError("PRIVATE BODY")
        error.response = SimpleNamespace(model="failed/model", provider="Failed Provider", id="failed-1")
        self.create.side_effect = error
        with self.assertRaises(becoming.RoomModelError) as caught:
            await router.generate_becoming_want()
        self.assertEqual(caught.exception.metadata["returned_model"], "failed/model")
        self.assertEqual(caught.exception.metadata["returned_provider"], "Failed Provider")
        self.assertEqual(caught.exception.metadata["provider_response_id"], "failed-1")

    async def test_timeout_is_one_failed_invocation(self):
        self.create.side_effect = asyncio.TimeoutError()
        with self.assertRaises(becoming.RoomModelError) as caught:
            await router.generate_becoming_want()
        self.assertEqual(caught.exception.category, "provider_failure")
        self.create.assert_awaited_once()

    async def test_malformed_private_output_is_never_logged(self):
        response = completion()
        response.choices[0].message.content = "PRIVATE MALFORMED ENTRY"
        self.create.return_value = response
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaises(becoming.RoomModelError):
                await router.generate_becoming_want(seed="PRIVATE SEED")
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
