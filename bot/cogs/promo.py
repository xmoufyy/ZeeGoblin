"""/solpump - Zee's Solpump promo card with artwork and a link button.

Kept separate from roll calls and tips on purpose: rewards never depend on gambling.
"""

from __future__ import annotations

from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from bot.branding import PURPLE, goblin_embed
from bot.helpers import bot_of
from config.settings import PROJECT_ROOT

ART_NAMES = ("solpump.png", "solpump.jpg", "solpump.jpeg", "solpump.webp")


def find_artwork(folder: Path = PROJECT_ROOT / "assets") -> Path | None:
    return next((folder / name for name in ART_NAMES if (folder / name).is_file()), None)


def promo_view(url: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Gamble On Solpump", emoji="🎰", style=discord.ButtonStyle.link, url=url
        )
    )
    return view


def promo_embed(artwork: Path | None) -> discord.Embed:
    embed = goblin_embed(
        "🎰 GAMBLE ON SOLPUMP WITH ZEE",
        "Join Zee on **Solpump** using the Dude-Munity link below. 👑\n\n"
        "-# 18+ only, where legal. Gamble responsibly. Roll calls and tips are always free "
        "and never require gambling.",
        colour=PURPLE,
    )
    if artwork is not None:
        embed.set_image(url=f"attachment://{artwork.name}")
    return embed


class PromoCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="solpump", description="Post Zee's Solpump link")
    @app_commands.checks.cooldown(1, 120, key=lambda i: i.channel_id)
    async def solpump(self, interaction: discord.Interaction) -> None:
        artwork = find_artwork()
        kwargs: dict = {
            "embed": promo_embed(artwork),
            "view": promo_view(bot_of(interaction).settings.solpump_url),
        }
        if artwork is not None:
            kwargs["file"] = discord.File(artwork, filename=artwork.name)
        await interaction.response.send_message(**kwargs)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PromoCog(bot))
