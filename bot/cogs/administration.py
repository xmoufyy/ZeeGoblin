"""/admin audit | presence | whoami."""

from __future__ import annotations

import json

import discord
from discord import app_commands
from discord.ext import commands

from bot.branding import BLACK, GOLD, PURPLE, goblin_embed, ts
from bot.helpers import actor_of, bot_of, defer, reply
from bot.presence import TEXT_KEY, TYPE_KEY, build_activity
from bot.views.confirmation_view import send_paginated
from config.permissions import Permission, require
from config.settings import ActivityType

ENTITY_CHOICES = [
    app_commands.Choice(name=name, value=name)
    for name in ("award", "event", "wallet", "batch", "payment", "dispute", "setting")
]


def _compact(raw: str | None, limit: int = 160) -> str:
    if not raw:
        return ""
    text = ", ".join(f"{k}={v}" for k, v in json.loads(raw).items())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class AdminCog(commands.GroupCog, group_name="admin", group_description="Staff tools"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="audit", description="Browse the audit trail")
    @app_commands.describe(
        entity_type="Filter by record type",
        entity_id="Filter by record ID",
        user="Filter by the staff member/user who acted",
    )
    @app_commands.choices(entity_type=ENTITY_CHOICES)
    async def audit(
        self,
        interaction: discord.Interaction,
        entity_type: app_commands.Choice[str] | None = None,
        entity_id: str | None = None,
        user: discord.Member | None = None,
        page: int = 1,
    ) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.AUDIT_VIEW)
        await defer(interaction)
        audit = bot_of(interaction).services.audit

        async def render(number: int) -> tuple[discord.Embed, int]:
            result = await audit.list(
                actor,
                entity_type=entity_type.value if entity_type else None,
                entity_id=entity_id,
                actor_filter=user.id if user else None,
                page=number,
                page_size=8,
            )
            lines = []
            for entry in result.items:
                who = f"<@{entry.actor_discord_id}>" if entry.actor_discord_id else "ZeeGoblin"
                line = (
                    f"`#{entry.id}` {ts(entry.created_at, 'f')} · **{entry.action}** "
                    f"{entry.entity_type} #{entry.entity_id} · {who}"
                )
                change = _compact(entry.after_json)
                if change:
                    line += f"\n  ↳ {change}"
                if entry.reason:
                    line += f"\n  ↳ reason: {entry.reason[:120]}"
                lines.append(line)
            embed = goblin_embed(
                "🧾 Audit trail", "\n".join(lines) or "No matching records.", colour=BLACK
            )
            embed.set_footer(
                text=f"Page {result.page}/{result.pages} • {result.total} records • append-only"
            )
            return embed, result.pages

        await send_paginated(interaction, render, max(1, page))

    @app_commands.command(name="presence", description="Owner: change ZeeGoblin's activity")
    @app_commands.choices(
        activity=[app_commands.Choice(name=a.value, value=a.value) for a in ActivityType]
    )
    async def presence(
        self,
        interaction: discord.Interaction,
        activity: app_commands.Choice[str],
        text: app_commands.Range[str, 1, 128],
    ) -> None:
        actor = actor_of(interaction)
        require(actor, Permission.ADMIN_CONFIG)
        await defer(interaction)
        bot = bot_of(interaction)
        await bot.services.settings.set(TYPE_KEY, activity.value, actor.discord_id)
        await bot.services.settings.set(TEXT_KEY, text, actor.discord_id)
        await bot.change_presence(activity=build_activity(ActivityType(activity.value), text))
        await reply(
            interaction,
            embed=goblin_embed(
                "🎰 Presence updated", f"Now **{activity.value}** {text}", colour=GOLD
            ),
        )

    @app_commands.command(name="whoami", description="Show the ZeeGoblin roles you hold")
    async def whoami(self, interaction: discord.Interaction) -> None:
        actor = actor_of(interaction)
        embed = goblin_embed("🧌 Your goblin clearance", colour=PURPLE)
        embed.add_field(name="Roles", value=", ".join(sorted(r.value for r in actor.roles)))
        embed.add_field(
            name="Permissions",
            value="\n".join(sorted(p.value for p in actor.permissions)) or "—",
            inline=False,
        )
        await reply(interaction, embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
