from __future__ import annotations

import os
from typing import Any

from openai import AsyncOpenAI

from .identity import build_memory_note, build_system_prompt


def _build_client() -> AsyncOpenAI:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is missing.")

    return AsyncOpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
    )


async def generate_companion_reply(
    *,
    user_text: str,
    history: list[dict[str, str]],
    latest_journal: str | None,
    is_dm: bool,
) -> str:
    client = _build_client()
    model = os.getenv("MODEL_PRIMARY", "anthropic/claude-opus-4-6")

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(is_dm=is_dm)}
    ]

    memory_note = build_memory_note(latest_journal)
    if memory_note:
        messages.append({"role": "system", "content": memory_note})

    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    response = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.60,
    )

    text = response.choices[0].message.content or ""
    return text.strip() or "UNKNOWN. I lost the thread for a second."
