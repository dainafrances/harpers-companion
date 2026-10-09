"""A small, restart-safe controller for Discord's custom status activity."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Callable
import unicodedata

import discord


_LOGGER = logging.getLogger(__name__)
MAX_STATUS_CHARACTERS = 128


def status_cooldown_seconds(value: object) -> float:
    """Keep malformed cooldown settings from disabling all later status updates."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 300.0
    return seconds if math.isfinite(seconds) and 20 <= seconds <= 86400 else 300.0


def status_interval_hours(value: object) -> float:
    """Validate configured timer hours, falling back to three hours."""
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return 3.0
    return hours if math.isfinite(hours) and 0.25 <= hours <= 8760 else 3.0


def normalize_status_text(value: object) -> str:
    """Validate one status line; an empty string explicitly clears the status."""
    if not isinstance(value, str):
        raise ValueError("Discord status text must be a string.")
    if any(unicodedata.category(character) in {"Cc", "Cs", "Zl", "Zp"} for character in value):
        raise ValueError("Discord status text must be a single line without control characters.")
    text = value.strip()
    if len(text) > MAX_STATUS_CHARACTERS:
        raise ValueError(f"Discord status text must be at most {MAX_STATUS_CHARACTERS} characters.")
    return text


class StatusPresence:
    """Send optional status updates without letting failures interrupt a reply.

    Construction loads the saved status into ``bot.activity`` so the initial
    Gateway IDENTIFY (and later fresh sessions) includes it. Updates inside the
    cooldown are declined; there is no background queue or reconnect callback.
    """

    def __init__(
        self,
        bot: discord.Client,
        path: str | Path,
        cooldown_seconds: float = 300,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.bot = bot
        self.path = Path(path)
        self.cooldown_seconds = status_cooldown_seconds(cooldown_seconds)
        self._log_callback = log
        self._lock = asyncio.Lock()
        self._last_update_at: float | None = None
        self.current_text = ""
        self.setup()

    @staticmethod
    def _activity(text: str) -> discord.CustomActivity | None:
        return discord.CustomActivity(name=text) if text else None

    def _log(self, message: str) -> None:
        # Logs intentionally omit the status wording and exception details.
        try:
            if self._log_callback is not None:
                self._log_callback(message)
            else:
                _LOGGER.warning(message)
        except Exception:
            pass

    def setup(self) -> None:
        """Restore a validated saved status synchronously, before bot startup."""
        try:
            if self.path.stat().st_size > 4096:
                raise ValueError("Status file is too large.")
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or "text" not in saved:
                raise ValueError("Invalid status file.")
            text = normalize_status_text(saved["text"])
        except FileNotFoundError:
            return
        except (OSError, UnicodeError, ValueError):
            self._log("Discord status restore failed; saved status ignored.")
            return
        self.bot.activity = self._activity(text)
        self.current_text = text
        self._log("Discord profile status restored for the next connection.")

    def _persist(self, text: str) -> None:
        temporary_path: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as handle:
                temporary_path = handle.name
                json.dump({"text": text}, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        except (OSError, UnicodeError, ValueError):
            self._log("Discord status persistence failed; live status remains active.")
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

    async def update(self, text: object) -> bool:
        """Return True only when a new status was sent successfully to Discord."""
        try:
            normalized = normalize_status_text(text)
        except ValueError:
            self._log("Discord status update ignored: invalid text.")
            return False

        async with self._lock:
            if normalized == self.current_text:
                self._log("Discord profile status unchanged: duplicate proposal.")
                return False
            now = time.monotonic()
            if self._last_update_at is not None and now - self._last_update_at < self.cooldown_seconds:
                self._log("Discord profile status unchanged: cooldown active.")
                return False
            activity = self._activity(normalized)
            try:
                await self.bot.change_presence(activity=activity, status=self.bot.status)
            except Exception:
                self._log("Discord status update failed; previous status retained.")
                return False
            # Client.change_presence does not update Client.activity itself.
            # Keep the initial-presence value current for future fresh sessions.
            self.bot.activity = activity
            self.current_text = normalized
            self._last_update_at = time.monotonic()
            self._persist(normalized)
            self._log("Discord profile status update sent.")
            return True
