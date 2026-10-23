"""/wallet set | view | remove - always ephemeral."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.branding import GOLD, ORANGE, PURPLE, Quips, goblin_embed, ts
from bot.helpers import bot_of, defer, identity_of, reply
from bot.views.confirmation_view import confirm
from utils.validation import shorten_wallet

OWNERSHIP_NOTE = (
    "Registering only tells ZeeGoblin where to send tips - it does **not** prove you own the "
    "wallet, so double-check it. Staff will **never** ask for your seed phrase or private key."
)


class WalletCog(commands.GroupCog, group_name="wallet", group_description="Your SOL payout wallet"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="set", description="Register or change your public SOL wallet")
    @app_commands.describe(address="Your PUBLIC Solana wallet address (never a seed phrase)")
    @app_commands.checks.cooldown(2, 60, key=lambda i: i.user.id)
    async def set_wallet(self, interaction: discord.Interaction, address: str) -> None:
        await defer(interaction)
        change = await bot_of(interaction).services.wallets.set_wallet(
            identity_of(interaction.user), address
        )
        embed = goblin_embed(
            Quips.WALLET_SET, f"`{change.address}`\n\n{OWNERSHIP_NOTE}", colour=GOLD
        )
        if change.previous_address:
            embed.add_field(name="Replaced", value=f"`{shorten_wallet(change.previous_address)}`")
        if change.awards_now_reviewable:
            embed.add_field(
                name="Awards unlocked",
                value=f"{change.awards_now_reviewable} award(s) waiting on a wallet are now "
                "pending staff review.",
                inline=False,
            )
        if change.awards_locked_to_previous:
            embed.add_field(
                name="⚠️ Already-approved awards",
                value=f"{change.awards_locked_to_previous} approved award(s) will still be paid "
                "to the wallet that was registered when they were approved. If that wallet is "
                "lost, ask a head moderator to review them.",
                inline=False,
            )
        await reply(interaction, embed=embed)

    @app_commands.command(name="view", description="See the wallet ZeeGoblin has on file for you")
    @app_commands.checks.cooldown(5, 30)
    async def view_wallet(self, interaction: discord.Interaction) -> None:
        await defer(interaction)
        services = bot_of(interaction).services
        wallet = await services.wallets.get_wallet(interaction.user.id)
        if wallet is None:
            await reply(
                interaction,
                embed=goblin_embed(
                    "👛 No wallet registered", Quips.CHECKIN_NO_WALLET, colour=ORANGE
                ),
            )
            return
        embed = goblin_embed("👛 Your payout wallet", f"`{wallet.address}`", colour=PURPLE)
        embed.add_field(name="Registered", value=ts(wallet.registered_at))
        history = await services.wallets.history(interaction.user.id, limit=5)
        if len(history) > 1:
            embed.add_field(
                name="Recent changes",
                value="\n".join(
                    f"{ts(h.created_at, 'd')} {h.action}: "
                    f"`{shorten_wallet(h.old_address)}` → `{shorten_wallet(h.new_address)}`"
                    for h in history
                ),
                inline=False,
            )
        embed.add_field(name="Reminder", value=OWNERSHIP_NOTE, inline=False)
        await reply(interaction, embed=embed)

    @app_commands.command(name="remove", description="Remove your registered wallet")
    @app_commands.checks.cooldown(5, 30)
    async def remove_wallet(self, interaction: discord.Interaction) -> None:
        await defer(interaction)
        services = bot_of(interaction).services
        if await services.wallets.get_wallet(interaction.user.id) is None:
            await reply(interaction, "You don't have a wallet registered.")
            return
        prompt = goblin_embed(
            "Remove your wallet?",
            "Unapproved awards will wait until you register a new wallet. Awards that were "
            "already approved keep their approved payout address.",
            colour=ORANGE,
        )
        if not await confirm(interaction, prompt, confirm_label="Remove wallet", danger=True):
            return
        removal = await services.wallets.remove_wallet(identity_of(interaction.user))
        text = f"Removed `{shorten_wallet(removal.address)}`."
        if removal.awards_back_to_awaiting_wallet:
            text += (
                f"\n{removal.awards_back_to_awaiting_wallet} pending award(s) now wait "
                "for a new wallet."
            )
        await reply(interaction, embed=goblin_embed("👛 Wallet removed", text, colour=ORANGE))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WalletCog(bot))
