"""The persistent "I'M HERE!" button and roll-call embeds.

The button is a :class:`discord.ui.DynamicItem`: its ``custom_id`` encodes the event ID, so
after a restart discord.py routes clicks on *any* old announcement back here without the bot
re-registering each message. Whether the click counts is decided by the database.
"""

from __future__ import annotations

import logging
import re

import discord

from bot.branding import GOLD, ORANGE, PURPLE, Quips, goblin_embed, sol, ts
from bot.helpers import bot_of, identity_of
from database.models import Event, EventStatus
from services.rollcall_service import CheckInOutcome

log = logging.getLogger(__name__)

CUSTOM_ID_PREFIX = "zeegoblin:rollcall:"


class CheckInButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"zeegoblin:rollcall:(?P<event_id>[0-9]+)"
):
    def __init__(self, event_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="I'M HERE!",
                emoji="👑",
                style=discord.ButtonStyle.success,
                custom_id=f"{CUSTOM_ID_PREFIX}{event_id}",
            )
        )
        self.event_id = event_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ) -> CheckInButton:
        return cls(int(match["event_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = bot_of(interaction)
        if interaction.guild_id != bot.settings.discord_guild_id:
            await interaction.response.send_message(
                "This button isn't for this server.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await bot.services.rollcalls.check_in(
                self.event_id, identity_of(interaction.user)
            )
        except Exception:
            log.exception("Check-in failed for roll call #%s", self.event_id)
            await interaction.followup.send(Quips.UNEXPECTED, ephemeral=True)
            return

        if result.outcome == CheckInOutcome.ACCEPTED:
            message = Quips.CHECKIN_OK
            if not result.has_wallet:
                message += "\n\n" + Quips.CHECKIN_NO_WALLET
            message += f"\n-# Reward if approved: {sol(result.reward_lamports)}"
        else:
            message = {
                CheckInOutcome.ALREADY_CHECKED_IN: Quips.CHECKIN_DUPLICATE,
                CheckInOutcome.CLOSED: Quips.CHECKIN_CLOSED,
                CheckInOutcome.FULL: Quips.CHECKIN_FULL,
                CheckInOutcome.NOT_FOUND: Quips.CHECKIN_MISSING,
            }[result.outcome]
        await interaction.followup.send(message, ephemeral=True)


def checkin_view(event_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(CheckInButton(event_id))
    return view


def active_embed(event: Event) -> discord.Embed:
    cap = event.participant_cap
    embed = goblin_embed(
        "👑 ZEE'S ROLL CALL!",
        "**Zee wants to know who's actually here!**\n\n"
        f"⏰ **Time remaining:** {ts(event.expires_at, 'R')} (ends {ts(event.expires_at, 't')})\n"
        f"💰 **Reward:** {sol(event.reward_lamports)} each, after staff approval\n"
        f"👥 **Checked in:** {event.entry_count} / {cap}\n\n"
        "🧌 Click the button before time runs out!",
        colour=PURPLE,
    )
    embed.set_footer(
        text=f"Roll call #{event.id} • Free to enter - no bets, deposits or "
        "purchases. Rewards need a registered wallet (/wallet set)."
    )
    return embed


def closed_embed(event: Event) -> discord.Embed:
    if event.status == EventStatus.CANCELLED:
        embed = goblin_embed(
            f"🚫 ROLL CALL #{event.id} CANCELLED",
            f"Staff cancelled this roll call. No awards were created.\n"
            f"👥 {event.entry_count} checked in before it closed.",
            colour=ORANGE,
        )
    else:
        embed = goblin_embed(
            f"🔒 ROLL CALL #{event.id} CLOSED",
            f"👥 **{event.entry_count}** goblins showed up.\n"
            f"💰 {sol(event.reward_lamports)} each is now **pending staff review**.\n\n"
            "Check your status any time with `/profile`. No wallet yet? `/wallet set` - "
            "your award waits for you.",
            colour=GOLD,
        )
    if event.closed_at:
        embed.add_field(name="Closed", value=ts(event.closed_at, "f"))
    return embed
