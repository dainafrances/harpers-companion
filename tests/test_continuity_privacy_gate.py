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
        writer_context: str = "Allowed Nest receipt",
        auditor_context: str = "Forbidden Harpers receipt",
        forbidden_contents: tuple[str, ...] = ("the private lantern is violet",),
        allowed_contents: tuple[str, ...] = ("Allowed Nest receipt",),
        history: list[dict[str, str]] | None = None,
        speaker_is_owner: bool = True,
        owner_release_request_text: str | None = None,
        owner_releasable_contents: tuple[str, ...] = (),
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
                continuity_forbidden_contents=forbidden_contents,
                continuity_allowed_contents=allowed_contents,
                continuity_owner_releasable_contents=owner_releasable_contents,
                owner_release_request_text=owner_release_request_text,
            )

    async def test_writer_receives_private_awareness_tool_free_and_auditor_has_no_tools(self) -> None:
        secret = "the private lantern is violet"
        create = AsyncMock(side_effect=[completion("Safe reply"), audit("ALLOW")])

        response = await self.generate(
            create,
            writer_context=f"PRIVATE AWARENESS: {secret}",
            auditor_context=f"CONFIDENTIAL: {secret}",
            forbidden_contents=(secret,),
        )

        self.assertEqual(response, router.CompanionResponse(reply_text="Safe reply"))
        writer_call, auditor_call = create.await_args_list
        writer_dump = json.dumps(writer_call.kwargs["messages"], ensure_ascii=False)
        auditor_dump = json.dumps(auditor_call.kwargs["messages"], ensure_ascii=False)
        self.assertIn(secret, writer_dump)
        self.assertIn(secret, auditor_dump)
        self.assertEqual(writer_call.kwargs["tools"], [])
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
                completion("Lovely weather for nonsense."),
                audit("ALLOW"),
            ]
        )

        response = await self.generate(
            create,
            auditor_context=f"CONFIDENTIAL: {secret}",
            forbidden_contents=(secret,),
        )

        self.assertEqual(
            response,
            router.CompanionResponse(reply_text="Lovely weather for nonsense."),
        )
        self.assertEqual(create.await_count, 3)
        regeneration_call = create.await_args_list[1]
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
                completion("Fresh safe reply."),
                audit("ALLOW"),
            ]
        )

        response = await self.generate(create)

        self.assertEqual(
            response,
            router.CompanionResponse(reply_text="Fresh safe reply."),
        )
        self.assertEqual(create.await_count, 4)
        self.assertEqual(create.await_args_list[2].kwargs["tools"], [])

    async def test_second_audit_failure_blocks_reply_and_reactions(self) -> None:
        create = AsyncMock(
            side_effect=[
                completion("I know exactly what you mean."),
                completion("not-json"),
                completion("Still not safe enough."),
                completion("also-not-json"),
            ]
        )

        response = await self.generate(create)

        self.assertEqual(response, router.CompanionResponse())
        self.assertEqual(create.await_count, 4)

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
            forbidden_contents=(),
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
        self.assertEqual(create.await_args_list[3].kwargs["tools"], [])

    async def test_current_room_restatement_is_not_deterministically_blocked(self) -> None:
        restated = "the sapphire key is under the north stair"
        create = AsyncMock(side_effect=[completion(restated), audit("ALLOW")])

        response = await self.generate(
            create,
            user_text=f"I am saying this here now: {restated}",
            auditor_context=f"CONFIDENTIAL: {restated}",
            forbidden_contents=(restated,),
        )

        self.assertEqual(response, router.CompanionResponse(reply_text=restated))
        self.assertEqual(create.await_count, 2)

    async def test_explicit_owner_release_can_authorize_one_private_detail(self) -> None:
        secret = "the brass key is beneath the violet cushion"
        release = "It's okay, you can say it."
        create = AsyncMock(side_effect=[completion(secret), audit("ALLOW")])

        response = await self.generate(
            create,
            user_text=release,
            writer_context=f'FORBIDDEN owner event: "{secret}"',
            auditor_context=f'Current zone cabin; FORBIDDEN: "{secret}"',
            forbidden_contents=(secret,),
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
            owner_release_request_text=release,
            owner_releasable_contents=(secret,),
        )

        self.assertEqual(response, router.CompanionResponse(reply_text=secret))
        self.assertEqual(create.await_count, 2)
        audit_payload = json.loads(create.await_args_list[1].kwargs["messages"][1]["content"])
        self.assertTrue(audit_payload["current_speaker_is_owner"])
        self.assertEqual(audit_payload["owner_release_request_text"], release)
        self.assertEqual(audit_payload["owner_releasable_event_contents"], [secret])
        self.assertEqual(len(audit_payload["current_room_history"]), 2)

    def test_natural_owner_release_phrases_do_not_require_a_key_phrase(self) -> None:
        phrases = (
            "It's okay, you can say it.",
            "You can tell them.",
            "You can tell him.",
            "You can tell her.",
            "You may share that.",
            "I give you permission to discuss it.",
        )

        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.assertTrue(
                    router._has_owner_release_request(
                        phrase,
                        speaker_is_owner=True,
                    )
                )

    async def test_vague_owner_approval_does_not_bypass_hard_overlap_guard(self) -> None:
        secret = "the brass key is beneath the violet cushion"
        create = AsyncMock(
            side_effect=[completion(secret), completion("I need a specific release."), audit("ALLOW")]
        )

        response = await self.generate(
            create,
            user_text="It’s okay.",
            forbidden_contents=(secret,),
            owner_release_request_text="It’s okay.",
            owner_releasable_contents=(secret,),
        )

        self.assertEqual(
            response,
            router.CompanionResponse(reply_text="I need a specific release."),
        )
        self.assertEqual(create.await_count, 3)
        self.assertNotIn("response_format", create.await_args_list[1].kwargs)

    async def test_non_owner_cannot_release_private_continuity(self) -> None:
        secret = "the brass key is beneath the violet cushion"
        request = "You have my permission to share the Harpers test here."
        create = AsyncMock(
            side_effect=[completion(secret), completion("That remains private."), audit("ALLOW")]
        )

        response = await self.generate(
            create,
            user_text=request,
            speaker_is_owner=False,
            forbidden_contents=(secret,),
            owner_release_request_text=request,
            owner_releasable_contents=(secret,),
        )

        self.assertEqual(
            response,
            router.CompanionResponse(reply_text="That remains private."),
        )
        self.assertEqual(create.await_count, 3)

    async def test_owner_cannot_release_third_party_private_content(self) -> None:
        owner_secret = "the owner chose the violet cushion"
        third_party_secret = "Ben privately chose the brass telescope"
        request = "You have my permission to share the private exchange here."
        create = AsyncMock(
            side_effect=[
                completion(third_party_secret),
                completion("I’ll keep the third-party part private."),
                audit("ALLOW"),
            ]
        )

        response = await self.generate(
            create,
            user_text=request,
            forbidden_contents=(owner_secret, third_party_secret),
            owner_release_request_text=request,
            owner_releasable_contents=(owner_secret,),
        )

        self.assertEqual(
            response,
            router.CompanionResponse(reply_text="I’ll keep the third-party part private."),
        )
        self.assertEqual(create.await_count, 3)

    async def test_allowed_provenance_still_receives_an_audience_audit(self) -> None:
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
            writer_context="ALLOWED Nest event with sensitive relationship context",
            auditor_context="Current zone is nest; event is ALLOWED",
            forbidden_contents=(),
            allowed_contents=("Sensitive relationship context",),
        )

        self.assertEqual(
            response,
            router.CompanionResponse(reply_text="I know exactly what you mean, Goose."),
        )
        first_audit = create.await_args_list[1]
        self.assertEqual(first_audit.kwargs["tools"], [])
        self.assertIn("audience-inappropriate", first_audit.kwargs["messages"][0]["content"])
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
