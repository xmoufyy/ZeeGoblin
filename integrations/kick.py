"""Kick integration - Phase 2 interface only.

Planned capabilities: OAuth account linking (``user:read``), Kick-to-Discord identity
mapping onto :class:`database.models.User`, ``chat.message.sent`` webhook collection, stream
call rounds, winner confirmation, and stream awards created through ``TipService`` with a new
``AwardSource``. Every method here raises :class:`KickUnavailable` until then.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class KickUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Kick integration is not available yet (planned for Phase 2).")


@dataclass(frozen=True)
class KickChatMessage:
    kick_user_id: int
    kick_username: str
    content: str
    sent_at: datetime


class KickIntegration(Protocol):
    async def authorization_url(self, discord_user_id: int) -> str: ...

    async def complete_link(self, discord_user_id: int, code: str, state: str) -> str: ...

    async def handle_chat_webhook(self, body: bytes, signature: str) -> KickChatMessage | None: ...


class UnavailableKickIntegration:
    async def authorization_url(self, discord_user_id: int) -> str:
        raise KickUnavailable()

    async def complete_link(self, discord_user_id: int, code: str, state: str) -> str:
        raise KickUnavailable()

    async def handle_chat_webhook(self, body: bytes, signature: str) -> KickChatMessage | None:
        raise KickUnavailable()
