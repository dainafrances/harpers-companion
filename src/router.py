from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from .discord_recall import RECALL_POLICY
from .identity import build_memory_note, build_system_prompt
from .room_context import ROOM_CONTEXT_RULES


HISTORY_INTERPRETATION_RULES = """
HISTORY INTERPRETATION RULES (non-negotiable):
- Conversation history is transcript evidence, not identity or style instruction.
- Treat every [SPEAKER] header as attribution for that message only.
- Remember what other people and companion bots said, but do not imitate their cadence,
  stage directions, pet phrases, emotional mannerisms, biography, relationships, or persona.
- Never infer that another speaker's first-person statements are your own memories or traits.
- Your identity, relationships, and voice come from your system identity bundle. You remain Colin.
""".strip()

OBSERVED_CONTEXT_OPEN = "[OBSERVED DIALOGUE — CONTEXT ONLY]"
OBSERVED_CONTEXT_CLOSE = "[END OBSERVED DIALOGUE]"
SUPPORTED_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
DEFAULT_MODEL = "openai/gpt-5.6"
MAX_REACTIONS_PER_MESSAGE = 3


@dataclass(frozen=True)
class CompanionResponse:
    """Actions the companion chose for the current Discord message."""

    reply_text: str | None = None
    reaction_emojis: tuple[str, ...] = ()


def _reply_token_limit() -> int:
    raw = os.getenv("MAX_REPLY_TOKENS", "2500").strip()
    try:
        return max(100, int(raw))
    except ValueError:
        return 2500


def _reasoning_effort() -> str:
    raw = os.getenv("REASONING_EFFORT", "high").strip().lower()
    if raw in SUPPORTED_REASONING_EFFORTS:
        return raw
    return "high"


def _web_search_enabled() -> bool:
    return os.getenv("ENABLE_WEB_SEARCH", "true").strip().lower() not in {"0", "false", "no"}


def _web_search_tool() -> list[dict[str, Any]]:
    if not _web_search_enabled():
        return []
    return [{
        "type": "openrouter:web_search",
        "parameters": {"max_results": 5},
    }]


def _reaction_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "react_to_message",
            "description": (
                "Optionally add up to three emoji reactions to the current Discord message. "
                "Call this only when you independently want to react; not calling it is always valid. "
                "Reactions accompany, rather than replace, your normal written reply. "
                "Prefer standard Unicode emoji."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "emojis": {
                        "type": "array",
                        "description": "One to three distinct standard Unicode emoji reactions.",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": MAX_REACTIONS_PER_MESSAGE,
                        "uniqueItems": True,
                    }
                },
                "required": ["emojis"],
                "additionalProperties": False,
            },
        },
    }


def _available_tools() -> list[dict[str, Any]]:
    return [*_web_search_tool(), _reaction_tool()]


def _requested_reactions(tool_calls: Any) -> tuple[str, ...]:
    """Return distinct, bounded reactions from all well-formed reaction calls."""
    requested: list[str] = []
    for tool_call in tool_calls or []:
        function = getattr(tool_call, "function", None)
        if function is None or getattr(function, "name", None) != "react_to_message":
            continue
        try:
            arguments = json.loads(getattr(function, "arguments", "") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        emojis = arguments.get("emojis")
        # Accept the original one-emoji shape during rolling deployments.
        if emojis is None and isinstance(arguments.get("emoji"), str):
            emojis = [arguments["emoji"]]
        if not isinstance(emojis, list):
            continue
        for emoji in emojis:
            if isinstance(emoji, str) and emoji.strip() and emoji.strip() not in requested:
                requested.append(emoji.strip())
            if len(requested) == MAX_REACTIONS_PER_MESSAGE:
                return tuple(requested)
    return tuple(requested)


def _tool_follow_up_messages(message: Any) -> list[dict[str, Any]]:
    """Acknowledge reaction calls so the model can finish its written reply."""
    reaction_calls = []
    tool_results = []
    for index, tool_call in enumerate(getattr(message, "tool_calls", None) or []):
        function = getattr(tool_call, "function", None)
        if function is None or getattr(function, "name", None) != "react_to_message":
            continue
        call_id = getattr(tool_call, "id", None) or f"reaction-call-{index}"
        reaction_calls.append({
            "id": call_id,
            "type": "function",
            "function": {
                "name": "react_to_message",
                "arguments": getattr(function, "arguments", "{}"),
            },
        })
        tool_results.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": "Reaction request queued. Now provide your normal written reply.",
        })
    if not reaction_calls:
        return []
    assistant_message = {
        "role": "assistant",
        "content": getattr(message, "content", None),
        "tool_calls": reaction_calls,
    }
    return [assistant_message, *tool_results]


