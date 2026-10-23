"""Gateway lifecycle logging and the central slash-command error handler."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from bot.branding import RED, Quips, goblin_embed
from services.errors import PermissionDenied, ZeeGoblinError
from utils.currency import AmountError
from utils.validation import ValidationError

log = logging.getLogger("zeegoblin.events")

USER_ERRORS = (ZeeGoblinError, AmountError, ValidationError)


def user_message(error: BaseException) -> str | None:
    """Text for expected errors; None means 'unexpected - log it'."""
    if isinstance(error, PermissionDenied):
        return f"{Quips.NO_PERMISSION}\n-# {error}"
    if isinstance(error, USER_ERRORS):
        return str(error)
    return None


async def send_error(interaction: discord.Interaction, text: str) -> None:
    embed = goblin_embed("Hold up", text, colour=RED)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        log.warning("Could not deliver error message for interaction %s", interaction.id)


async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    if isinstance(error, app_commands.CommandOnCooldown):
        await send_error(interaction, Quips.COOLDOWN.format(seconds=int(error.retry_after) + 1))
        return
    if isinstance(error, app_commands.NoPrivateMessage):
        await send_error(interaction, "ZeeGoblin commands only work inside the server.")
        return
    if isinstance(error, app_commands.CheckFailure):
        await send_error(interaction, Quips.NO_PERMISSION)
        return

    original = getattr(error, "original", error)
    message = user_message(original)
    if message is None:
        command = interaction.command.qualified_name if interaction.command else "?"
        log.error(
            "Unhandled error in /%s (user %s)", command, interaction.user.id, exc_info=original
        )
        message = Quips.UNEXPECTED
    elif isinstance(original, PermissionDenied):
        command = interaction.command.qualified_name if interaction.command else "?"
        log.info("Permission denied: user %s tried /%s", interaction.user.id, command)
    await send_error(interaction, message)


def register_lifecycle_logging(client: discord.Client) -> None:
    @client.event
    async def on_ready() -> None:
        log.info("Connected to Discord as %s (id %s)", client.user, client.user and client.user.id)

    @client.event
    async def on_resumed() -> None:
        log.info("Discord session resumed")

    @client.event
    async def on_disconnect() -> None:
        log.warning("Disconnected from Discord; discord.py will reconnect automatically")
