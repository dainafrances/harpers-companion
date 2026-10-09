from __future__ import annotations

import importlib
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
router = importlib.import_module("src.router")


def completion(content: str | None, *tool_calls: object) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=content, tool_calls=list(tool_calls), annotations=[],
    ))])


def tool_call(name: str, arguments: object, *, call_id: str = "status-1") -> SimpleNamespace:
    return SimpleNamespace(id=call_id, function=SimpleNamespace(
        name=name, arguments=json.dumps(arguments),
    ))


def status_call(text: str = "Kettle on.") -> SimpleNamespace:
    return tool_call("set_discord_status", {"text": text})


def audit(decision: str, *reason_codes: str) -> SimpleNamespace:
    return completion(json.dumps({"decision": decision, "reason_codes": list(reason_codes)}))


class StatusRouterTests(unittest.IsolatedAsyncioTestCase):
    async def generate(self, create: AsyncMock, *, owner: bool = True, **kwargs):
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        arguments = {
            "user_text": "Hello", "history": [], "latest_journal": None,
            "is_dm": True, "speaker_name": "Daina", "speaker_is_owner": owner,
        }
        arguments.update(kwargs)
        with (
            patch.object(router, "_client", client),
            patch.object(router, "build_system_prompt", return_value="IDENTITY"),
            patch.object(router, "build_memory_note", return_value=""),
            patch.dict(os.environ, {"ENABLE_WEB_SEARCH": "false"}),
        ):
            return await router.generate_companion_reply(**arguments)

    def setUp(self):
        enabled = patch.dict(os.environ, {"ENABLE_DISCORD_STATUS": "true"})
        enabled.start()
        self.addCleanup(enabled.stop)

    async def test_status_and_reactions_have_one_written_continuation(self):
        reaction = tool_call("react_to_message", {"emojis": ["💚"]}, call_id="reaction-1")
        create = AsyncMock(side_effect=[
            completion(None, status_call(), reaction), completion("Still here."), audit("ALLOW"),
        ])
        response = await self.generate(create)
        self.assertEqual(response, router.CompanionResponse(
            reply_text="Still here.", reaction_emojis=("💚",), status_text="Kettle on.",
        ))
        self.assertEqual(create.await_count, 3)
        writer, continuation, status_audit = create.await_args_list
        self.assertIn("set_discord_status", [tool["function"]["name"] for tool in writer.kwargs["tools"]])
        follow_up = continuation.kwargs["messages"][-3:]
        self.assertEqual([tool["function"]["name"] for tool in follow_up[0]["tool_calls"]],
                         ["set_discord_status", "react_to_message"])
        self.assertEqual([message["tool_call_id"] for message in follow_up[1:]],
                         ["status-1", "reaction-1"])
        self.assertEqual(continuation.kwargs["tools"], [])
        self.assertEqual(status_audit.kwargs["tools"], [])
        self.assertIn("not been applied", follow_up[1]["content"])

    async def test_non_owner_forged_call_is_ignored_but_text_continues(self):
        create = AsyncMock(side_effect=[completion(None, status_call()), completion("Hello there.")])
        response = await self.generate(create, owner=False)
        self.assertEqual(response, router.CompanionResponse(reply_text="Hello there."))
        self.assertEqual(create.await_count, 2)
        self.assertNotIn("set_discord_status", [tool["function"]["name"]
                         for tool in create.await_args_list[0].kwargs["tools"]])
        self.assertIn("not accepted", create.await_args_list[1].kwargs["messages"][-1]["content"])

    async def test_disabled_feature_ignores_forged_call(self):
        create = AsyncMock(side_effect=[completion(None, status_call()), completion("Hello.")])
        with patch.dict(os.environ, {"ENABLE_DISCORD_STATUS": "false"}):
            response = await self.generate(create)
        self.assertEqual(response, router.CompanionResponse(reply_text="Hello."))
        self.assertEqual(create.await_count, 2)
        self.assertNotIn("set_discord_status", [tool["function"]["name"]
                         for tool in create.await_args_list[0].kwargs["tools"]])

    def test_malformed_and_unbounded_status_arguments_are_ignored(self):
        for arguments in ([], None, {"text": 3}, {"text": "x" * 129}, {},
                          {"text": "Hello", "other": "value"}):
            with self.subTest(arguments=arguments):
                call = tool_call("set_discord_status", arguments)
                self.assertIsNone(router._requested_status([call], enabled=True))
        malformed = status_call()
        malformed.function.arguments = "not-json"
        self.assertIsNone(router._requested_status([malformed], enabled=True))
        self.assertEqual(router._requested_status([status_call("")], enabled=True), "")
        self.assertEqual(router._requested_status([status_call("First"), status_call("Last")], enabled=True), "Last")

    async def test_malformed_local_actions_do_not_lose_normal_reply(self):
        malformed = tool_call("set_discord_status", [])
        invalid_reaction = tool_call("react_to_message", [])
        create = AsyncMock(side_effect=[
            completion(None, malformed, invalid_reaction), completion("Here I am."),
        ])
        self.assertEqual(await self.generate(create), router.CompanionResponse(reply_text="Here I am."))
        self.assertEqual(create.await_count, 2)

    async def test_empty_continuation_preserves_original_text_and_citations(self):
        first = completion("Already written.", status_call())
        first.choices[0].message.annotations = [
            {"type": "url_citation", "url_citation": {"url": "https://example.com/source", "title": "Source"}},
        ]
        create = AsyncMock(side_effect=[first, completion("  "), audit("ALLOW")])
        response = await self.generate(create)
        self.assertIn("Already written.", response.reply_text)
        self.assertIn("https://example.com/source", response.reply_text)
        self.assertEqual(response.status_text, "Kettle on.")

    async def test_empty_status_continuation_drops_status_and_preserves_reaction(self):
        reaction = tool_call("react_to_message", {"emojis": ["💚"]}, call_id="reaction-1")
        create = AsyncMock(side_effect=[completion(None, status_call(), reaction), completion(None)])
        response = await self.generate(create)
        self.assertEqual(response, router.CompanionResponse(reaction_emojis=("💚",)))
        self.assertEqual(create.await_count, 2)

    async def test_failed_continuation_preserves_already_written_text(self):
        create = AsyncMock(side_effect=[
            completion("Already written.", status_call()), RuntimeError("unavailable"), audit("ALLOW"),
        ])
        self.assertEqual(await self.generate(create), router.CompanionResponse(
            reply_text="Already written.", status_text="Kettle on.",
        ))

    async def test_clear_skips_content_audit(self):
        create = AsyncMock(side_effect=[completion(None, status_call("")), completion("All clear.")])
        self.assertEqual(await self.generate(create), router.CompanionResponse(reply_text="All clear.", status_text=""))
        self.assertEqual(create.await_count, 2)

    async def test_status_rejection_preserves_reply_and_reactions(self):
        reaction = tool_call("react_to_message", {"emojis": ["💚"]}, call_id="reaction-1")
        create = AsyncMock(side_effect=[
            completion(None, status_call("A private detail"), reaction),
            completion("Private reply stays here."), audit("REJECT", "EXPLICIT_CONFIDENCE"),
        ])
        response = await self.generate(create)
        self.assertEqual(response, router.CompanionResponse(
            reply_text="Private reply stays here.", reaction_emojis=("💚",),
        ))

    async def test_status_auditor_errors_preserve_reply(self):
        create = AsyncMock(side_effect=[
            completion(None, status_call()), completion("Normal reply."),
            RuntimeError("unavailable"), RuntimeError("unavailable"),
        ])
        self.assertEqual(await self.generate(create), router.CompanionResponse(reply_text="Normal reply."))
        self.assertEqual(create.await_count, 4)

    async def test_malformed_status_audit_fails_closed_without_losing_reply(self):
        create = AsyncMock(side_effect=[
            completion(None, status_call()), completion("Normal reply."),
            completion("not-json"), completion(json.dumps({"decision": "ALLOW"})),
        ])
        self.assertEqual(await self.generate(create), router.CompanionResponse(reply_text="Normal reply."))
        self.assertEqual(create.await_count, 4)

    async def test_room_privacy_gate_runs_before_global_status_audit(self):
        create = AsyncMock(side_effect=[
            completion(None, status_call()), completion("Room reply."), audit("ALLOW"), audit("ALLOW"),
        ])
        response = await self.generate(create, continuity_auditor_context="Private receipt")
        self.assertEqual(response.status_text, "Kettle on.")
        room_audit, status_audit = create.await_args_list[-2:]
        self.assertEqual(room_audit.kwargs["messages"][0]["content"], router.PRIVACY_AUDITOR_SYSTEM)
        self.assertEqual(status_audit.kwargs["messages"][0]["content"], router.STATUS_AUDITOR_SYSTEM)
        self.assertEqual(create.await_args_list[1].kwargs["tools"], [])

    async def test_rejected_room_candidate_never_releases_its_status(self):
        create = AsyncMock(side_effect=[
            completion(None, status_call("Private detail")), completion("Rejected text."),
            audit("REJECT", "EXPLICIT_CONFIDENCE"), completion("Safe replacement."), audit("ALLOW"),
        ])
        response = await self.generate(create, continuity_auditor_context="Private receipt")
        self.assertEqual(response, router.CompanionResponse(reply_text="Safe replacement."))
        self.assertEqual(create.await_count, 5)
        self.assertFalse(any(call.kwargs["messages"][0]["content"] == router.STATUS_AUDITOR_SYSTEM
                             for call in create.await_args_list))

    async def test_public_audit_has_global_audience_and_full_inert_sources(self):
        create = AsyncMock(side_effect=[
            completion(None, status_call()), completion("Private room reply."), audit("ALLOW"), audit("ALLOW"),
        ])
        await self.generate(
            create, user_text="Owner's current words", history=[{"role": "user", "content": "Current-room confidence"}],
            latest_journal="Journal confidence", discord_retrieval_context="Retrieved confidence",
            continuity_writer_context="Writer awareness", continuity_auditor_context="Auditor awareness",
            continuity_private_origin_contents=("Private origin",), continuity_routine_contents=("Room routine",),
            continuity_couple_private_contents=("Couple detail",), direct_owner_message_text="Direct owner words",
        )
        payload = json.loads(create.await_args_list[-1].kwargs["messages"][1]["content"])
        self.assertEqual(payload["audience"]["scope"], "global_public_profile")
        self.assertTrue(payload["audience"]["visible_across_all_shared_servers"])
        self.assertTrue(payload["audience"]["current_room_does_not_limit_audience"])
        source = payload["source_evidence"]
        self.assertEqual(source["current_room_history"][0]["content"], "Current-room confidence")
        for field, expected in (
            ("latest_journal_context", "Journal confidence"), ("discord_retrieval_context", "Retrieved confidence"),
            ("continuity_writer_context", "Writer awareness"), ("continuity_auditor_context", "Auditor awareness"),
            ("direct_owner_message_text", "Direct owner words"),
        ):
            self.assertEqual(source[field], expected)
        self.assertEqual(source["continuity_private_origin_contents"], ["Private origin"])
        self.assertEqual(source["continuity_routine_contents"], ["Room routine"])
        self.assertEqual(source["continuity_couple_private_contents"], ["Couple detail"])
        self.assertNotIn("routine_origin_event_contents", payload)

    async def test_no_status_request_adds_no_auditor_calls(self):
        create = AsyncMock(return_value=completion("Hello."))
        self.assertEqual(await self.generate(create), router.CompanionResponse(reply_text="Hello."))
        self.assertEqual(create.await_count, 1)

    async def test_public_status_audit_receives_original_source_images(self):
        image_urls = ["https://cdn.discordapp.com/photo.png", "https://cdn.discordapp.com/screenshot.png"]
        create = AsyncMock(side_effect=[
            completion(None, status_call()), completion("Normal reply."), audit("ALLOW"),
        ])
        response = await self.generate(create, image_urls=image_urls)
        self.assertEqual(response.status_text, "Kettle on.")
        writer_content = create.await_args_list[0].kwargs["messages"][-1]["content"]
        audit_content = create.await_args_list[-1].kwargs["messages"][-1]["content"]
        self.assertEqual(audit_content[1:], writer_content[1:])
        self.assertEqual([part["image_url"]["url"] for part in audit_content[1:]], image_urls)
        payload = json.loads(audit_content[0]["text"])
        self.assertEqual(payload["audience"]["scope"], "global_public_profile")
        self.assertEqual(payload["candidate_status_text"], "Kettle on.")

    async def test_image_status_auditor_failure_preserves_normal_reply(self):
        image_urls = ["https://cdn.discordapp.com/private-photo.png"]
        create = AsyncMock(side_effect=[
            completion(None, status_call()), completion("Normal reply."),
            RuntimeError("vision unavailable"), RuntimeError("vision unavailable"),
        ])
        response = await self.generate(create, image_urls=image_urls)
        self.assertEqual(response, router.CompanionResponse(reply_text="Normal reply."))
        for call in create.await_args_list[-2:]:
            self.assertEqual(call.kwargs["messages"][-1]["content"][1], {
                "type": "image_url", "image_url": {"url": image_urls[0]},
            })


if __name__ == "__main__":
    unittest.main()
