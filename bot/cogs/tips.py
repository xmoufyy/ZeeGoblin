"""/tip ... and /dispute ... - the tip ledger in Discord."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.autocomplete import open_dispute_choices, rollcall_choices
from bot.branding import (
    GOLD,
    MAGENTA,
    ORANGE,
    PURPLE,
    Quips,
    goblin_embed,
    sol,
    status_text,
    ts,
    tx_link,
)
from bot.events import guarded
from bot.helpers import actor_of, bot_of, defer, identity_of, reply
from bot.views.approval_view import ApprovalQueueView
from bot.views.award_view import AwardActionsView
from bot.views.batch_view import batch_files, batch_summary_embed
from bot.views.confirmation_view import confirm, send_paginated
from bot.views.payment_view import batch_pay_view
from config.permissions import Permission, require
from database.models import AwardStatus, DisputeResolution
from services.errors import InputError, NotFound
from services.tip_service import AwardRow
from utils.currency import parse_sol

STATUS_CHOICES = [
    app_commands.Choice(name=s.value.replace("_", " "), value=s.value) for s in AwardStatus
]


def wallet_text(row: AwardRow) -> str:
    if row.payout_wallet:
        return f"🔒 `{row.payout_wallet}`"
    if row.current_wallet:
        return f"👛 `{row.current_wallet}`"
    return "👛 *no wallet registered*"


WAITING_STATUSES = (AwardStatus.PENDING_REVIEW, AwardStatus.AWAITING_WALLET)


def award_line(row: AwardRow, *, show_user: bool = True, show_wallet: bool = False) -> str:
    who = f"<@{row.discord_user_id}> · " if show_user else ""
    line = (
        f"**#{row.id}** · {who}{sol(row.amount_lamports)} · {row.reason[:60]} · "
        f"{status_text(row.status)}"
    )
    if row.status in WAITING_STATUSES:
        line += f" · ⏳ {ts(row.created_at, 'R')}"  # how long it's been waiting on staff/member
    return f"{line}\n  ↳ {wallet_text(row)}" if show_wallet else line


# What each status means for the member, shown in their own /tip history.
MEMBER_HINTS = {
    AwardStatus.AWAITING_WALLET: "register a wallet with `/wallet set` to claim this",
    AwardStatus.PENDING_REVIEW: "waiting for staff review",
    AwardStatus.APPROVED: "approved, waiting for the next payout batch",
    AwardStatus.QUEUED: "in a payout batch, payment coming",
    AwardStatus.PAYMENT_SUBMITTED: "payment sent, waiting for staff confirmation",
    AwardStatus.PAID: "paid (manually confirmed by staff)",
    AwardStatus.DISPUTED: "under review by staff",
    AwardStatus.VOIDED: "cancelled",
}


def member_history_line(row: AwardRow) -> str:
    line = (
        f"{ts(row.created_at, 'd')} · **{sol(row.amount_lamports)}** · {row.reason[:60]}\n"
        f"  ↳ {status_text(row.status)}: {MEMBER_HINTS.get(AwardStatus(row.status), '')}"
    )
    if row.payment_signature:
        line += f" · {tx_link(row.payment_signature)}"
    return line


def award_embed(row: AwardRow, title: str | None = None) -> discord.Embed:
    embed = goblin_embed(title or f"Award #{row.id}", colour=PURPLE)
    embed.add_field(name="Recipient", value=f"<@{row.discord_user_id}> ({row.username})")
    embed.add_field(name="Amount", value=sol(row.amount_lamports))
    embed.add_field(name="Status", value=status_text(row.status))
    embed.add_field(name="Reason", value=row.reason[:1024], inline=False)
    source = f"Roll call #{row.event_id}" if row.event_id else "Manual award"
    embed.add_field(name="Source", value=source)
    embed.add_field(name="Created", value=ts(row.created_at))
    if row.approved_at:
        embed.add_field(
            name="Approved", value=f"<@{row.approved_by_discord_id}> · {ts(row.approved_at)}"
        )
    if row.payout_wallet:
        embed.add_field(name="Payout wallet (locked)", value=f"`{row.payout_wallet}`", inline=False)
    elif row.current_wallet:
        embed.add_field(
            name="Current wallet (locks in on approval)",
            value=f"`{row.current_wallet}`",
            inline=False,
        )
    if row.batch_id:
        embed.add_field(name="Batch", value=f"#{row.batch_id}")
    if row.payment_signature:
        embed.add_field(name="Transaction", value=tx_link(row.payment_signature), inline=False)
    if row.paid_at:
        source_text = {
            "manual": "manually confirmed by staff",
            "dispute_resolution": "settled via dispute review",
        }.get(row.verification_source or "", row.verification_source or "")
        embed.add_field(name="Paid", value=f"{ts(row.paid_at)} · {source_text}")
    return embed


async def _award_choices(
    interaction: discord.Interaction, current: str, statuses: tuple[AwardStatus, ...]
) -> list[app_commands.Choice[int]]:
    actor = actor_of(interaction)
    if not actor.can(Permission.AWARD_VIEW_QUEUE):
        return []
    tips = bot_of(interaction).services.tips
    rows: list[AwardRow] = []
    for status in statuses:
        rows += (await tips.list_awards(actor, status=status, page_size=25)).items
    current = current.strip().lstrip("#")
    rows = [
        r
        for r in rows
        if not current or str(r.id).startswith(current) or current.lower() in r.username.lower()
    ]
    return [
        app_commands.Choice(
            name=f"#{r.id} · {r.username} · {sol(r.amount_lamports)} · {r.reason}"[:100],
            value=r.id,
        )
        for r in sorted(rows, key=lambda r: r.id)[:25]
    ]


async def remind_missing_wallets(interaction: discord.Interaction) -> None:
    """Privately nudge members whose awards are stuck on 'awaiting wallet'.

    Rate-limited per member (24 h) and skips anyone who turned DMs off.
    """
    actor = actor_of(interaction)
    require(actor, Permission.AWARD_VIEW_QUEUE)
    bot = bot_of(interaction)
    await interaction.response.defer(ephemeral=True, thinking=True)
    if not bot.settings.member_dms:
        raise InputError("Member DMs are switched off for this server (`MEMBER_DMS=false`).")
    owed = {m.discord_user_id: m for m in await bot.services.notifications.missing_wallets(actor)}
    chosen, skipped = await bot.services.notifications.claim_wallet_reminders(list(owed))
    bot.dm_members(
        {
            discord_id: goblin_embed(
                "👛 You've got SOL waiting!",
                f"You have **{owed[discord_id].award_count}** Dude-Munity award(s) worth "
                f"**{sol(owed[discord_id].lamports)}** waiting, but no wallet registered.\n\n"
                "Run `/wallet set` in the server with your **public** SOL address so staff can "
                "review them. Never share your seed phrase or private key.\n"
                "-# Don't want DMs? `/notifications dms:off`",
                colour=ORANGE,
            )
            for discord_id in chosen
        }
    )
    await interaction.followup.send(
        f"📨 Reminding **{len(chosen)}** member(s) privately."
        + (
            f" Skipped {skipped} (reminded in the last 24 h, or DMs turned off)." if skipped else ""
        ),
        ephemeral=True,
    )


class TipCog(commands.GroupCog, group_name="tip", group_description="The Dude-Munity tip ledger"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def services(self):  # type: ignore[no-untyped-def]
        return self.bot.services  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ browsing

    @app_commands.command(name="list", description="Browse the award queue")
    @app_commands.autocomplete(event_id=rollcall_choices)
    @app_commands.describe(
        status="Which awards to show (default: pending review)",
        event_id="Only awards from this roll call",
    )
    @app_commands.choices(status=STATUS_CHOICES)
    async def list_awards(
        self,
        interaction: discord.Interaction,
        status: app_commands.Choice[str] | None = None,
        event_id: int | None = None,
        page: int = 1,
    ) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.AWARD_VIEW_QUEUE)
        await defer(interaction)
        wanted = AwardStatus(status.value) if status else AwardStatus.PENDING_REVIEW
        shown: list[AwardRow] = []  # rows on the page currently displayed

        async def render(number: int) -> tuple[discord.Embed, int]:
            result = await self.services.tips.list_awards(
                actor, status=wanted, event_id=event_id, page=number, page_size=10
            )
            shown[:] = result.items
            total = sum(row.amount_lamports for row in result.items)
            lines = [award_line(row, show_wallet=True) for row in result.items] or [
                "Nothing here. 🧌"
            ]
            embed = goblin_embed(f"{status_text(wanted)} awards", "\n".join(lines), colour=PURPLE)
            embed.set_footer(
                text=f"Page {result.page}/{result.pages} • {result.total} awards • "
                f"{sol(total)} on this page • 🔒 locked payout · 👛 current wallet"
            )
            return embed, result.pages

        async def copy_wallets(number: int) -> str:
            result = await self.services.tips.list_awards(
                actor, status=wanted, event_id=event_id, page=number, page_size=10
            )
            wallets = [row.display_wallet for row in result.items if row.display_wallet]
            return "\n".join(wallets) or "No wallets on this page."

        if wanted != AwardStatus.PENDING_REVIEW or not actor.can(Permission.AWARD_APPROVE):
            extra = []
            if wanted == AwardStatus.AWAITING_WALLET:
                remind = discord.ui.Button(
                    label="📨 Remind missing wallets", style=discord.ButtonStyle.primary
                )
                remind.callback = lambda i: guarded(i, remind_missing_wallets(i))
                extra.append(remind)
            await send_paginated(
                interaction,
                render,
                max(1, page),
                copy_text=copy_wallets,
                copy_label="📋 Copy wallets",
                extra_items=extra,
            )
            return

        def approvable() -> list[AwardRow]:
            if actor.can(Permission.APPROVE_OWN_AWARD):
                return list(shown)
            return [row for row in shown if row.discord_user_id != actor.discord_id]

        first_embed, pages = await render(max(1, page))
        view = ApprovalQueueView(
            interaction.user.id,
            render,
            max(1, min(page, pages)),
            pages,
            current_rows=approvable,
            copy_text=copy_wallets,
            copy_label="📋 Copy wallets",
        )
        await reply(interaction, embed=first_embed, view=view)

    @app_commands.command(name="view", description="Details for one award")
    @app_commands.checks.cooldown(5, 30)
    async def view(self, interaction: discord.Interaction, award_id: int) -> None:
        await defer(interaction)
        actor = actor_of(interaction)
        row = await self.services.tips.get(actor, award_id)
        view = AwardActionsView(
            interaction.user.id,
            row,
            actor,
            embed_for=award_embed,
            show_history=lambda i, discord_id: self.send_history(i, discord_id, page=1),
        )
        await reply(interaction, embed=award_embed(row), view=view if view.children else None)

    @app_commands.command(name="history", description="Tip history for you or a member")
    @app_commands.checks.cooldown(5, 30)
    @app_commands.describe(user="Staff only: whose history to show")
    async def history(
        self, interaction: discord.Interaction, user: discord.Member | None = None, page: int = 1
    ) -> None:
        await self.send_history(interaction, (user or interaction.user).id, page=page)

    async def send_history(
        self, interaction: discord.Interaction, discord_user_id: int, *, page: int
    ) -> None:
        actor = actor_of(interaction)
        if discord_user_id != actor.discord_id:
            require(actor, Permission.TIP_HISTORY_ANY)
        await defer(interaction)
        whose = "Your tips" if discord_user_id == actor.discord_id else "Tips"

        async def render(number: int) -> tuple[discord.Embed, int]:
            result = await self.services.tips.list_awards(
                actor, discord_user_id=discord_user_id, page=number, page_size=10
            )
            lines = [member_history_line(row) for row in result.items] or [
                "No awards yet. Show up to Zee's next roll call! 👑"
            ]
            embed = goblin_embed(f"📜 {whose}", "\n".join(lines), colour=PURPLE)
            if discord_user_id != actor.discord_id:
                embed.description = f"<@{discord_user_id}>\n\n" + (embed.description or "")
            embed.set_footer(text=f"Page {result.page}/{result.pages} • {result.total} awards")
            return embed, result.pages

        await send_paginated(interaction, render, max(1, page))

    # ------------------------------------------------------------------ changes

    @app_commands.command(name="add", description="Request a manual award (goes to review)")
    @app_commands.describe(amount="SOL, e.g. 0.05", reason="Why they earned it")
    async def add(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: str,
        reason: app_commands.Range[str, 1, 300],
    ) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.AWARD_REQUEST)
        if user.bot:
            raise InputError("Bots don't get tips. Not even me. 😤")
        await defer(interaction)
        row = await self.services.tips.add_manual(
            actor, identity_of(user), parse_sol(amount), reason
        )
        embed = award_embed(row, f"📝 Award #{row.id} requested")
        if row.status == AwardStatus.AWAITING_WALLET:
            embed.add_field(
                name="Heads up", value=f"{user.mention} has no wallet registered yet.", inline=False
            )
        await reply(interaction, embed=embed)

    @app_commands.command(name="approve", description="Approve a pending award")
    async def approve(self, interaction: discord.Interaction, award_id: int) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.AWARD_APPROVE)
        await defer(interaction)
        row = await self.services.tips.approve(actor, award_id)
        await reply(interaction, embed=award_embed(row, Quips.APPROVED))

    @approve.autocomplete("award_id")
    async def _approve_ac(self, interaction: discord.Interaction, current: str):  # type: ignore[no-untyped-def]
        return await _award_choices(interaction, current, (AwardStatus.PENDING_REVIEW,))

    @app_commands.command(
        name="approve-event", description="Approve every pending award from a roll call"
    )
    @app_commands.autocomplete(event_id=rollcall_choices)
    async def approve_event(self, interaction: discord.Interaction, event_id: int) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.AWARD_APPROVE)
        await defer(interaction)
        pending = await self.services.tips.list_awards(
            actor, status=AwardStatus.PENDING_REVIEW, event_id=event_id, page_size=1
        )
        if not pending.total:
            raise NotFound(f"Roll call #{event_id} has no awards pending review.")
        prompt = goblin_embed(
            f"Approve roll call #{event_id}?",
            f"**{pending.total}** pending award(s). Each locks in the member's current wallet "
            "as its payout address.",
            colour=ORANGE,
        )
        if not await confirm(interaction, prompt, confirm_label=f"Approve {pending.total}"):
            return
        result = await self.services.tips.approve_event(actor, event_id)
        text = (
            f"Approved **{result.approved}** award(s) totalling **{sol(result.total_lamports)}**."
        )
        if result.skipped_own:
            text += (
                f"\nSkipped {result.skipped_own} of your own (another staff member must approve)."
            )
        if result.skipped_no_wallet:
            text += f"\nSkipped {result.skipped_no_wallet} with no wallet."
        await reply(interaction, embed=goblin_embed(Quips.APPROVED, text, colour=GOLD))

    @app_commands.command(name="void", description="Void an award (keeps its history)")
    async def void(
        self,
        interaction: discord.Interaction,
        award_id: int,
        reason: app_commands.Range[str, 1, 300],
    ) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.AWARD_VOID)
        await defer(interaction)
        row = await self.services.tips.get(actor, award_id)
        prompt = award_embed(row, f"Void award #{award_id}?")
        prompt.add_field(name="Void reason", value=reason, inline=False)
        if not await confirm(interaction, prompt, confirm_label="Void award", danger=True):
            return
        row = await self.services.tips.void(actor, award_id, reason)
        await reply(interaction, embed=award_embed(row, f"🚫 Award #{award_id} voided"))

    @void.autocomplete("award_id")
    async def _void_ac(self, interaction: discord.Interaction, current: str):  # type: ignore[no-untyped-def]
        return await _award_choices(
            interaction,
            current,
            (
                AwardStatus.PENDING_REVIEW,
                AwardStatus.AWAITING_WALLET,
                AwardStatus.APPROVED,
                AwardStatus.DISPUTED,
            ),
        )

    @view.autocomplete("award_id")
    async def _view_ac(self, interaction: discord.Interaction, current: str):  # type: ignore[no-untyped-def]
        return await _award_choices(interaction, current, tuple(AwardStatus))

    @app_commands.command(name="flag", description="Flag a suspicious award for head-mod review")
    async def flag(
        self,
        interaction: discord.Interaction,
        award_id: int,
        reason: app_commands.Range[str, 1, 300],
    ) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.AWARD_FLAG)
        await defer(interaction)
        dispute = await self.services.tips.flag(actor, award_id, reason)
        embed = goblin_embed(
            f"⚠️ Award #{award_id} flagged",
            f"Dispute #{dispute.id} opened: {reason}",
            colour=MAGENTA,
        )
        await reply(interaction, embed=embed)
        await bot_of(interaction).notify_staff(embed)

    @flag.autocomplete("award_id")
    async def _flag_ac(self, interaction: discord.Interaction, current: str):  # type: ignore[no-untyped-def]
        return await _award_choices(
            interaction,
            current,
            (
                AwardStatus.PENDING_REVIEW,
                AwardStatus.AWAITING_WALLET,
                AwardStatus.APPROVED,
                AwardStatus.PAYMENT_SUBMITTED,
            ),
        )

    @app_commands.command(
        name="rewallet",
        description="Send an approved award back to review to change its payout wallet",
    )
    async def rewallet(
        self,
        interaction: discord.Interaction,
        award_id: int,
        reason: app_commands.Range[str, 1, 300],
    ) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.AWARD_REWALLET)
        await defer(interaction)
        row = await self.services.tips.get(actor, award_id)
        prompt = award_embed(row, f"Unlock award #{award_id}'s payout wallet?")
        prompt.add_field(
            name="What happens",
            value=(
                "The award returns to **pending review** and its locked wallet is cleared. "
                "Re-approving it locks the member's *current* wallet."
            ),
            inline=False,
        )
        if not await confirm(interaction, prompt, confirm_label="Send back to review", danger=True):
            return
        row = await self.services.tips.return_to_review(actor, award_id, reason)
        await reply(interaction, embed=award_embed(row, f"↩️ Award #{award_id} back in review"))

    @rewallet.autocomplete("award_id")
    async def _rewallet_ac(self, interaction: discord.Interaction, current: str):  # type: ignore[no-untyped-def]
        return await _award_choices(interaction, current, (AwardStatus.APPROVED,))

    # ------------------------------------------------------------------ payouts

    @app_commands.command(
        name="export-batch",
        description="Reserve approved awards into a payment batch and export CSVs",
    )
    @app_commands.describe(max_awards="Optional cap on how many awards to include")
    async def export_batch(
        self,
        interaction: discord.Interaction,
        max_awards: app_commands.Range[int, 1, 5000] | None = None,
    ) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.BATCH_EXPORT)
        await defer(interaction)
        ledger = await self.services.tips.ledger_summary(actor)
        ready = ledger.by_status[AwardStatus.APPROVED]
        if not ready.count:
            raise NotFound("No approved awards are waiting for payment. 🧌 The vault is quiet.")
        count = min(ready.count, max_awards) if max_awards else ready.count
        prompt = goblin_embed(
            "Create a payment batch?",
            f"Up to **{count}** approved award(s) ({sol(ready.lamports)} total approved) will be "
            "reserved for payment. ZeeGoblin will **not** send any SOL - you'll get CSV files "
            "to pay from, then record each transfer with the **💸 Record a payment** menu.",
            colour=ORANGE,
        )
        if not await confirm(interaction, prompt, confirm_label="Create batch"):
            return
        detail = await self.services.payments.create_batch(actor, max_awards=max_awards)
        await reply(
            interaction,
            embed=batch_summary_embed(detail),
            files=batch_files(detail),
            view=batch_pay_view(interaction.user.id, detail, actor),
        )


RESOLUTION_CHOICES = [
    app_commands.Choice(name="reopen - back to pending review", value=DisputeResolution.REOPENED),
    app_commands.Choice(name="void - cancel the award", value=DisputeResolution.VOIDED),
    app_commands.Choice(
        name="settled - recorded payment counts as paid", value=DisputeResolution.SETTLED
    ),
]


class DisputeCog(commands.GroupCog, group_name="dispute", group_description="Flagged awards"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="list", description="Open (or all) disputes")
    async def list_disputes(
        self, interaction: discord.Interaction, include_resolved: bool = False, page: int = 1
    ) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.AWARD_VIEW_QUEUE)
        await defer(interaction)
        tips = bot_of(interaction).services.tips

        async def render(number: int) -> tuple[discord.Embed, int]:
            result = await tips.list_disputes(actor, open_only=not include_resolved, page=number)
            lines = [
                f"**#{d.id}** · award #{d.award_id} · {d.status}"
                + (f" ({d.resolution})" if d.resolution else "")
                + f" · by <@{d.opened_by_discord_id}> {ts(d.opened_at, 'R')}\n  ↳ {d.reason[:120]}"
                for d in result.items
            ] or ["No disputes. Peaceful goblin times."]
            embed = goblin_embed("⚠️ Disputes", "\n".join(lines), colour=MAGENTA)
            embed.set_footer(text=f"Page {result.page}/{result.pages} • {result.total} total")
            return embed, result.pages

        await send_paginated(interaction, render, max(1, page))

    @app_commands.command(name="resolve", description="Resolve a dispute")
    @app_commands.autocomplete(dispute_id=open_dispute_choices)
    @app_commands.choices(outcome=RESOLUTION_CHOICES)
    async def resolve(
        self,
        interaction: discord.Interaction,
        dispute_id: int,
        outcome: app_commands.Choice[str],
        note: app_commands.Range[str, 1, 300],
    ) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.DISPUTE_RESOLVE)
        await defer(interaction)
        tips = bot_of(interaction).services.tips
        dispute = await tips.get_dispute(actor, dispute_id)
        prompt = award_embed(
            await tips.get(actor, dispute.award_id),
            f"Resolve dispute #{dispute_id} as {outcome.value}?",
        )
        prompt.add_field(name="Why it was flagged", value=dispute.reason[:1024], inline=False)
        prompt.add_field(name="Your note", value=note, inline=False)
        if not await confirm(
            interaction,
            prompt,
            confirm_label="Resolve",
            danger=outcome.value == DisputeResolution.VOIDED,
        ):
            return
        dispute = await tips.resolve_dispute(
            actor, dispute_id, DisputeResolution(outcome.value), note
        )
        await reply(
            interaction,
            embed=goblin_embed(
                f"✅ Dispute #{dispute.id} resolved",
                f"Outcome: **{dispute.resolution}**\n{note}",
                colour=GOLD,
            ),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TipCog(bot))
    await bot.add_cog(DisputeCog(bot))
