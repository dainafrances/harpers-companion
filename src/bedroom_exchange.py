"""Room-specific transport and mention handling; no prompt or memory changes."""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Callable

import aiohttp

GUILD_ID = 1489462985897279631
CHANNEL_ID = 1497494466712440912
MAX_TURNS = 3


class ExchangeStopped(Exception):
    """A stale or unavailable grant cannot produce an automated reply."""


def in_bedroom(message) -> bool:
    return (
        getattr(getattr(message, "guild", None), "id", None) == GUILD_ID
        and getattr(getattr(message, "channel", None), "id", None) == CHANNEL_ID
    )


@dataclass(frozen=True)
class PreparedReply:
    text: str
    peer_id: int | None = None
    grant: str | None = None
    send: bool = True


class ExchangeClient:
    def __init__(self, *, user: Callable, owner_id: int | None, peer_name: str,
                 transport=None, log=print):
        self.user = user
        self.owner_id = owner_id
        self.peer_name = peer_name
        self.transport = transport
        self.log = log
        self.enabled = os.getenv("BEDROOM_EXCHANGE_ENABLED", "false").lower() == "true"
        self.secret = os.getenv("BEDROOM_EXCHANGE_SECRET", "")
        self.url = os.getenv("BEDROOM_COORDINATOR_URL", "").rstrip("/")
        self.profile = None
        self.grants = {}
        self.last_observed_owner_id = 0

    async def call(self, operation, **fields):
        user = self.user()
        if not self.enabled or not user or len(self.secret) < 32:
            return {"ok": False, "reason": "not_configured"}
        payload = {"operation": operation, "actor_id": str(user.id), **fields}
        try:
            if self.transport is not None:
                return await self.transport(payload)
            if not self.url:
                return {"ok": False, "reason": "not_configured"}
            timeout = aiohttp.ClientTimeout(total=4)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self.url + "/internal/bedroom-exchange",
                    headers={"Authorization": "Bearer " + self.secret},
                    json=payload,
                ) as response:
                    if response.status != 200:
                        raise ValueError("coordinator_http_status")
                    return await response.json()
        except Exception:
            self.log("[bedroom] coordination_unavailable")
            return {"ok": False, "reason": "unavailable"}

    async def get_profile(self):
        if self.profile is not None:
            return self.profile
        result = await self.call("profile")
        if (
            result.get("ok") is True
            and self.owner_id
            and result.get("owner_id") == str(self.owner_id)
            and result.get("peer_id")
        ):
            self.profile = result
            return result
        return None

    async def observe_owner(self, message):
        if (in_bedroom(message) and not message.author.bot
                and self.owner_id and message.author.id == self.owner_id):
            if await self.get_profile() is not None:
                result = await self.call("observe", message_id=str(message.id))
                if result.get("ok") and message.id > self.last_observed_owner_id:
                    self.last_observed_owner_id = message.id
                    self.grants.clear()
                    if result.get("reset"):
                        self.log("[bedroom] allowance_reset")

    async def admit(self, message) -> bool:
        """Only a direct, published tag from the exact peer can spend a turn."""
        profile = await self.get_profile()
        user = self.user()
        if not profile or not user:
            return False
        peer_id = int(profile["peer_id"])
        if (
            not in_bedroom(message) or not message.author.bot
            or message.author.id != peer_id
            or not any(mention.id == user.id for mention in message.mentions)
            or not re.search(r"<@!?" + str(user.id) + r">", message.content or "")
            or (message.content or "").lstrip().startswith("!")
        ):
            return False
        # Discord may deliver the ping before the sender's publish RPC returns.
        for attempt in range(10):
            result = await self.call("reply", message_id=str(message.id))
            if result.get("ok"):
                self.grants[message.id] = result["grant"]
                self.log("[bedroom] turn_admitted turn=" + str(result["turn"]))
                return True
            if result.get("reason") != "not_published":
                self.log("[bedroom] trigger_blocked reason=" + str(result.get("reason")))
                return False
            await asyncio.sleep(0.1 + min(attempt, 3) * 0.05)
        self.log("[bedroom] trigger_blocked reason=not_published")
        return False

    def plain_text(self, text, peer_id=None):
        aliases = (
            ("ben morgan", "benedict", "benji", "ben")
            if self.peer_name == "Ben" else ("colin", "moose")
        )
        names = "|".join(re.escape(alias) for alias in aliases)
        pattern = (
            r"\[PING:\s*(?:" + names + r")\s*\]"
            r"|@(?:" + names + r")(?:#\d{4})?\b"
        )
        if peer_id:
            pattern += r"|<@!?" + str(peer_id) + r">"
        intended = re.search(pattern, text, re.IGNORECASE) is not None
        plain = re.sub(pattern, self.peer_name, text, flags=re.IGNORECASE)
        plain = re.sub(r"\[PING:\s*[^\]]+\]", "", plain, flags=re.IGNORECASE)
        return plain.strip(), intended

    async def prepare(self, message, text):
        if not in_bedroom(message):
            return None
        profile = await self.get_profile()
        peer_id = int(profile["peer_id"]) if profile else None
        plain, intended = self.plain_text(text, peer_id)
        grant = self.grants.pop(message.id, None)
        if message.author.bot:
            # No coordination means no automated reply, including after a restart.
            if not grant:
                return PreparedReply(plain, send=False)
        elif not (intended and profile and message.author.id == self.owner_id):
            return PreparedReply(plain)
        else:
            result = await self.call("start", message_id=str(message.id))
            if not result.get("ok"):
                return PreparedReply(plain)
            grant = result["grant"]

        status = await self.call("status", grant=grant)
        if not status.get("ok"):
            # A human's normal text reply survives an unavailable coordinator.
            return PreparedReply(plain, send=not message.author.bot)
        ping = intended and status.get("can_ping") is True
        # Put the single ping last so it cannot interrupt a multi-part reply.
        rendered = plain + ("\n\n<@" + str(peer_id) + ">" if ping else "")
        return PreparedReply(rendered, peer_id if ping else None, grant)

    async def publish(self, prepared, sent_ids):
        if prepared.grant and sent_ids:
            result = await self.call(
                "publish", grant=prepared.grant,
                message_ids=[str(message_id) for message_id in sent_ids],
                handoff=prepared.peer_id is not None,
            )
            if not result.get("ok"):
                self.log("[bedroom] publish_failed; exchange_stops")
