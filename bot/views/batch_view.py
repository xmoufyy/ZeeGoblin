"""Embeds and CSV attachments for payment batches."""

from __future__ import annotations

import io

import discord

from bot.branding import GOLD, goblin_embed, sol, status_text, ts
from services.payment_service import BatchDetail, batch_csv, summary_csv
from utils.validation import shorten_wallet


def batch_summary_embed(detail: BatchDetail) -> discord.Embed:
    batch = detail.batch
    embed = goblin_embed(
        f"📦 Payment batch #{batch.id}",
        f"**{batch.award_count}** award(s) · **{sol(batch.total_lamports)}** · "
        f"**{len(detail.wallets)}** wallet(s) · status **{batch.status}**\n"
        f"Created by <@{batch.created_by_discord_id}> {ts(batch.created_at, 'R')}",
        colour=GOLD,
    )
    lines = [
        f"`{shorten_wallet(w.wallet)}` · {sol(w.lamports)} · {len(w.award_ids)} award(s)"
        for w in detail.wallets[:15]
    ]
    if len(detail.wallets) > 15:
        lines.append(f"…and {len(detail.wallets) - 15} more (see the summary CSV)")
    embed.add_field(name="Per wallet", value="\n".join(lines) or "—", inline=False)
    counts = ", ".join(f"{status_text(s)} {n}" for s, n in detail.status_counts.items())
    embed.add_field(name="Award statuses", value=counts or "—", inline=False)
    if detail.payments:
        embed.add_field(
            name="Payments recorded",
            value="\n".join(
                f"#{p.id} · {sol(p.amount_lamports)} → `{shorten_wallet(p.recipient_wallet)}` · "
                f"{p.status}"
                for p in detail.payments[-10:]
            ),
            inline=False,
        )
    if batch.cancel_reason:
        embed.add_field(name="Cancelled", value=batch.cancel_reason[:1024], inline=False)
    if batch.status == "open":
        embed.add_field(
            name="Next",
            value=(
                "Send each wallet's total from your own wallet (full addresses are in the "
                "CSV files), then pick the wallet in **💸 Record a payment you sent** below "
                "and paste the transaction signature. ZeeGoblin never sends SOL itself."
            ),
            inline=False,
        )
    return embed


def batch_files(detail: BatchDetail) -> list[discord.File]:
    batch_id = detail.batch.id
    return [
        discord.File(io.BytesIO(batch_csv(detail)), filename=f"zeegoblin-batch-{batch_id}.csv"),
        discord.File(
            io.BytesIO(summary_csv(detail)),
            filename=f"zeegoblin-batch-{batch_id}-wallet-summary.csv",
        ),
    ]
