"""/staff - one dashboard showing what needs doing, with buttons instead of commands to remember.

The buttons run the same handlers as the slash commands, so permissions, confirmations and
error messages behave identically.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from bot.branding import GOLD, sol, ts
from bot.events import guarded
from bot.helpers import actor_of, bot_of, defer, reply
from bot.views.confirmation_view import OwnedView
from config.permissions import Actor, Permission, require
from database.models import AwardStatus, BatchStatus, PaymentStatus

Action = Callable[[discord.Interaction], Awaitable[None]]


async def build_dashboard(
    bot: commands.Bot, actor: Actor, guild_id: int | None
) -> tuple[discord.Embed, list[tuple[str, discord.ButtonStyle, Action, bool]]]:
    """The dashboard embed plus (label, style, action, disabled) for each button."""
    services = bot.services  # type: ignore[attr-defined]
    ledger = await services.tips.ledger_summary(actor)
    totals = ledger.by_status
    pending, awaiting = totals[AwardStatus.PENDING_REVIEW], totals[AwardStatus.AWAITING_WALLET]
    approved = totals[AwardStatus.APPROVED]

    embed = discord.Embed(title="👑 Goblin control room", colour=GOLD)
    active = await services.rollcalls.active_events(guild_id)
    if active:
        event = active[0]
        embed.add_field(
            name="🔴 Roll call running",
            value=f"#{event.id} · 👥 {event.entry_count} · ends {ts(event.expires_at, 'R')}",
            inline=False,
        )
    embed.add_field(name="🕵️ Pending review", value=f"**{pending.count}** · {sol(pending.lamports)}")
    embed.add_field(
        name="👛 Missing wallet", value=f"**{awaiting.count}** · {sol(awaiting.lamports)}"
    )
    embed.add_field(
        name="✅ Approved, unbatched", value=f"**{approved.count}** · {sol(approved.lamports)}"
    )
    embed.add_field(name="⚠️ Open disputes", value=f"**{ledger.open_disputes}**")

    tips = bot.get_cog("TipCog")
    disputes = bot.get_cog("DisputeCog")
    batches_cog = bot.get_cog("BatchCog")

    def tip_list(status: AwardStatus) -> Action:
        choice = app_commands.Choice(name=status.value, value=status.value)
        return lambda i: tips.list_awards.callback(tips, i, choice, None, 1)  # type: ignore[union-attr]

    buttons: list[tuple[str, discord.ButtonStyle, Action, bool]] = [
        (
            f"🕵️ Review pending ({pending.count})",
            discord.ButtonStyle.primary,
            tip_list(AwardStatus.PENDING_REVIEW),
            pending.count == 0,
        ),
        (
            f"👛 Missing wallets ({awaiting.count})",
            discord.ButtonStyle.secondary,
            tip_list(AwardStatus.AWAITING_WALLET),
            awaiting.count == 0,
        ),
        (
            f"⚠️ Disputes ({ledger.open_disputes})",
            discord.ButtonStyle.secondary,
            lambda i: disputes.list_disputes.callback(disputes, i, False, 1),  # type: ignore[union-attr]
            ledger.open_disputes == 0,
        ),
    ]
    if actor.can(Permission.BATCH_EXPORT):
        buttons.append(
            (
                f"📦 Export batch ({approved.count})",
                discord.ButtonStyle.success,
                lambda i: tips.export_batch.callback(tips, i, None),  # type: ignore[union-attr]
                approved.count == 0,
            )
        )
    if actor.can(Permission.BATCH_VIEW):
        open_batches = await services.payments.list_batches(
            actor, status=BatchStatus.OPEN, page_size=3
        )
        if open_batches.total:
            embed.add_field(
                name="📦 Open batches",
                value="\n".join(
                    f"#{b.id} · {b.award_count} award(s) · {sol(b.total_lamports)}"
                    for b in open_batches.items
                )
                + ("" if open_batches.total <= 3 else f"\n…and {open_batches.total - 3} more"),
                inline=False,
            )
        for batch in open_batches.items:
            buttons.append(
                (
                    f"💸 Pay batch #{batch.id}",
                    discord.ButtonStyle.success,
                    (
                        lambda batch_id: (
                            lambda i: batches_cog.view.callback(  # type: ignore[union-attr]
                                batches_cog, i, batch_id
                            )
                        )
                    )(batch.id),
                    False,
                )
            )
    if actor.can(Permission.PAYMENT_VIEW):
        unconfirmed = await services.payments.payment_history(
            actor, status=PaymentStatus.SUBMITTED, page_size=1
        )
        if unconfirmed.total:
            embed.add_field(
                name="📨 Payments to confirm",
                value=f"**{unconfirmed.total}** (open the batch to confirm them)",
                inline=False,
            )
    embed.set_footer(text="Buttons work for 10 minutes • only you can use them")
    return embed, buttons


class DashboardView(OwnedView):
    def __init__(self, bot: commands.Bot, owner_id: int, buttons) -> None:  # type: ignore[no-untyped-def]
        super().__init__(owner_id, timeout=600)
        self.bot = bot
        for label, style, action, disabled in buttons:
            button: discord.ui.Button = discord.ui.Button(
                label=label, style=style, disabled=disabled
            )
            button.callback = (lambda run: lambda i: guarded(i, run(i)))(action)
            self.add_item(button)
        refresh: discord.ui.Button = discord.ui.Button(
            label="🔄 Refresh", style=discord.ButtonStyle.secondary
        )
        refresh.callback = self._refresh
        self.add_item(refresh)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        async def run() -> None:
            embed, buttons = await build_dashboard(
                self.bot, actor_of(interaction), interaction.guild_id
            )
            await interaction.response.edit_message(
                embed=embed, view=DashboardView(self.bot, interaction.user.id, buttons)
            )

        await guarded(interaction, run())


class StaffCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="staff", description="Staff dashboard: what needs doing, one click")
    async def staff(self, interaction: discord.Interaction) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.AWARD_VIEW_QUEUE)
        await defer(interaction)
        embed, buttons = await build_dashboard(bot_of(interaction), actor, interaction.guild_id)
        await reply(
            interaction, embed=embed, view=DashboardView(self.bot, interaction.user.id, buttons)
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StaffCog(bot))
