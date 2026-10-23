"""Community fun: /leaderboard, /rank, /roast. Nothing here touches SOL or rewards."""

from __future__ import annotations

import random

import discord
from discord import app_commands
from discord.ext import commands

from bot.branding import GOLD, MAGENTA, PURPLE, goblin_embed
from bot.helpers import bot_of
from services.community_service import MemberStats, rank_for

NO_PINGS = discord.AllowedMentions.none()
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

# Playful, casino/goblin-flavoured, and never about who someone *is* (looks, identity, etc.).
ROASTS = (
    "{name} checks in to roll calls faster than they check their bank balance. Smart, honestly.",
    "{name}'s wallet has more dust than a goblin cave.",
    "I've seen {name}'s stream calls. The rocket crashed out of second-hand embarrassment.",
    "{name} brings 'I'M HERE!' energy and 'where am I?' results.",
    "{name} is the reason the goblin treasury has an 'Are you sure?' button.",
    "{name} called 100x once. It was the font size.",
    "Even the 3-minute roll call timer is more patient than {name}'s chat spam.",
    "If excuses were SOL, {name} would finally be in profit.",
    "{name} treats every stream like a tutorial and still skips the tutorial.",
    "The goblin council has voted. {name} is officially *mid*, and it wasn't close.",
    "{name} has main-character energy in a stream where they're an extra.",
    "{name} asked for tips so often the goblins learned their name before Zee did.",
    "{name}'s hot takes are colder than a hardware wallet in a freezer.",
    "{name} would lose a staring contest to a loading screen.",
    "{name} clicked I'M HERE! and the goblins still marked them 'missing in brain'.",
    "Somewhere a goblin is counting coins. {name} is counting on vibes.",
    "{name}'s strategy is 'trust me bro' with extra steps.",
    "{name} is proof that confidence and accuracy are two different stats.",
    "{name} types 'LFG' like it's a personality.",
    "The goblins tried to roast {name}, but chat already did it better.",
    "{name}'s predictions come with a free side of copium.",
    "{name} shows up to every hype train and somehow still misses the station.",
)


def stats_line(stats: MemberStats) -> str:
    medal = MEDALS.get(stats.position, f"`#{stats.position}`")
    streak = f" · 🔥 {stats.current_streak}" if stats.current_streak > 1 else ""
    return (
        f"{medal} <@{stats.discord_user_id}> · **{stats.checkins}** roll calls{streak} · "
        f"{stats.rank.title}"
    )


def progress_bar(value: int, start: int, end: int, width: int = 10) -> str:
    filled = round(width * (value - start) / max(1, end - start))
    return "▰" * filled + "▱" * (width - filled)


class FunCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="leaderboard", description="Top roll-call regulars in the Dude-Munity"
    )
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.channel_id)
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        everyone = await bot_of(interaction).services.community.all_stats()
        if not everyone:
            await interaction.response.send_message(
                "🧌 No roll calls completed yet. The throne is empty… for now."
            )
            return
        lines = [stats_line(s) for s in everyone[:10]]
        mine = next((s for s in everyone if s.discord_user_id == interaction.user.id), None)
        if mine and mine.position > 10:
            lines += ["⋯", stats_line(mine)]
        embed = goblin_embed("👑 Goblin Leaderboard", "\n".join(lines), colour=GOLD)
        embed.set_footer(text="Roll-call attendance only • 🔥 = current streak • /rank for yours")
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_PINGS)

    @app_commands.command(name="rank", description="Your goblin rank, streak and leaderboard spot")
    @app_commands.describe(member="Check someone else's rank")
    @app_commands.checks.cooldown(3, 30)
    async def rank(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        target = member or interaction.user
        stats = await bot_of(interaction).services.community.member_stats(target.id)
        checkins = stats.checkins if stats else 0
        current, following = rank_for(checkins)

        embed = goblin_embed(f"{current.title}", colour=PURPLE)
        embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
        embed.add_field(name="Roll calls", value=f"**{checkins}**")
        if stats:
            embed.add_field(name="Leaderboard", value=f"#{stats.position}")
            embed.add_field(
                name="Streak", value=f"🔥 {stats.current_streak} (best {stats.best_streak})"
            )
        if following:
            embed.add_field(
                name=f"Next: {following.title}",
                value=f"{progress_bar(checkins, current.min_checkins, following.min_checkins)} "
                f"{following.min_checkins - checkins} more roll call(s)",
                inline=False,
            )
        else:
            embed.add_field(name="👑 Max rank", value="Bow down, goblins.", inline=False)
        if not stats:
            embed.description = "No roll calls yet. Smash **I'M HERE!** next time Zee calls! 👑"
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_PINGS)

    @app_commands.command(name="roast", description="Let ZeeGoblin roast someone (all in fun)")
    @app_commands.checks.cooldown(1, 60)
    async def roast(self, interaction: discord.Interaction, member: discord.Member) -> None:
        settings = bot_of(interaction).settings
        if self.bot.user and member.id == self.bot.user.id:
            text = "Roast **me**? I'm a goblin made of code and spite. Nice try. 🧌"
        elif member.id == settings.owner_user_id:
            text = "Roast **Zee**? Bold. The goblin respects it… but I'd like to keep my job. 👑"
        elif member.bot:
            text = f"{member.mention} is a bot. Roasting it would be bullying a toaster."
        else:
            line = random.choice(ROASTS).format(name=member.mention)
            text = f"Self-roast? Respect. 🫡\n{line}" if member == interaction.user else line
        embed = goblin_embed("🔥 Goblin Roast", text, colour=MAGENTA)
        embed.set_footer(text=f"Requested by {interaction.user.display_name} • all in fun")
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_PINGS)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FunCog(bot))