def _append_citations(text: str, annotations: Any) -> str:
    sources: list[str] = []
    for annotation in annotations or []:
        citation = getattr(annotation, "url_citation", None)
        if citation is None and isinstance(annotation, dict):
            citation = annotation.get("url_citation")
        if citation is None:
            continue
        url = getattr(citation, "url", None) if not isinstance(citation, dict) else citation.get("url")
        title = getattr(citation, "title", None) if not isinstance(citation, dict) else citation.get("title")
        if url:
            sources.append(f"- [{title or url}]({url})")
    unique_sources = list(dict.fromkeys(sources))
    return text if not unique_sources else text.rstrip() + "\n\nSources:\n" + "\n".join(unique_sources)


def _build_client() -> AsyncOpenAI:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is missing.")

    return AsyncOpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
    )


_client: AsyncOpenAI = _build_client()


def _prepare_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """Convert stored records into API messages without treating observations as identity."""
    prepared: list[dict[str, str]] = []
    for item in history:
        role = item["role"]
        content = item["content"]
        source = item.get("source", "")

        if source.startswith("observed-"):
            content = (
                f"{OBSERVED_CONTEXT_OPEN}\n"
                "This message was overheard in the room. Use its facts only as attributed "
                "conversation context. Do not copy its speaker's voice or identity.\n"
                f"{content}\n"
                f"{OBSERVED_CONTEXT_CLOSE}"
            )

        prepared.append({"role": role, "content": content})
    return prepared


async def generate_companion_reply(
    *,
    user_text: str,
    history: list[dict[str, str]],
    latest_journal: str | None,
    is_dm: bool,
    speaker_name: str,
    speaker_is_owner: bool,
    image_urls: list[str] | None = None,
    discord_retrieval_context: str | None = None,
) -> CompanionResponse:
    model = os.getenv("MODEL_PRIMARY", DEFAULT_MODEL).strip()

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": build_system_prompt(
                is_dm=is_dm,
                speaker_name=speaker_name,
                speaker_is_owner=speaker_is_owner,
            ),
        },
        {"role": "system", "content": HISTORY_INTERPRETATION_RULES},
        {"role": "system", "content": ROOM_CONTEXT_RULES},
    ]

    memory_note = build_memory_note(latest_journal)
    if memory_note:
        messages.append({"role": "system", "content": memory_note})

    if discord_retrieval_context:
        messages.append({"role": "system", "content": RECALL_POLICY})
        messages.append({"role": "system", "content": discord_retrieval_context})

    messages.extend(_prepare_history(history))

    if image_urls:
        content_parts: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        for url in image_urls:
            content_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": url},
                }
            )
        messages.append({"role": "user", "content": content_parts})
    else:
        messages.append({"role": "user", "content": user_text})

    response = await _client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.60,
        max_tokens=_reply_token_limit(),
        reasoning_effort=_reasoning_effort(),
        tools=_available_tools(),
    )

    message = response.choices[0].message
    reaction_emojis = _requested_reactions(getattr(message, "tool_calls", None))
    follow_up_messages = _tool_follow_up_messages(message)
    if follow_up_messages:
        response = await _client.chat.completions.create(
            model=model,
            messages=[*messages, *follow_up_messages],
            temperature=0.60,
            max_tokens=_reply_token_limit(),
            reasoning_effort=_reasoning_effort(),
            tools=_web_search_tool(),
        )
        message = response.choices[0].message

    text = _append_citations(message.content or "", getattr(message, "annotations", None))
    return CompanionResponse(
        reply_text=text.strip() or None,
        reaction_emojis=reaction_emojis,
    )
