"""Pay a batch without typing IDs: pick a wallet, paste the signature, click confirm."""

from __future__ import annotations

import contextlib
from collections import defaultdict

import discord

from bot.branding import GOLD, PURPLE, Quips, goblin_embed, sol, tx_link
from bot.events import guarded
from bot.helpers import actor_of, bot_of
from bot.views.batch_view import batch_files
from bot.views.confirmation_view import OwnedView
from config.permissions import Actor, Permission
from database.models import AwardStatus, BatchStatus, PaymentStatus
from services.payment_service import BatchDetail, PaymentRecord
from services.tip_service import AwardRow
from utils.validation import shorten_wallet

MAX_OPTIONS = 25


class RecordPaymentModal(discord.ui.Modal):
    def __init__(self, batch_id: int, wallet: str, lamports: int) -> None:
        super().__init__(title=f"Record payment · batch #{batch_id}"[:45], timeout=600)
        self.batch_id = batch_id
        self.wallet = wallet
        self.lamports = lamports
        self.signature: discord.ui.TextInput = discord.ui.TextInput(
            placeholder="Paste the Solana transaction signature", min_length=64, max_length=88
        )
        self.note: discord.ui.TextInput = discord.ui.TextInput(
            required=False, max_length=300, style=discord.TextStyle.paragraph
        )
        self.add_item(
            discord.ui.Label(
                text="Transaction signature",
                description=f"{sol(lamports)} → {wallet}"[:100],
                component=self.signature,
            )
        )
        self.add_item(discord.ui.Label(text="Note (optional)", component=self.note))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await guarded(interaction, self._submit(interaction))

    async def _submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await bot_of(interaction).services.payments.record_payment(
            actor_of(interaction),
            batch_id=self.batch_id,
            wallet=self.wallet,
            amount_lamports=self.lamports,
            signature=self.signature.value,
            note=self.note.value or None,
        )
        await interaction.followup.send(
            embed=recorded_embed(result),
            view=ConfirmPaymentView(interaction.user.id, result.payment.id),
            ephemeral=True,
        )


def recorded_embed(result: PaymentRecord) -> discord.Embed:
    payment = result.payment
    embed = goblin_embed(
        f"📨 Payment #{payment.id} recorded",
        f"{sol(payment.amount_lamports)} → `{payment.recipient_wallet}`\n"
        f"Transaction: {tx_link(payment.signature)}\n\n"
        "Once you've checked it landed, click **Confirm payment sent**. That's a manual "
        "confirmation by you, not blockchain verification.",
        colour=PURPLE,
    )
    return embed


