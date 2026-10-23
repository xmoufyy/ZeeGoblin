"""Bot activity ("Playing Gambling on Solpump"), configurable by env or owner command."""

from __future__ import annotations

import discord

from config.settings import ActivityType, Settings
from services.container import SettingsStore

TYPE_KEY = "presence.type"
TEXT_KEY = "presence.text"

_DISCORD_TYPES = {
    ActivityType.WATCHING: discord.ActivityType.watching,
    ActivityType.LISTENING: discord.ActivityType.listening,
    ActivityType.COMPETING: discord.ActivityType.competing,
}


def build_activity(kind: ActivityType, text: str) -> discord.BaseActivity:
    if kind == ActivityType.PLAYING:
        return discord.Game(name=text)
    if kind == ActivityType.CUSTOM:
        return discord.CustomActivity(name=text)
    return discord.Activity(type=_DISCORD_TYPES[kind], name=text)


async def load_activity(settings: Settings, store: SettingsStore) -> discord.BaseActivity:
    """An owner override saved in the database wins over the .env default."""
    kind = await store.get(TYPE_KEY)
    text = await store.get(TEXT_KEY)
    try:
        resolved = ActivityType(kind) if kind else settings.bot_activity_type
    except ValueError:
        resolved = settings.bot_activity_type
    return build_activity(resolved, text or settings.bot_activity_text)
