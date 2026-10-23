"""Pending-award list with approve controls: pick awards from a menu, or approve the page."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

import discord

from bot.branding import Quips, sol
from bot.events import guarded
from bot.helpers import actor_of, bot_of
from bot.views.confirmation_view import CopyText, Paginator, Renderer
from database.models import Event, EventStatus
from services.tip_service import AwardRow

# Approves the given award IDs as the clicking user; returns a summary to show them.
Approver = Callable[[discord.Interaction, list[int]], Awaitable[str]]


class ApprovalQueueView(Paginator):
    def __init__(
        self,
        owner_id: int,
        render: Renderer,
        page: int,
        pages: int,
        *,
        current_rows: Callable[[], list[AwardRow]],
        approve: Approver,
        copy_text: CopyText | None = None,
        copy_label: str = "📋 Copy",
    ) -> None:
        self._current_rows = current_rows
        self._approve = approve
        self.picker: discord.ui.Select = discord.ui.Select(
            placeholder="✅ Approve selected awards…", min_values=1, row=1
        )
        self.picker.callback = self._on_pick
        self.approve_page: discord.ui.Button = discord.ui.Button(
            label="✅ Approve all on page", style=discord.ButtonStyle.success, row=2
        )
        self.approve_page.callback = self._on_approve_page
        super().__init__(owner_id, render, page, pages, copy_text=copy_text, copy_label=copy_label)

    def _sync_buttons(self) -> None:
        super()._sync_buttons()
        rows = self._current_rows()
        # Discord rejects an empty select menu, so it's only shown while there's something to pick.
        for item in (self.picker, self.approve_page):
            if item in self.children:
                self.remove_item(item)
        if not rows:
            return
        self.picker.options = [
            discord.SelectOption(
                label=f"#{row.id} · {row.username} · {sol(row.amount_lamports)}"[:100],
                description=row.reason[:100],
                value=str(row.id),
            )
            for row in rows[:25]
        ]
        self.picker.max_values = len(self.picker.options)
        self.add_item(self.picker)
        self.add_item(self.approve_page)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, [int(value) for value in self.picker.values])

    async def _on_approve_page(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, [row.id for row in self._current_rows()])

    async def _run(self, interaction: discord.Interaction, award_ids: list[int]) -> None:
        summary = await self._approve(interaction, award_ids)
        await self._show(interaction, self.page)  # re-render: approved awards leave the list
        await interaction.followup.send(summary, ephemeral=True)


class ApproveEventButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"zeegoblin:approve-event:(?P<event_id>[0-9]+)",
):
    """Persistent "Approve all" on a roll call's staff summary; survives restarts."""

    def __init__(self, event_id: int, pending: int | None = None) -> None:
        label = f"✅ Approve all ({pending})" if pending else "✅ Approve all pending"
        super().__init__(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.success,
                custom_id=f"zeegoblin:approve-event:{event_id}",
            )
        )
        self.event_id = event_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ) -> ApproveEventButton:
        return cls(int(match["event_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != bot_of(interaction).settings.discord_guild_id:
            return
        await guarded(interaction, self._approve(interaction))

    async def _approve(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await bot_of(interaction).services.tips.approve_event(
            actor_of(interaction), self.event_id
        )
        if not (result.approved or result.skipped_own or result.skipped_no_wallet):
            text = f"Nothing left to approve for roll call #{self.event_id}. 🧌"
        else:
            text = (
                f"{Quips.APPROVED}\nApproved **{result.approved}** award(s) totalling "
                f"**{sol(result.total_lamports)}** from roll call #{self.event_id}."
            )
            if result.skipped_own:
                text += f"\nSkipped {result.skipped_own} of your own."
            if result.skipped_no_wallet:
                text += f"\nSkipped {result.skipped_no_wallet} with no wallet."
        await interaction.followup.send(text, ephemeral=True)


def approve_event_view(event: Event) -> discord.ui.View | None:
    pending = event.award_count - event.awaiting_wallet_count
    if event.status != EventStatus.CLOSED or pending <= 0:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(ApproveEventButton(event.id, pending))
    return view
