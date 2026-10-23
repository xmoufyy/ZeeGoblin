"""Confirm/cancel prompts and simple pagination, restricted to the invoking user."""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable

import discord

from bot.helpers import reply


class _OwnedView(discord.ui.View):
    def __init__(self, owner_id: int, *, timeout: float) -> None:
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "🧌 Hands off, that button isn't yours.", ephemeral=True
            )
            return False
        return True


class ConfirmationView(_OwnedView):
    def __init__(self, owner_id: int, *, confirm_label: str, danger: bool, timeout: float = 60):
        super().__init__(owner_id, timeout=timeout)
        self.confirmed: bool | None = None
        self.confirm_button.label = confirm_label
        self.confirm_button.style = (
            discord.ButtonStyle.danger if danger else discord.ButtonStyle.success
        )

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.confirmed = True
        await interaction.response.edit_message(content="⏳ Confirmed - working on it…", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.confirmed = False
        await interaction.response.edit_message(content="❎ Cancelled. Nothing changed.", view=None)
        self.stop()


async def confirm(
    interaction: discord.Interaction,
    embed: discord.Embed,
    *,
    confirm_label: str = "Confirm",
    danger: bool = False,
) -> bool:
    """Show a confirmation prompt and wait. Returns True only on an explicit confirm."""
    view = ConfirmationView(interaction.user.id, confirm_label=confirm_label, danger=danger)
    message = await reply(interaction, embed=embed, view=view)
    timed_out = await view.wait()
    if timed_out and message is not None:
        with contextlib.suppress(discord.HTTPException):
            await message.edit(content="⌛ Timed out. Nothing changed.", view=None)
    return bool(view.confirmed)


Renderer = Callable[[int], Awaitable[tuple[discord.Embed, int]]]
# Returns plain text for a page (e.g. wallet addresses) sent privately for easy copying.
CopyText = Callable[[int], Awaitable[str]]


class Paginator(_OwnedView):
    def __init__(
        self,
        owner_id: int,
        render: Renderer,
        page: int,
        pages: int,
        *,
        copy_text: CopyText | None = None,
        copy_label: str = "📋 Copy",
    ) -> None:
        super().__init__(owner_id, timeout=300)
        self.render = render
        self.page = page
        self.pages = pages
        if pages <= 1:
            self.remove_item(self.previous_button)
            self.remove_item(self.next_button)
        if copy_text is not None:
            button: discord.ui.Button = discord.ui.Button(
                label=copy_label, style=discord.ButtonStyle.primary
            )

            async def send_copy(interaction: discord.Interaction) -> None:
                text = await copy_text(self.page)
                # Plain text, no markdown: long-press / "Copy Text" copies exactly this.
                await interaction.response.send_message(text[:2000], ephemeral=True)

            button.callback = send_copy
            self.add_item(button)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.previous_button.disabled = self.page <= 1
        self.next_button.disabled = self.page >= self.pages

    async def _show(self, interaction: discord.Interaction, page: int) -> None:
        embed, self.pages = await self.render(page)
        self.page = min(page, self.pages)
        self._sync_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def previous_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._show(interaction, self.page - 1)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._show(interaction, self.page + 1)


async def send_paginated(
    interaction: discord.Interaction,
    render: Renderer,
    page: int = 1,
    *,
    copy_text: CopyText | None = None,
    copy_label: str = "📋 Copy",
) -> None:
    embed, pages = await render(page)
    view = None
    if pages > 1 or copy_text is not None:
        view = Paginator(
            interaction.user.id, render, page, pages, copy_text=copy_text, copy_label=copy_label
        )
    await reply(interaction, embed=embed, view=view)
