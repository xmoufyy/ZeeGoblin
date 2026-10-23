"""Pending-award list with approve controls: pick awards from a menu, or approve the page."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord

from bot.branding import sol
from bot.views.confirmation_view import CopyText, Paginator, Renderer
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
