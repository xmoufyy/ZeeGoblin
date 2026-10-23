"""The ZeeGoblin Discord client: startup, recovery, command sync, shutdown."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.events import on_app_command_error, register_lifecycle_logging, send_error
from bot.presence import load_activity
from bot.views.approval_view import ApproveEventButton
from bot.views.rollcall_view import CheckInButton
from config.settings import Settings
from services.container import Services

log = logging.getLogger("zeegoblin")

EXTENSIONS = (
    "bot.cogs.wallet",
    "bot.cogs.profile",
    "bot.cogs.rollcall",
    "bot.cogs.tips",
    "bot.cogs.payments",
    "bot.cogs.administration",
    "bot.cogs.promo",
    "bot.cogs.staff",
    "bot.cogs.fun",
)


class ZeeTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        client = interaction.client
        if interaction.guild_id != client.settings.discord_guild_id:  # type: ignore[attr-defined]
            await send_error(interaction, "ZeeGoblin only works inside the Dude-Munity server.")
            return False
        return True


class ZeeGoblinBot(commands.Bot):
    def __init__(self, settings: Settings, services: Services) -> None:
        intents = discord.Intents.default()
        intents.message_content = settings.enable_message_checkin
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            tree_cls=ZeeTree,
            help_command=None,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        self.settings = settings
        self.services = services
        self._background: set[asyncio.Task[None]] = set()
        self.tree.on_error = on_app_command_error
        register_lifecycle_logging(self)

    async def setup_hook(self) -> None:
        # Persistent check-in buttons for every roll call, past and future.
        self.add_dynamic_items(CheckInButton, ApproveEventButton)
        for extension in EXTENSIONS:
            await self.load_extension(extension)

        report = await self.services.recovery.recover()
        if report.finalized:
            log.info("Finalized %d roll call(s) that expired while offline", len(report.finalized))

        self.activity = await load_activity(self.settings, self.services.settings)

        if self.settings.sync_commands_on_startup:
            # Commands are registered to the one configured guild only (instant updates,
            # never global). setup_hook runs once per process, not on reconnects.
            guild = discord.Object(id=self.settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            # Only re-register when the command definitions changed: Discord rate-limits
            # registration, and quick restarts otherwise stall startup for up to a minute.
            payload = [
                command.to_dict(self.tree) for command in self.tree.get_commands(guild=guild)
            ]
            digest = hashlib.sha256(
                json.dumps(payload, sort_keys=True, default=str).encode()
            ).hexdigest()
            hash_key = f"commands.hash.{guild.id}"
            if await self.services.settings.get(hash_key) == digest:
                log.info("Slash commands unchanged; skipping registration")
                return
            try:
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %d slash command group(s) to guild %s", len(synced), guild.id)
                await self.services.settings.set(hash_key, digest, None)
            except discord.Forbidden:
                log.error(
                    "Missing access to register commands in guild %s. Was the bot invited with "
                    "the applications.commands scope, and is DISCORD_GUILD_ID correct?",
                    guild.id,
                )
                raise

    def dm_members(self, messages: dict[int, discord.Embed]) -> None:
        """DM members in the background (MEMBER_DMS). Closed DMs are skipped silently."""
        if not self.settings.member_dms or not messages:
            return
        task = asyncio.create_task(self._send_dms(messages))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _send_dms(self, messages: dict[int, discord.Embed]) -> None:
        for discord_id, embed in messages.items():
            try:
                user = self.get_user(discord_id) or await self.fetch_user(discord_id)
                await user.send(embed=embed)
            except Exception as exc:  # closed DMs, deleted users, network blips: never fatal
                log.debug("Could not DM %s: %s", discord_id, exc)
            await asyncio.sleep(1)  # stay well inside Discord's DM rate limits

    async def notify_staff(self, embed: discord.Embed, view: discord.ui.View | None = None) -> bool:
        """Post to the private staff channel if one is configured. Never raises."""
        channel_id = self.settings.staff_channel_id
        if not channel_id:
            return False
        try:
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            if view is None:
                await channel.send(embed=embed)  # type: ignore[union-attr]
            else:
                await channel.send(embed=embed, view=view)  # type: ignore[union-attr]
            return True
        except discord.HTTPException as exc:
            log.warning("Could not post to staff channel %s: %s", channel_id, exc)
            return False

    async def close(self) -> None:
        log.info("Shutting down ZeeGoblin…")
        await super().close()  # cogs unload here; the database is disposed by main.run()