class ConfirmPaymentView(OwnedView):
    def __init__(self, owner_id: int, payment_id: int) -> None:
        super().__init__(owner_id, timeout=900)
        self.payment_id = payment_id

    @discord.ui.button(label="✅ Confirm payment sent", style=discord.ButtonStyle.success)
    async def confirm_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await guarded(interaction, self._confirm(interaction))

    async def _confirm(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()  # update the message that holds the button
        await confirm_and_report(interaction, self.payment_id)
        self.stop()
        with contextlib.suppress(discord.HTTPException):
            await interaction.edit_original_response(view=None)  # can't be confirmed twice


async def confirm_and_report(
    interaction: discord.Interaction, payment_id: int, note: str | None = None
) -> None:
    """Confirm a payment, tell the moderator, and DM each recipient that they were paid."""
    bot = bot_of(interaction)
    actor = actor_of(interaction)
    confirmed = await bot.services.payments.confirm_payment(actor, payment_id, note)
    signature = confirmed.payment.signature

    by_member: dict[int, list[AwardRow]] = defaultdict(list)
    for award_id in confirmed.award_ids:
        row = await bot.services.tips.get(actor, award_id)
        by_member[row.discord_user_id].append(row)
    bot.dm_members(
        {
            discord_id: goblin_embed(
                "💰 Zee paid you!",
                f"**{sol(sum(r.amount_lamports for r in rows))}** was sent to "
                f"`{shorten_wallet(rows[0].payout_wallet)}` for: "
                + "; ".join(dict.fromkeys(r.reason for r in rows))
                + f"\n\nTransaction: {tx_link(signature)}\n"
                "-# Confirmed manually by staff. See all your tips with /profile.",
                colour=GOLD,
            )
            for discord_id, rows in by_member.items()
        }
    )

    await interaction.followup.send(
        embed=goblin_embed(
            Quips.PAID,
            f"Payment #{payment_id} manually confirmed. {len(confirmed.award_ids)} award(s) "
            f"marked paid. Transaction: {tx_link(signature)}",
            colour=GOLD,
        ),
        ephemeral=True,
    )


class BatchPayView(OwnedView):
    """Controls under a batch: download CSVs, record a payment, confirm a recorded one."""

    def __init__(self, owner_id: int, detail: BatchDetail, actor: Actor) -> None:
        super().__init__(owner_id, timeout=900)
        self.batch_id = detail.batch.id
        if actor.can(Permission.BATCH_EXPORT):
            download: discord.ui.Button = discord.ui.Button(
                label="📄 Download CSVs", style=discord.ButtonStyle.secondary, row=0
            )
            download.callback = lambda i: guarded(i, self._download(i))
            self.add_item(download)
        if detail.batch.status != BatchStatus.OPEN:
            return
        unpaid: dict[str, int] = defaultdict(int)
        names: dict[str, list[str]] = defaultdict(list)
        for line in detail.lines:
            if line.status == AwardStatus.QUEUED:
                unpaid[line.wallet] += line.amount_lamports
                if line.username not in names[line.wallet]:
                    names[line.wallet].append(line.username)
        self.unpaid = unpaid

        if actor.can(Permission.PAYMENT_RECORD) and unpaid:
            pick: discord.ui.Select = discord.ui.Select(
                placeholder=f"💸 Record a payment you sent… ({len(unpaid)} unpaid wallet(s))",
                options=[
                    discord.SelectOption(
                        label=f"{', '.join(names[wallet])} · {sol(amount)}"[:100],
                        description=wallet[:100],
                        value=wallet,
                    )
                    for wallet, amount in list(unpaid.items())[:MAX_OPTIONS]
                ],
            )
            pick.callback = lambda i: self._record(i, pick.values[0])
            self.add_item(pick)

        submitted = [p for p in detail.payments if p.status == PaymentStatus.SUBMITTED]
        if actor.can(Permission.PAYMENT_CONFIRM) and submitted:
            confirm: discord.ui.Select = discord.ui.Select(
                placeholder=f"✅ Confirm a recorded payment… ({len(submitted)} waiting)",
                options=[
                    discord.SelectOption(
                        label=f"#{p.id} · {sol(p.amount_lamports)} → "
                        f"{shorten_wallet(p.recipient_wallet)}"[:100],
                        description=f"tx {p.signature[:40]}…",
                        value=str(p.id),
                    )
                    for p in submitted[:MAX_OPTIONS]
                ],
            )
            confirm.callback = lambda i: guarded(i, self._confirm(i, int(confirm.values[0])))
            self.add_item(confirm)

    @property
    def has_actions(self) -> bool:
        return bool(self.children)

    async def _record(self, interaction: discord.Interaction, wallet: str) -> None:
        # Opening a modal must be the first response, so no defer here.
        await interaction.response.send_modal(
            RecordPaymentModal(self.batch_id, wallet, self.unpaid[wallet])
        )

    async def _confirm(self, interaction: discord.Interaction, payment_id: int) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await confirm_and_report(interaction, payment_id)

    async def _download(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        detail = await bot_of(interaction).services.payments.export_batch(
            actor_of(interaction), self.batch_id
        )
        await interaction.followup.send(
            f"📄 CSVs for batch #{self.batch_id}. Pay from the **wallet-summary** file.",
            files=batch_files(detail),
            ephemeral=True,
        )


def batch_pay_view(owner_id: int, detail: BatchDetail, actor: Actor) -> BatchPayView | None:
    view = BatchPayView(owner_id, detail, actor)
    return view if view.has_actions else None
