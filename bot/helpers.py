"""Small glue between Discord interactions and the service layer."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import discord

from config.permissions import Actor, resolve_roles
from services.accounts import DiscordIdentity

if TYPE_CHECKING:
    from bot.client import ZeeGoblinBot


def bot_of(interaction: discord.Interaction) -> ZeeGoblinBot:
    return cast("ZeeGoblinBot", interaction.client)


def identity_of(user: discord.abc.User) -> DiscordIdentity:
    display = getattr(user, "display_name", None)
    return DiscordIdentity(user.id, user.name, display if display != user.name else None)


def actor_of(interaction: discord.Interaction) -> Actor:
    """Resolve roles from the member data Discord sends with *this* interaction."""
    settings = bot_of(interaction).settings
    user = interaction.user
    role_ids = [role.id for role in getattr(user, "roles", [])]
    roles = resolve_roles(
        user.id,
        role_ids,
        owner_user_id=settings.owner_user_id,
        head_mod_role_id=settings.head_mod_role_id,
        mod_role_id=settings.mod_role_id,
        payment_mod_role_id=settings.payment_mod_role_id,
    )
    return Actor(user.id, roles, user.name)


async def reply(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    files: list[discord.File] | None = None,
    ephemeral: bool = True,
) -> discord.WebhookMessage | None:
    kwargs: dict = {"content": content, "ephemeral": ephemeral}
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view
    if files:
        kwargs["files"] = files
    if interaction.response.is_done():
        return await interaction.followup.send(wait=True, **kwargs)
    await interaction.response.send_message(**kwargs)
    return None


async def defer(interaction: discord.Interaction, *, ephemeral: bool = True) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)
