"""/rollcall start | status | end | history, plus the closing scheduler.

The scheduler polls SQLite every few seconds instead of trusting in-memory timers, so a
restart, crash or reconnect never loses a deadline. Discord-side follow-ups (editing the
announcement, notifying staff) are tracked in the database and retried until done.
"""

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.autocomplete import rollcall_choices
from bot.branding import GOLD, MAGENTA, ORANGE, PURPLE, goblin_embed, sol, ts
from bot.helpers import actor_of, defer, identity_of, reply
from bot.views.approval_view import approve_event_view
from bot.views.confirmation_view import confirm, send_paginated
from bot.views.rollcall_view import active_embed, checkin_view, closed_embed
from config.permissions import Permission, require
from database.models import Event, EventStatus
from services.errors import InputError, NotFound
from services.rollcall_service import CheckInOutcome, FinalizeResult
from utils.currency import parse_sol
from utils.timeutil import utcnow
from utils.validation import shorten_wallet

log = logging.getLogger(__name__)

TICK_SECONDS = 5
COUNT_REFRESH_SECONDS = 10  # minimum gap between announcement edits per roll call
RESYNC_EVERY_TICKS = 6
MESSAGE_CHECKIN_WORDS = {"present", "here", "i'm here", "im here", "i’m here"}


