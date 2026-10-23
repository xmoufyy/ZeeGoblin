"""/report tips (end-of-stream summary) and /help (only shows what you can use)."""

from __future__ import annotations

from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from bot.autocomplete import rollcall_choices
from bot.branding import GOLD, PURPLE, goblin_embed, sol, status_text
from bot.helpers import actor_of, bot_of, defer, reply
from bot.views.confirmation_view import OwnedView
from config.permissions import Actor, Permission, require
from database.models import AwardStatus

PERIODS = {
    "stream": ("Last 12 hours", timedelta(hours=12)),
    "day": ("Last 24 hours", timedelta(hours=24)),
    "week": ("Last 7 days", timedelta(days=7)),
    "month": ("Last 30 days", timedelta(days=30)),
    "all": ("All time", None),
}


class ReportCog(commands.GroupCog, group_name="report", group_description="Staff reports"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="tips", description="End-of-stream tip summary for a period")
    @app_commands.describe(
        period="Time window (default: last 12 hours)", event_id="Only this roll call"
    )
    @app_commands.choices(
        period=[app_commands.Choice(name=label, value=key) for key, (label, _) in PERIODS.items()]
    )
    @app_commands.autocomplete(event_id=rollcall_choices)
    async def tips(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str] | None = None,
        event_id: int | None = None,
    ) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.AWARD_VIEW_QUEUE)
        await defer(interaction)
        key = period.value if period else ("all" if event_id else "stream")
        label, window = PERIODS[key]
        report = await bot_of(interaction).services.tips.report(
            actor, window=window, event_id=event_id
        )

        scope = f"roll call #{event_id}" if event_id else label.lower()
        created = report.created
        embed = goblin_embed(f"📊 Tip report · {scope}", colour=GOLD)
        embed.add_field(name="Awards created", value=f"**{report.created_count}**")
        embed.add_field(name="SOL awarded", value=f"**{sol(report.awarded_lamports)}**")
        embed.add_field(
            name="Payments confirmed",
            value=f"{report.confirmed_payments} · {sol(report.confirmed_lamports)}",
        )
        embed.add_field(
            name="Where those awards are now",
            value="\n".join(
                f"{status_text(status)}: {created[status].count} · {sol(created[status].lamports)}"
                for status in AwardStatus
                if created[status].count
            )
            or "Nothing in this window.",
            inline=False,
        )
        embed.add_field(
            name="🏦 Outstanding (whole ledger)",
            value=f"**{sol(report.outstanding_lamports)}** not yet paid or voided",
            inline=False,
        )
        embed.set_footer(text="Paid = manually confirmed by staff, not blockchain-verified")
        await reply(interaction, embed=embed)


# (category, required permission or None, [(command, what it does / example)])
HELP: list[tuple[str, Permission | None, list[tuple[str, str]]]] = [
    (
        "👑 Members",
        None,
        [
            ("I'M HERE! button", "Check in when Zee starts a roll call."),
            ("/profile", "Your wallet, tip totals and roll-call count."),
            ("/tip history", "Every tip you've earned and where it is in the payout process."),
            ("/rank · /leaderboard", "Your goblin rank and the top roll-call regulars."),
            ("/roast member:", "Let ZeeGoblin roast someone (all in fun)."),
            ("/notifications dms:off", "Stop DMs from ZeeGoblin. Turn back on any time."),
            ("/solpump", "Zee's Solpump link."),
        ],
    ),
    (
        "👛 Wallet",
        None,
        [
            (
                "/wallet set address:",
                "Register your PUBLIC SOL address, e.g. `/wallet set address:7Kx…`."
                " Never share a seed phrase or private key.",
            ),
            ("/wallet view", "See the wallet on file and recent changes."),
            ("/wallet remove", "Remove it. Approved tips keep their locked payout address."),
        ],
    ),
    (
        "🔴 Roll calls",
        Permission.ROLLCALL_START,
        [
            (
                "/rollcall start",
                "Post a roll call here. e.g. `/rollcall start duration:3 reward:0.01`.",
            ),
            ("/rollcall status · history", "The running roll call / past ones."),
            ("/rollcall end", "Head mods: close early, or `cancel:True` for no awards."),
        ],
    ),
    (
        "🕵️ Moderator tools",
        Permission.AWARD_VIEW_QUEUE,
        [
            ("/staff", "Everything waiting on staff, with one-click buttons. Start here."),
            ("/tip list", "Award queue. Head mods approve from the menu (with a summary first)."),
            (
                "/tip view award_id:",
                "One award with Approve / Void / Flag / Member's tips buttons.",
            ),
            ("/tip add user: amount: reason:", "Request a manual award, e.g. `amount:0.05`."),
            ("/tip flag · /dispute list · resolve", "Send an award to review / handle disputes."),
            ("/report tips period:", "End-of-stream summary (default: last 12 hours)."),
        ],
    ),
    (
        "💸 Payment tools",
        Permission.BATCH_EXPORT,
        [
            ("/tip export-batch", "Reserve approved tips into a batch and get the CSVs."),
            (
                "/batch view batch_id:",
                "Pay from menus: pick a wallet, paste the signature, confirm.",
            ),
            ("/payment history", "Recorded payments with Solscan links."),
            ("/batch cancel", "Release unpaid awards if a batch won't be paid."),
        ],
    ),
    (
        "🛠️ Admin",
        Permission.AUDIT_VIEW,
        [
            ("/admin audit", "Append-only audit trail, filterable by record or user."),
            ("/admin presence", "Owner: change the bot's status line."),
            ("/admin whoami", "Check which ZeeGoblin roles you have."),
        ],
    ),
]


def help_sections(actor: Actor) -> list[tuple[str, list[tuple[str, str]]]]:
    return [(name, items) for name, perm, items in HELP if perm is None or actor.can(perm)]


def help_embed(name: str, items: list[tuple[str, str]]) -> discord.Embed:
    embed = goblin_embed(f"🧌 ZeeGoblin help · {name}", colour=PURPLE)
    for command, text in items:
        embed.add_field(name=command, value=text, inline=False)
    return embed


class HelpView(OwnedView):
    def __init__(self, owner_id: int, sections: list[tuple[str, list[tuple[str, str]]]]) -> None:
        super().__init__(owner_id, timeout=300)
        self.sections = dict(sections)
        menu: discord.ui.Select = discord.ui.Select(
            placeholder="Pick a category…",
            options=[discord.SelectOption(label=name, value=name) for name, _ in sections],
        )

        async def show(interaction: discord.Interaction) -> None:
            name = menu.values[0]
            await interaction.response.edit_message(embed=help_embed(name, self.sections[name]))

        menu.callback = show
        self.add_item(menu)


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="What ZeeGoblin can do for you")
    async def help(self, interaction: discord.Interaction) -> None:
        sections = help_sections(actor_of(interaction))
        name, items = sections[0]
        view = HelpView(interaction.user.id, sections) if len(sections) > 1 else None
        await reply(interaction, embed=help_embed(name, items), view=view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ReportCog(bot))
    await bot.add_cog(HelpCog(bot))
