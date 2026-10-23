"""/batch ... and /payment ... - manual payout tracking. ZeeGoblin never sends SOL."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.autocomplete import batch_choices, unconfirmed_payment_choices
from bot.branding import GOLD, MAGENTA, ORANGE, PURPLE, goblin_embed, sol, ts, tx_link
from bot.helpers import actor_of, bot_of, defer, reply
from bot.views.batch_view import batch_summary_embed
from bot.views.confirmation_view import confirm, send_paginated
from bot.views.payment_view import ConfirmPaymentView, batch_pay_view, confirm_and_report
from config.permissions import Permission, require
from database.models import PaymentStatus
from utils.currency import parse_sol
from utils.validation import parse_id_list, shorten_wallet

PAYMENT_STATUS_TEXT = {
    PaymentStatus.SUBMITTED: "📨 submitted (awaiting confirmation)",
    PaymentStatus.MANUALLY_CONFIRMED: "✅ manually confirmed",
    PaymentStatus.BLOCKCHAIN_VERIFIED: "⛓️ blockchain verified",
    PaymentStatus.FLAGGED: "⚠️ flagged mismatch",
}


class BatchCog(commands.GroupCog, group_name="batch", group_description="Payment batches"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="list", description="Recent payment batches")
    async def list_batches(self, interaction: discord.Interaction, page: int = 1) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.BATCH_VIEW)
        await defer(interaction)
        payments = bot_of(interaction).services.payments

        async def render(number: int) -> tuple[discord.Embed, int]:
            result = await payments.list_batches(actor, page=number)
            progress = await payments.batch_progress(actor, [b.id for b in result.items])
            lines = [
                f"**#{b.id}** · {b.status} · {sol(b.total_lamports)} · "
                f"💰 {progress[b.id][0]}/{b.award_count} paid"
                + (f" · {sol(progress[b.id][1])} left" if progress[b.id][1] else "")
                + f" · {ts(b.created_at, 'd')} by <@{b.created_by_discord_id}>"
                for b in result.items
            ] or ["No batches yet."]
            embed = goblin_embed("📦 Payment batches", "\n".join(lines), colour=GOLD)
            embed.set_footer(text=f"Page {result.page}/{result.pages} • {result.total} total")
            return embed, result.pages

        await send_paginated(interaction, render, max(1, page))

    @app_commands.command(name="view", description="Batch details")
    @app_commands.autocomplete(batch_id=batch_choices)
    async def view(self, interaction: discord.Interaction, batch_id: int) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.BATCH_VIEW)
        await defer(interaction)
        detail = await bot_of(interaction).services.payments.get_batch(actor, batch_id)
        await reply(
            interaction,
            embed=batch_summary_embed(detail),
            view=batch_pay_view(interaction.user.id, detail, actor),
        )

    @app_commands.command(name="cancel", description="Cancel a batch and release unpaid awards")
    @app_commands.autocomplete(batch_id=batch_choices)
    async def cancel(
        self,
        interaction: discord.Interaction,
        batch_id: int,
        reason: app_commands.Range[str, 1, 300],
    ) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.BATCH_CANCEL)
        await defer(interaction)
        services = bot_of(interaction).services
        detail = await services.payments.get_batch(actor, batch_id)
        prompt = batch_summary_embed(detail)
        prompt.title = f"Cancel batch #{batch_id}?"
        prompt.add_field(
            name="What happens",
            value=(
                "Queued awards go back to **approved** for a future batch. Awards with a recorded "
                "payment stay as they are. Only cancel if you haven't sent these payments."
            ),
            inline=False,
        )
        if not await confirm(interaction, prompt, confirm_label="Cancel batch", danger=True):
            return
        result = await services.payments.cancel_batch(actor, batch_id, reason)
        await reply(
            interaction,
            embed=goblin_embed(
                f"🚫 Batch #{batch_id} cancelled",
                f"Released {result.released} award(s) back to approved; "
                f"{result.retained} with recorded payments kept.\nReason: {reason}",
                colour=ORANGE,
            ),
        )


class PaymentCog(
    commands.GroupCog, group_name="payment", group_description="Record SOL you sent manually"
):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="record", description="Record a SOL transfer you already sent")
    @app_commands.autocomplete(batch_id=batch_choices)
    @app_commands.describe(
        batch_id="Payment batch the transfer belongs to",
        wallet="Recipient wallet you sent to",
        amount="Exact SOL sent, e.g. 0.03",
        signature="Solana transaction signature",
        award_ids="Optional: award IDs covered, e.g. '12 13'. Default: all unpaid awards "
        "for this wallet in the batch",
        flag_mismatch="Record anyway if it doesn't match (sends the awards to dispute review)",
        note="Optional note",
    )
    async def record(
        self,
        interaction: discord.Interaction,
        batch_id: int,
        wallet: str,
        amount: str,
        signature: str,
        award_ids: str | None = None,
        flag_mismatch: bool = False,
        note: app_commands.Range[str, 0, 300] | None = None,
    ) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.PAYMENT_RECORD)
        await defer(interaction)
        bot = bot_of(interaction)
        result = await bot.services.payments.record_payment(
            actor,
            batch_id=batch_id,
            wallet=wallet,
            amount_lamports=parse_sol(amount),
            signature=signature,
            award_ids=parse_id_list(award_ids, field="award_ids") or None,
            note=note,
            flag_mismatch=flag_mismatch,
        )
        payment = result.payment
        embed = goblin_embed(
            f"{'⚠️' if result.flagged else '📨'} Payment #{payment.id} recorded",
            f"{sol(payment.amount_lamports)} → `{payment.recipient_wallet}`\n"
            f"Awards: {', '.join(f'#{i}' for i in result.award_ids)}\n"
            f"Transaction: {tx_link(payment.signature)}",
            colour=MAGENTA if result.flagged else PURPLE,
        )
        if result.flagged:
            embed.add_field(
                name="Flagged for review", value="; ".join(result.mismatches), inline=False
            )
            await bot.notify_staff(embed)
            await reply(interaction, embed=embed)
            return
        embed.add_field(
            name="Next",
            value="Once you've checked the transfer landed, click **Confirm payment sent**.",
            inline=False,
        )
        await reply(
            interaction, embed=embed, view=ConfirmPaymentView(interaction.user.id, payment.id)
        )

    @app_commands.command(name="confirm", description="Confirm a recorded payment was sent")
    @app_commands.autocomplete(payment_id=unconfirmed_payment_choices)
    async def confirm_payment(
        self,
        interaction: discord.Interaction,
        payment_id: int,
        note: app_commands.Range[str, 0, 300] | None = None,
    ) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.PAYMENT_CONFIRM)
        await defer(interaction)
        services = bot_of(interaction).services
        row = await services.payments.get_payment(actor, payment_id)
        payment = row.payment
        prompt = goblin_embed(
            f"Confirm payment #{payment_id}?",
            f"{sol(payment.amount_lamports)} → `{payment.recipient_wallet}`\n"
            f"Transaction: {tx_link(payment.signature)}\nAwards: "
            f"{', '.join(f'#{i}' for i in row.award_ids)}\n\n"
            "You are attesting this transfer was sent. This is a **manual confirmation**, "
            "not blockchain verification.",
            colour=ORANGE,
        )
        if not await confirm(interaction, prompt, confirm_label="Confirm payment"):
            return
        await confirm_and_report(interaction, payment_id, note)

    @app_commands.command(name="history", description="Recorded payments")
    @app_commands.autocomplete(batch_id=batch_choices)
    @app_commands.choices(
        status=[app_commands.Choice(name=v, value=k) for k, v in PAYMENT_STATUS_TEXT.items()]
    )
    async def history(
        self,
        interaction: discord.Interaction,
        batch_id: int | None = None,
        status: app_commands.Choice[str] | None = None,
        page: int = 1,
    ) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.PAYMENT_VIEW)
        await defer(interaction)
        payments = bot_of(interaction).services.payments
        wanted = PaymentStatus(status.value) if status else None

        async def render(number: int) -> tuple[discord.Embed, int]:
            result = await payments.payment_history(
                actor, batch_id=batch_id, status=wanted, page=number
            )
            lines = [
                f"**#{r.payment.id}** · batch #{r.payment.batch_id} · "
                f"{sol(r.payment.amount_lamports)} → `{shorten_wallet(r.payment.recipient_wallet)}`"
                f" · {PAYMENT_STATUS_TEXT[PaymentStatus(r.payment.status)]}\n"
                f"  ↳ awards {', '.join(f'#{i}' for i in r.award_ids)} · "
                f"{tx_link(r.payment.signature)} · {ts(r.payment.recorded_at, 'd')}"
                for r in result.items
            ] or ["No payments recorded."]
            embed = goblin_embed("💸 Payment history", "\n".join(lines), colour=PURPLE)
            embed.set_footer(
                text=f"Page {result.page}/{result.pages} • {result.total} total • "
                "manual confirmations are not blockchain-verified"
            )
            return embed, result.pages

        await send_paginated(interaction, render, max(1, page))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BatchCog(bot))
    await bot.add_cog(PaymentCog(bot))
