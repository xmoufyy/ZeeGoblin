"""/profile - a member's private payout overview, straight from the ledger."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.branding import PURPLE, goblin_embed, sol, ts
from bot.helpers import bot_of, defer, reply
from database.models import AwardStatus as S


class ProfileCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="profile", description="Your wallet, tips and payout status")
    @app_commands.checks.cooldown(5, 30)
    async def profile(self, interaction: discord.Interaction) -> None:
        await defer(interaction)
        summary = await bot_of(interaction).services.tips.profile(interaction.user.id)
        totals = summary.by_status

        embed = goblin_embed(f"👑 {interaction.user.display_name}'s goblin file", colour=PURPLE)
        embed.add_field(name="Discord", value=f"{interaction.user.name}\n`{interaction.user.id}`")
        embed.add_field(
            name="SOL wallet",
            value=(
                f"`{summary.wallet}`\nsince {ts(summary.wallet_registered_at, 'd')}"
                if summary.wallet and summary.wallet_registered_at
                else "Not registered - use `/wallet set`"
            ),
            inline=False,
        )
        pending = totals[S.PENDING_REVIEW].lamports + totals[S.AWAITING_WALLET].lamports
        lines = [
            f"**Total earned:** {sol(summary.earned_lamports)}",
            f"🕵️ Pending review: {sol(pending)}"
            + (
                f" ({totals[S.AWAITING_WALLET].count} waiting on your wallet)"
                if totals[S.AWAITING_WALLET].count
                else ""
            ),
            f"✅ Approved: {sol(totals[S.APPROVED].lamports)}",
            f"📦 Queued for payment: {sol(totals[S.QUEUED].lamports)}",
            f"📨 Payment submitted: {sol(totals[S.PAYMENT_SUBMITTED].lamports)}",
            f"💰 Paid (manually confirmed by staff): {sol(summary.paid_manual_lamports)}",
        ]
        if summary.paid_other_lamports:
            lines.append(
                f"💰 Paid (settled via dispute review): {sol(summary.paid_other_lamports)}"
            )
        lines.append(f"⚠️ Disputed: {sol(totals[S.DISPUTED].lamports)}")
        embed.add_field(name="Tips", value="\n".join(lines), inline=False)
        embed.add_field(name="Roll calls attended", value=str(summary.rollcall_count))
        embed.add_field(
            name="Note",
            value="Paid amounts are confirmed manually by staff; on-chain verification is "
            "not enabled yet. See every award with `/tip history`.",
            inline=False,
        )
        await reply(interaction, embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ProfileCog(bot))
