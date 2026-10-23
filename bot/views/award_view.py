"""Quick actions under an award (/tip view): approve, void, flag, member history.

Buttons are shown only when the viewer's role and the award's status allow the action, and
every click re-checks both through the services, so a stale message can't do anything wrong.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord

from bot.events import guarded
from bot.helpers import actor_of, bot_of
from bot.views.confirmation_view import OwnedView
from config.permissions import Actor, Permission
from database.models import AwardStatus
from services.award_states import can_transition
from services.tip_service import AwardRow

EmbedFor = Callable[[AwardRow], discord.Embed]
ShowHistory = Callable[[discord.Interaction, int], Awaitable[None]]


class ReasonModal(discord.ui.Modal):
    def __init__(
        self, title: str, on_reason: Callable[[discord.Interaction, str], Awaitable[None]]
    ):
        super().__init__(title=title[:45], timeout=300)
        self.reason: discord.ui.TextInput = discord.ui.TextInput(
            min_length=1, max_length=300, style=discord.TextStyle.paragraph
        )
        self.add_item(
            discord.ui.Label(text="Reason (kept in the audit log)", component=self.reason)
        )
        self._on_reason = on_reason

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await guarded(interaction, self._on_reason(interaction, self.reason.value))


class AwardActionsView(OwnedView):
    def __init__(
        self,
        owner_id: int,
        row: AwardRow,
        actor: Actor,
        *,
        embed_for: EmbedFor,
        show_history: ShowHistory,
    ) -> None:
        super().__init__(owner_id, timeout=600)
        self.award_id = row.id
        self.embed_for = embed_for
        self.show_history = show_history
        self._build(row, actor)

    def _button(self, label: str, style: discord.ButtonStyle, action) -> None:  # type: ignore[no-untyped-def]
        button: discord.ui.Button = discord.ui.Button(label=label, style=style)
        button.callback = action
        self.add_item(button)

    def _build(self, row: AwardRow, actor: Actor) -> None:
        self.clear_items()
        status = row.status
        if actor.can(Permission.AWARD_APPROVE) and status == AwardStatus.PENDING_REVIEW:
            self._button("✅ Approve", discord.ButtonStyle.success, self._approve)
        if actor.can(Permission.AWARD_VOID) and can_transition(status, AwardStatus.VOIDED):
            self._button("🚫 Void", discord.ButtonStyle.danger, self._void)
        if (
            actor.can(Permission.AWARD_FLAG)
            and status != AwardStatus.QUEUED
            and can_transition(status, AwardStatus.DISPUTED)
        ):
            self._button("⚠️ Flag", discord.ButtonStyle.secondary, self._flag)
        if actor.can(Permission.TIP_HISTORY_ANY):
            self._button("📜 Member's tips", discord.ButtonStyle.secondary, self._history)

    async def _refresh(self, interaction: discord.Interaction, row: AwardRow) -> None:
        self._build(row, actor_of(interaction))
        if self.message is not None:
            await self.message.edit(embed=self.embed_for(row), view=self if self.children else None)

    async def _approve(self, interaction: discord.Interaction) -> None:
        async def run() -> None:
            await interaction.response.defer()
            row = await bot_of(interaction).services.tips.approve(
                actor_of(interaction), self.award_id
            )
            await self._refresh(interaction, row)

        await guarded(interaction, run())

    async def _void(self, interaction: discord.Interaction) -> None:
        async def on_reason(modal_interaction: discord.Interaction, reason: str) -> None:
            await modal_interaction.response.defer()
            row = await bot_of(modal_interaction).services.tips.void(
                actor_of(modal_interaction), self.award_id, reason
            )
            await self._refresh(modal_interaction, row)

        await interaction.response.send_modal(
            ReasonModal(f"Void award #{self.award_id}", on_reason)
        )

    async def _flag(self, interaction: discord.Interaction) -> None:
        async def on_reason(modal_interaction: discord.Interaction, reason: str) -> None:
            await modal_interaction.response.defer()
            tips = bot_of(modal_interaction).services.tips
            actor = actor_of(modal_interaction)
            await tips.flag(actor, self.award_id, reason)
            await self._refresh(modal_interaction, await tips.get(actor, self.award_id))

        await interaction.response.send_modal(
            ReasonModal(f"Flag award #{self.award_id}", on_reason)
        )

    async def _history(self, interaction: discord.Interaction) -> None:
        async def run() -> None:
            row = await bot_of(interaction).services.tips.get(actor_of(interaction), self.award_id)
            await self.show_history(interaction, row.discord_user_id)

        await guarded(interaction, run())