class RollCallCog(commands.GroupCog, group_name="rollcall", group_description="Zee's roll calls"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._shown_counts: dict[int, int] = {}
        self._last_edit: dict[int, float] = {}
        self._tick = 0

    async def cog_load(self) -> None:
        self.scheduler.start()

    async def cog_unload(self) -> None:
        self.scheduler.cancel()

    @property
    def services(self):  # type: ignore[no-untyped-def]
        return self.bot.services  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ commands

    @app_commands.command(name="start", description="Start a roll call in this channel")
    @app_commands.describe(
        duration="Minutes until check-in closes",
        reward="SOL per participant, e.g. 0.01",
        max_participants="Optional cap on participants",
        budget_sol="Optional total SOL budget for this roll call",
    )
    async def start(
        self,
        interaction: discord.Interaction,
        duration: app_commands.Range[int, 1, 1440] | None = None,
        reward: str | None = None,
        max_participants: app_commands.Range[int, 1, 100_000] | None = None,
        budget_sol: str | None = None,
    ) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.ROLLCALL_START)  # fail fast before touching the channel
        channel = interaction.channel
        if (
            not isinstance(channel, discord.TextChannel | discord.Thread)
            or interaction.guild is None
        ):
            raise InputError("Run this in a server text channel.")
        perms = channel.permissions_for(interaction.guild.me)
        missing = [
            name
            for name, ok in (
                ("View Channel", perms.view_channel),
                ("Send Messages", perms.send_messages),
                ("Embed Links", perms.embed_links),
            )
            if not ok
        ]
        if missing:
            raise InputError(
                f"I need these permissions in {channel.mention}: {', '.join(missing)}."
            )

        limits = self.services.limits
        await defer(interaction)
        event = await self.services.rollcalls.start(
            actor,
            guild_id=interaction.guild.id,
            channel_id=channel.id,
            duration_minutes=duration or limits.default_rollcall_duration_minutes,
            reward_lamports=parse_sol(reward) if reward else limits.default_rollcall_reward,
            max_participants=max_participants,
            budget_lamports=parse_sol(budget_sol) if budget_sol else None,
        )
        ping = self.bot.settings.rollcall_ping  # type: ignore[attr-defined]
        try:
            message = await channel.send(
                content=ping or None,
                embed=active_embed(event),
                view=checkin_view(event.id),
                allowed_mentions=discord.AllowedMentions(everyone=bool(ping)),
            )
        except discord.HTTPException as exc:
            await self.services.rollcalls.abandon_unannounced(event.id, actor.discord_id, str(exc))
            log.warning("Could not post roll call #%s announcement: %s", event.id, exc)
            raise InputError(
                "I couldn't post the roll call in this channel, so it was "
                "cancelled. Check my permissions and try again."
            ) from exc
        await self.services.rollcalls.attach_message(event.id, message.id)
        self._shown_counts[event.id] = 0
        self._last_edit[event.id] = time.monotonic()
        live = goblin_embed(
            f"👑 Roll call #{event.id} is live",
            f"Closes {ts(event.expires_at, 'R')}. {sol(event.reward_lamports)} each, up to "
            f"{event.participant_cap} people (budget {sol(event.budget_lamports)}).\n"
            f"[Jump to announcement]({message.jump_url})",
            colour=PURPLE,
        )
        if ping and not perms.mention_everyone:
            live.add_field(
                name="⚠️ Nobody was pinged",
                value=f"I don't have **Mention @everyone, @here, and All Roles** in "
                f"{channel.mention}, so {ping} showed as plain text. Grant it to ZeeGoblin's "
                "role for this channel.",
                inline=False,
            )
        await reply(interaction, embed=live)

    @app_commands.command(name="status", description="Show the running roll call")
    async def status(self, interaction: discord.Interaction) -> None:
        require(actor_of(interaction), Permission.ROLLCALL_VIEW)
        await defer(interaction)
        active = await self.services.rollcalls.active_events(interaction.guild_id)
        if not active:
            await reply(interaction, "🧌 No roll call is running right now.")
            return
        event = active[0]
        embed = active_embed(event)
        embed.add_field(name="Started by", value=f"<@{event.created_by_discord_id}>")
        if event.message_id:
            embed.add_field(
                name="Announcement",
                value=(
                    f"https://discord.com/channels/{event.guild_id}/{event.channel_id}/{event.message_id}"
                ),
            )
        await reply(interaction, embed=embed)

    @app_commands.command(name="end", description="Close the running roll call now")
    @app_commands.autocomplete(event_id=rollcall_choices)
    @app_commands.describe(
        event_id="Roll call ID (defaults to the running one)",
        cancel="Cancel without creating any awards",
    )
    async def end(
        self, interaction: discord.Interaction, event_id: int | None = None, cancel: bool = False
    ) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.ROLLCALL_END)
        await defer(interaction)
        if event_id is None:
            active = await self.services.rollcalls.active_events(interaction.guild_id)
            if not active:
                raise NotFound("No roll call is running.")
            event_id = active[0].id
        event = await self.services.rollcalls.get(event_id)
        if event is None:
            raise NotFound(f"Roll call #{event_id} not found.")
        if event.status != EventStatus.ACTIVE:
            raise InputError(f"Roll call #{event_id} is already {event.status}.")
        prompt = goblin_embed(
            f"{'Cancel' if cancel else 'End'} roll call #{event_id}?",
            f"{event.entry_count} checked in so far. "
            + (
                "**No awards will be created.**"
                if cancel
                else f"Each gets a pending {sol(event.reward_lamports)} award for review."
            ),
            colour=ORANGE,
        )
        if not await confirm(
            interaction, prompt, confirm_label="Cancel it" if cancel else "End it", danger=cancel
        ):
            return
        result = await self.services.rollcalls.end(actor, event_id, award=not cancel)
        if not result.already_finalized:
            await self.publish_closure(result.event, result)
        await reply(interaction, embed=self._summary_embed(result.event, result))

    @app_commands.command(name="history", description="Recent roll calls")
    async def history(self, interaction: discord.Interaction, page: int = 1) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.ROLLCALL_VIEW)
        await defer(interaction)

        async def render(number: int) -> tuple[discord.Embed, int]:
            result = await self.services.rollcalls.history(actor, page=number, page_size=10)
            lines = [
                f"**#{e.id}** · {e.status} · {ts(e.started_at, 'd')} · 👥 {e.entry_count} · "
                f"{sol(e.reward_lamports)} each · awarded {sol(e.awarded_lamports)}"
                for e in result.items
            ] or ["No roll calls yet."]
            embed = goblin_embed("📜 Roll call history", "\n".join(lines), colour=PURPLE)
            embed.set_footer(text=f"Page {result.page}/{result.pages} • {result.total} total")
            return embed, result.pages

        await send_paginated(interaction, render, max(1, page))

    # ------------------------------------------------------------------ optional "present" messages

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        settings = self.bot.settings  # type: ignore[attr-defined]
        if (
            not settings.enable_message_checkin
            or message.author.bot
            or message.guild is None
            or message.guild.id != settings.discord_guild_id
        ):
            return
        if message.content.strip().lower().rstrip("!.") not in MESSAGE_CHECKIN_WORDS:
            return
        active = [
            e
            for e in await self.services.rollcalls.active_events(message.guild.id)
            if e.channel_id == message.channel.id
        ]
        if not active:
            return
        try:
            result = await self.services.rollcalls.check_in(
                active[0].id, identity_of(message.author), source="message"
            )
            reaction = {
                CheckInOutcome.ACCEPTED: "👑",
                CheckInOutcome.ALREADY_CHECKED_IN: "🧌",
                CheckInOutcome.CLOSED: "⏰",
                CheckInOutcome.FULL: "🚪",
            }.get(result.outcome)
            if reaction:
                await message.add_reaction(reaction)
        except discord.HTTPException:
            pass
        except Exception:
            log.exception("Message check-in failed")

    # ------------------------------------------------------------------ scheduler

    @tasks.loop(seconds=TICK_SECONDS)
    async def scheduler(self) -> None:
        try:
            for result in await self.services.rollcalls.finalize_due():
                await self.publish_closure(result.event, result)
            self._tick += 1
            if self._tick % RESYNC_EVERY_TICKS == 1:
                for event in await self.services.rollcalls.unsynced_closed_events():
                    await self.publish_closure(event, None)
            await self._refresh_counts()
        except Exception:
            # Never let the loop die; the database keeps the state, we retry next tick.
            log.exception("Roll call scheduler tick failed")

    @scheduler.before_loop
    async def _before_scheduler(self) -> None:
        await self.bot.wait_until_ready()

    async def _refresh_counts(self) -> None:
        now = time.monotonic()
        for event in await self.services.rollcalls.active_events():
            if self._shown_counts.get(event.id) == event.entry_count:
                continue
            if (
                now - self._last_edit.get(event.id, 0) < COUNT_REFRESH_SECONDS
                or not event.message_id
            ):
                continue
            self._last_edit[event.id] = now
            try:
                await self._message(event).edit(embed=active_embed(event))
                self._shown_counts[event.id] = event.entry_count
            except discord.HTTPException as exc:
                log.debug("Could not refresh roll call #%s count: %s", event.id, exc)

    def _message(self, event: Event) -> discord.PartialMessage:
        channel = self.bot.get_partial_messageable(event.channel_id, guild_id=event.guild_id)
        return channel.get_partial_message(event.message_id)  # type: ignore[arg-type]

    async def publish_closure(self, event: Event, result: FinalizeResult | None) -> None:
        """Edit the announcement and notify staff; each step is recorded once it succeeds."""
        self._shown_counts.pop(event.id, None)
        self._last_edit.pop(event.id, None)
        if not event.announcement_synced:
            done = True
            if event.message_id:
                try:
                    await self._message(event).edit(embed=closed_embed(event), view=None)
                except (discord.NotFound, discord.Forbidden) as exc:
                    log.warning(
                        "Roll call #%s announcement can't be edited (%s); skipping", event.id, exc
                    )
                except discord.HTTPException as exc:
                    log.warning(
                        "Roll call #%s announcement edit failed, will retry: %s", event.id, exc
                    )
                    done = False
            if done:
                await self.services.rollcalls.mark_synced(event.id, announcement=True)
        if not event.staff_notified:
            if self.bot.settings.staff_channel_id:  # type: ignore[attr-defined]
                sent = await self.bot.notify_staff(  # type: ignore[attr-defined]
                    self._summary_embed(event, result), approve_event_view(event)
                )
            else:
                sent = True  # nothing to notify; summary is in the logs
            if sent:
                await self.services.rollcalls.mark_synced(event.id, staff=True)

    @staticmethod
    def _summary_embed(event: Event, result: FinalizeResult | None) -> discord.Embed:
        cancelled = event.status == EventStatus.CANCELLED
        embed = goblin_embed(
            f"{'🚫' if cancelled else '🔒'} Roll call #{event.id} {event.status}",
            colour=ORANGE if cancelled else GOLD,
        )
        embed.add_field(name="Checked in", value=str(event.entry_count))
        embed.add_field(name="Awards", value=str(event.award_count))
        embed.add_field(name="Total pending", value=sol(event.awarded_lamports))
        if event.awaiting_wallet_count:
            embed.add_field(name="Missing wallets", value=str(event.awaiting_wallet_count))
        embed.add_field(
            name="Closed",
            value=f"{event.close_reason} · {ts(event.closed_at or utcnow())}",
            inline=False,
        )
        if result and result.shared_wallets:
            lines = [
                f"`{shorten_wallet(wallet)}` ← " + ", ".join(f"<@{d}>" for d in discord_ids)
                for wallet, discord_ids in list(result.shared_wallets.items())[:10]
            ]
            embed.add_field(
                name="👀 Shared wallets (review, not proof of abuse)",
                value="\n".join(lines),
                inline=False,
            )
            embed.colour = MAGENTA
        if not cancelled and event.award_count:
            embed.add_field(
                name="Next step",
                value="Click **Approve all** below (head mods), or review one by one with "
                "`/tip list`. Members without a wallet are approved once they add one.",
                inline=False,
            )
        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RollCallCog(bot))
