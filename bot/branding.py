"""Dude-Munity look and ZeeGoblin's voice. Serious/admin messages stay plain and precise."""

from __future__ import annotations

from datetime import datetime

import discord

from database.models import AwardStatus
from services.award_states import label
from utils.currency import format_sol

GOLD = discord.Colour(0xD4AF37)
PURPLE = discord.Colour(0x6A1B9A)
ORANGE = discord.Colour(0xFF7A00)
BLACK = discord.Colour(0x111111)
MAGENTA = discord.Colour(0xE0119D)
RED = discord.Colour(0xC62828)

FOOTER = "ZeeGoblin 👑 Dude-Munity"

STATUS_EMOJI: dict[str, str] = {
    AwardStatus.AWAITING_WALLET: "👛",
    AwardStatus.PENDING_REVIEW: "🕵️",
    AwardStatus.APPROVED: "✅",
    AwardStatus.QUEUED: "📦",
    AwardStatus.PAYMENT_SUBMITTED: "📨",
    AwardStatus.PAID: "💰",
    AwardStatus.DISPUTED: "⚠️",
    AwardStatus.VOIDED: "🚫",
}


class Quips:
    CHECKIN_OK = (
        "👑 YOU'RE HERE! ZeeGoblin has recorded your attendance. Now pray Zee remembers to pay you."
    )
    CHECKIN_NO_WALLET = (
        "💀 Bro wants SOL but hasn't even registered a wallet. Use `/wallet set` "
        "before staff review, or your award just sits there collecting dust."
    )
    CHECKIN_DUPLICATE = "🧌 Relax, greedy goblin. You already checked in."
    CHECKIN_CLOSED = "⏰ Too slow! This roll call is closed. The goblin waits for no one."
    CHECKIN_FULL = "🚪 Roll call is full - the goblin treasury has a budget, you know."
    CHECKIN_MISSING = "🤔 ZeeGoblin can't find that roll call. Spooky."
    APPROVED = "💰 Another tip approved. Zee's wallet is trembling."
    PAID = "✅ Paid! The goblin treasury has survived another transaction."
    WALLET_SET = "🔐 Wallet locked into the goblin vault."
    NO_PERMISSION = "🚫 Nice try. You don't have permission to do that."
    COOLDOWN = "🧌 Slow down, goblin. Try again in {seconds}s."
    UNEXPECTED = (
        "💥 Something broke on ZeeGoblin's end. Nothing was changed unless you were told so. "
        "The error has been logged for the staff."
    )


def sol(lamports: int) -> str:
    return f"{format_sol(lamports)} SOL"


def goblin_embed(
    title: str, description: str | None = None, *, colour: discord.Colour = GOLD
) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, colour=colour)
    embed.set_footer(text=FOOTER)
    return embed


def status_text(status: str) -> str:
    return f"{STATUS_EMOJI.get(status, '')} {label(status)}".strip()


def tx_link(signature: str) -> str:
    """Markdown link to a transaction on Solscan, so anyone can check it landed."""
    return f"[`{signature[:10]}…{signature[-6:]}`](https://solscan.io/tx/{signature})"


def ts(moment: datetime, style: str = "f") -> str:
    """Discord timestamp markup, rendered in each viewer's own timezone."""
    return f"<t:{int(moment.timestamp())}:{style}>"
