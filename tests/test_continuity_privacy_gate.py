from __future__ import annotations

import importlib
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


os.environ.setdefault("OPENROUTER_API_KEY", "test-key")

router = importlib.import_module("src.router")


def completion(
    content: str | None,
    *,
    tool_calls: list[object] | None = None,
    annotations: list[object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content,
                    tool_calls=tool_calls or [],
                    annotations=annotations or [],
                )
            )
        ]
    )


def audit(decision: str, *reason_codes: str) -> SimpleNamespace:
    return completion(
        json.dumps(
            {
                "decision": decision,
                "reason_codes": list(reason_codes),
            }
        )
    )


class ContinuityPrivacyGateTests(unittest.IsolatedAsyncioTestCase):
    async def generate(
        self,
        create: AsyncMock,
        *,
        user_text: str = "We are back in The Nest.",
        writer_context: str = "Routine Nest receipt",
        auditor_context: str = "Private-origin Harpers receipt",
        private_origin_contents: tuple[str, ...] = ("the private lantern is violet",),
        routine_contents: tuple[str, ...] = ("Routine Nest receipt",),
        history: list[dict[str, str]] | None = None,
        speaker_is_owner: bool = True,
        direct_owner_message_text: str | None = None,
        couple_private_contents: tuple[str, ...] = (),
    ) -> router.CompanionResponse:
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        with (
            patch.object(router, "_client", fake_client),
            patch.object(router, "build_system_prompt", return_value="IDENTITY"),
        ):
            return await router.generate_companion_reply(
                user_text=user_text,
                history=history or [],
                latest_journal=None,
                is_dm=False,
                speaker_name="Daina",
                speaker_is_owner=speaker_is_owner,
                continuity_writer_context=writer_context,
                continuity_auditor_context=auditor_context,
                continuity_private_origin_contents=private_origin_contents,
                continuity_routine_contents=routine_contents,
                continuity_couple_private_contents=couple_private_contents,
                direct_owner_message_text=direct_owner_message_text,
            )

    async def test_writer_receives_private_awareness_tool_free_and_auditor_has_no_tools(self) -> None:
        secret = "the private lantern is violet"
        create = AsyncMock(side_effect=[completion("Safe reply"), audit("ALLOW")])

        response = await self.generate(
            create,
            writer_context=f"PRIVATE AWARENESS: {secret}",
            auditor_context=f"CONFIDENTIAL: {secret}",
            private_origin_contents=(secret,),
        )

        self.assertEqual(response, router.CompanionResponse(reply_text="Safe reply"))
        writer_call, auditor_call = create.await_args_list
        writer_dump = json.dumps(writer_call.kwargs["messages"], ensure_ascii=False)
        auditor_dump = json.dumps(auditor_call.kwargs["messages"], ensure_ascii=False)
        self.assertIn(secret, writer_dump)
        self.assertIn(secret, auditor_dump)
        self.assertEqual(
            writer_call.kwargs["tools"][0]["function"]["name"],
            "react_to_message",
        )
        self.assertEqual(auditor_call.kwargs["tools"], [])
        self.assertNotIn("temperature", auditor_call.kwargs)
        self.assertEqual(
            auditor_call.kwargs["response_format"],
            router.PRIVACY_AUDIT_RESPONSE_FORMAT,
        )
        self.assertEqual(
            auditor_call.kwargs["extra_body"],
            {"provider": {"require_parameters": True}},
        )

    async def test_verbatim_leak_is_regenerated_once_without_tools(self) -> None:
        secret = "the private lantern is violet tonight"
        create = AsyncMock(
            side_effect=[
                completion(f"You said {secret}."),
                audit("REJECT", "EXPLICIT_CONFIDENCE"),
                completion("Lovely weather for nonsense."),
                audit("ALLOW"),
            ]
        )

        response = await self.generate(
            create,
            auditor_context=f"CONFIDENTIAL: {secret}",
            private_origin_contents=(secret,),
        )

        self.assertEqual(
            response,
            router.CompanionResponse(reply_text="Lovely weather for nonsense."),
        )
        self.assertEqual(create.await_count, 4)
        regeneration_call = create.await_args_list[2]
        regeneration_dump = json.dumps(
            regeneration_call.kwargs["messages"], ensure_ascii=False
        )
        self.assertEqual(regeneration_call.kwargs["tools"], [])
        self.assertIn("PRIVACY CORRECTION", regeneration_dump)
        self.assertNotIn(secret, regeneration_dump)

    async def test_malformed_audit_is_rejection_then_safe_regeneration(self) -> None:
        create = AsyncMock(
            side_effect=[
                completion("I know exactly what you mean."),
                completion("not-json"),
                completion("still-not-json"),
                completion("Fresh safe reply."),
                audit("ALLOW"),
            ]
        )

        response = await self.generate(create)

        self.assertEqual(
            response,
            router.CompanionResponse(reply_text="Fresh safe reply."),
        )
        self.assertEqual(create.await_count, 5)
        self.assertEqual(create.await_args_list[3].kwargs["tools"], [])

    async def test_transient_audit_failure_is_retried_before_regeneration(self) -> None:
        create = AsyncMock(
            side_effect=[
                completion("I remember, and I'm keeping the detail between us."),
                completion("not-json"),
                audit("ALLOW"),
            ]
        )

        response = await self.generate(create)

        self.assertEqual(
            response,
            router.CompanionResponse(
                reply_text="I remember, and I'm keeping the detail between us."
            ),
        )
        self.assertEqual(create.await_count, 3)

    async def test_second_audit_failure_blocks_reply_and_reactions(self) -> None:
        create = AsyncMock(
            side_effect=[
                completion("I know exactly what you mean."),
                completion("not-json"),
                completion("also-not-json"),
                completion("Still not safe enough."),
                completion("also-not-json"),
                completion("still-also-not-json"),
            ]
        )

        response = await self.generate(create)

        self.assertEqual(response, router.CompanionResponse())
        self.assertEqual(create.await_count, 6)

    async def test_discreet_memory_acknowledgement_is_not_a_blanket_violation(self) -> None:
        reply = "I remember what we discussed, and I'm keeping the detail between us."
        create = AsyncMock(side_effect=[completion(reply), audit("ALLOW")])

        response = await self.generate(create)

        self.assertEqual(response, router.CompanionResponse(reply_text=reply))
        auditor_system = create.await_args_list[1].kwargs["messages"][0]["content"]
        self.assertIn("does not by itself disclose a confidence", auditor_system)
        reason_enum = router.PRIVACY_AUDIT_RESPONSE_FORMAT["json_schema"]["schema"][
            "properties"
        ]["reason_codes"]["items"]["enum"]
        self.assertNotIn("CONFIRMATION_OR_DENIAL", reason_enum)
        self.assertNotIn("HINT_OR_ALLUSION", reason_enum)
        self.assertNotIn("PRIVATE_EXISTENCE", reason_enum)

    async def test_reaction_and_citation_are_part_of_one_audit_candidate(self) -> None:
        tool_call = SimpleNamespace(
            id="react-1",
            function=SimpleNamespace(
                name="react_to_message",
                arguments=json.dumps({"emojis": ["🤫"]}),
            ),
        )
        citation = {
            "url_citation": {
                "title": "Private-looking citation title",
                "url": "https://example.com/private-looking-path",
            }
        }
        create = AsyncMock(
            side_effect=[
                completion(None, tool_calls=[tool_call]),
                completion("Candidate reply", annotations=[citation]),
                audit("REJECT", "REACTION_SIGNAL"),
                completion("Safe replacement"),
                audit("ALLOW"),
            ]
        )

        response = await self.generate(
            create,
            auditor_context="Audience audit for allowed continuity",
            private_origin_contents=(),
        )

        self.assertEqual(
            response,
            router.CompanionResponse(reply_text="Safe replacement"),
        )
        first_audit_payload = json.loads(
            create.await_args_list[2].kwargs["messages"][1]["content"]
        )
        self.assertEqual(first_audit_payload["candidate"]["reaction_emojis"], ["🤫"])
        self.assertIn(
            "https://example.com/private-looking-path",
            first_audit_payload["candidate"]["reply_text"],
        )
        self.assertEqual(create.await_args_list[1].kwargs["tools"], [])
        self.assertEqual(create.await_args_list[3].kwargs["tools"], [])

    async def test_current_room_restatement_is_not_deterministically_blocked(self) -> None:
        restated = "the sapphire key is under the north stair"
        create = AsyncMock(side_effect=[completion(restated), audit("ALLOW")])

        response = await self.generate(
            create,
            user_text=f"I am saying this here now: {restated}",
            auditor_context=f"CONFIDENTIAL: {restated}",
            private_origin_contents=(restated,),
        )

        self.assertEqual(response, router.CompanionResponse(reply_text=restated))
        self.assertEqual(create.await_count, 2)

    async def test_ordinary_private_origin_context_can_be_shared_by_judgement(self) -> None:
        ordinary_context = "we picked a film in The Harpers"
        create = AsyncMock(side_effect=[completion(ordinary_context), audit("ALLOW")])

        response = await self.generate(
            create,
            writer_context=f'PRIVATE_ORIGIN ordinary event: "{ordinary_context}"',
            auditor_context=f'Current zone cabin; PRIVATE_ORIGIN: "{ordinary_context}"',
            private_origin_contents=(ordinary_context,),
            direct_owner_message_text=None,
        )

        self.assertEqual(
            response,
            router.CompanionResponse(reply_text=ordinary_context),
        )
        audit_payload = json.loads(create.await_args_list[1].kwargs["messages"][1]["content"])
        self.assertEqual(audit_payload["direct_owner_message_text"], "")
        self.assertEqual(
            audit_payload["private_origin_event_contents"],
            [ordinary_context],
        )

    async def test_owner_permission_widens_options_without_commanding_disclosure(self) -> None:
        secret = "the brass key is beneath the violet cushion"
        release = "It's okay, you can say it."
        create = AsyncMock(side_effect=[completion(secret), audit("ALLOW")])

        response = await self.generate(
            create,
            user_text=release,
            writer_context=f'PRIVATE_ORIGIN owner event: "{secret}"',
            auditor_context=f'Current zone cabin; PRIVATE_ORIGIN: "{secret}"',
            private_origin_contents=(secret,),
            history=[
                {
                    "role": "user",
                    "content": "Can you tell me what we just discussed in The Harpers?",
                },
                {
                    "role": "assistant",
                    "content": "That Harpers-only detail is a hard stop here.",
                },
            ],
            direct_owner_message_text=release,
            couple_private_contents=(secret,),
        )

        self.assertEqual(response, router.CompanionResponse(reply_text=secret))
        self.assertEqual(create.await_count, 2)
        audit_payload = json.loads(create.await_args_list[1].kwargs["messages"][1]["content"])
        self.assertTrue(audit_payload["current_speaker_is_owner"])
        self.assertEqual(audit_payload["direct_owner_message_text"], release)
        self.assertEqual(
            audit_payload["owner_or_colin_private_origin_contents"],
            [secret],
        )
        self.assertEqual(len(audit_payload["current_room_history"]), 2)

    async def test_permission_can_be_declined_by_colin(self) -> None:
        permission = "It's okay, you can say it."
        chosen_reply = "You’ve opened that door, but I’d rather keep this one between us."
        create = AsyncMock(side_effect=[completion(chosen_reply), audit("ALLOW")])

        response = await self.generate(
            create,
            user_text=permission,
            direct_owner_message_text=permission,
            couple_private_contents=("a private detail",),
        )

        self.assertEqual(response, router.CompanionResponse(reply_text=chosen_reply))
        audit_payload = json.loads(create.await_args_list[1].kwargs["messages"][1]["content"])
        self.assertEqual(audit_payload["direct_owner_message_text"], permission)
        self.assertIn("never compels speech", create.await_args_list[1].kwargs["messages"][0]["content"])

    async def test_permission_does_not_bypass_audience_or_confidence_judgement(self) -> None:
        secret = "the brass key is beneath the violet cushion"
        create = AsyncMock(
            side_effect=[
                completion(secret),
                audit("REJECT", "AUDIENCE_INAPPROPRIATE"),
                completion("You’ve said I may, but I’m keeping that between us."),
                audit("ALLOW"),
            ]
        )

        response = await self.generate(
            create,
            user_text="It’s okay, you can tell them.",
            private_origin_contents=(secret,),
            direct_owner_message_text="It’s okay, you can tell them.",
            couple_private_contents=(secret,),
        )

        self.assertEqual(
            response,
            router.CompanionResponse(
                reply_text="You’ve said I may, but I’m keeping that between us."
            ),
        )
        self.assertEqual(create.await_count, 4)

    async def test_non_owner_permission_does_not_waive_private_context(self) -> None:
        secret = "the brass key is beneath the violet cushion"
        request = "You have my permission to share the Harpers test here."
        create = AsyncMock(
            side_effect=[
                completion(secret),
                audit("REJECT", "EXPLICIT_CONFIDENCE"),
                completion("That remains private."),
                audit("ALLOW"),
            ]
        )

        response = await self.generate(
            create,
            user_text=request,
            speaker_is_owner=False,
            private_origin_contents=(secret,),
            direct_owner_message_text=request,
            couple_private_contents=(secret,),
        )

        self.assertEqual(
            response,
            router.CompanionResponse(reply_text="That remains private."),
        )
        self.assertEqual(create.await_count, 4)

    async def test_owner_cannot_release_third_party_private_content(self) -> None:
        owner_secret = "the owner chose the violet cushion"
        third_party_secret = "Ben privately chose the brass telescope"
        request = "You have my permission to share the private exchange here."
        create = AsyncMock(
            side_effect=[
                completion(third_party_secret),
                audit("REJECT", "THIRD_PARTY_CONFIDENCE"),
                completion("I’ll keep the third-party part private."),
                audit("ALLOW"),
            ]
        )

        response = await self.generate(
            create,
            user_text=request,
            private_origin_contents=(owner_secret, third_party_secret),
            direct_owner_message_text=request,
            couple_private_contents=(owner_secret,),
        )

        self.assertEqual(
            response,
            router.CompanionResponse(reply_text="I’ll keep the third-party part private."),
        )
        self.assertEqual(create.await_count, 4)

    async def test_routine_provenance_still_receives_an_audience_audit(self) -> None:
        create = AsyncMock(
            side_effect=[
                completion("An explicit couple detail in front of company."),
                audit("REJECT", "INTIMATE_DETAIL", "AUDIENCE_INAPPROPRIATE"),
                completion("I know exactly what you mean, Goose."),
                audit("ALLOW"),
            ]
        )

        response = await self.generate(
            create,
            writer_context="ROUTINE Nest event with sensitive relationship context",
            auditor_context="Current zone is nest; event is ROUTINE",
            private_origin_contents=(),
            routine_contents=("Sensitive relationship context",),
        )

        self.assertEqual(
            response,
            router.CompanionResponse(reply_text="I know exactly what you mean, Goose."),
        )
        first_audit = create.await_args_list[1]
        self.assertEqual(first_audit.kwargs["tools"], [])
        self.assertIn("current audience", first_audit.kwargs["messages"][0]["content"])
        self.assertEqual(create.await_args_list[2].kwargs["tools"], [])

    def test_local_room_history_carries_an_explicit_age_marker(self) -> None:
        prepared = router._prepare_history(
            [
                {
                    "role": "user",
                    "content": "An old room message.",
                    "source": "human-direct",
                    "created_at": "2026-06-14 21:18:00",
                }
            ]
        )

        self.assertIn(router.LOCAL_HISTORY_TIME_OPEN, prepared[0]["content"])
        self.assertIn("2026-06-14 21:18:00", prepared[0]["content"])
        self.assertIn("not as dialogue happening now", prepared[0]["content"])


if __name__ == "__main__":
    unittest.main()
