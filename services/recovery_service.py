"""Startup reconciliation after a crash, restart, or long disconnect."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from database.models import Event
from services.rollcall_service import FinalizeResult, RollCallService

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryReport:
    finalized: list[FinalizeResult]
    still_active: list[Event]
    needs_discord_sync: list[Event]


class RecoveryService:
    def __init__(self, rollcalls: RollCallService) -> None:
        self._rollcalls = rollcalls

    async def recover(self) -> RecoveryReport:
        """Finalize roll calls that expired while offline and list follow-up work.

        Safe to run any number of times: finalization is idempotent and transactional, so an
        interrupted finalization simply never committed and is redone here.
        """
        finalized = await self._rollcalls.finalize_due()
        active = await self._rollcalls.active_events()
        unsynced = await self._rollcalls.unsynced_closed_events()
        log.info(
            "Recovery: finalized %d expired roll call(s), %d still active, %d need Discord sync",
            len(finalized),
            len(active),
            len(unsynced),
        )
        return RecoveryReport(finalized, active, unsynced)
