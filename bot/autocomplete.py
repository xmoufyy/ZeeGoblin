"""Autocomplete for ID options, so mods pick from a list instead of typing numbers.

Each lookup respects permissions: someone who can't view a record never sees its choices.
"""

from __future__ import annotations

import discord
from discord import app_commands

from bot.branding import sol
from bot.helpers import actor_of, bot_of
from config.permissions import Permission
from database.models import PaymentStatus
from utils.validation import shorten_wallet

Choices = list[app_commands.Choice[int]]


def _matches(value: int, current: str) -> bool:
    current = current.strip().lstrip("#")
    return not current or str(value).startswith(current)


def _choices(items: list[tuple[int, str]], current: str) -> Choices:
    return [
        app_commands.Choice(name=label[:100], value=value)
        for value, label in items
        if _matches(value, current)
    ][:25]


async def batch_choices(interaction: discord.Interaction, current: str) -> Choices:
    actor = actor_of(interaction)
    if not actor.can(Permission.BATCH_VIEW):
        return []
    page = await bot_of(interaction).services.payments.list_batches(actor, page_size=25)
    # Open batches first: they're the ones mods act on.
    batches = sorted(page.items, key=lambda b: (b.status != "open", -b.id))
    return _choices(
        [
            (b.id, f"#{b.id} · {b.status} · {b.award_count} award(s) · {sol(b.total_lamports)}")
            for b in batches
        ],
        current,
    )


async def unconfirmed_payment_choices(interaction: discord.Interaction, current: str) -> Choices:
    actor = actor_of(interaction)
    if not actor.can(Permission.PAYMENT_VIEW):
        return []
    page = await bot_of(interaction).services.payments.payment_history(
        actor, status=PaymentStatus.SUBMITTED, page_size=25
    )
    return _choices(
        [
            (
                r.payment.id,
                f"#{r.payment.id} · {sol(r.payment.amount_lamports)} → "
                f"{shorten_wallet(r.payment.recipient_wallet)} · batch #{r.payment.batch_id}",
            )
            for r in page.items
        ],
        current,
    )


async def rollcall_choices(interaction: discord.Interaction, current: str) -> Choices:
    actor = actor_of(interaction)
    if not actor.can(Permission.ROLLCALL_VIEW):
        return []
    page = await bot_of(interaction).services.rollcalls.history(actor, page_size=25)
    return _choices(
        [
            (
                e.id,
                f"#{e.id} · {e.status} · 👥 {e.entry_count} · {sol(e.reward_lamports)} each · "
                f"{e.started_at:%b %d %H:%M} UTC",
            )
            for e in page.items
        ],
        current,
    )


async def open_dispute_choices(interaction: discord.Interaction, current: str) -> Choices:
    actor = actor_of(interaction)
    if not actor.can(Permission.AWARD_VIEW_QUEUE):
        return []
    page = await bot_of(interaction).services.tips.list_disputes(actor, page_size=25)
    return _choices(
        [(d.id, f"#{d.id} · award #{d.award_id} · {d.reason}") for d in page.items], current
    )
