"""ZeeGoblin entry point: `python main.py`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from pydantic import ValidationError

MIN_PYTHON = (3, 12)


def _load_settings():  # type: ignore[no-untyped-def]
    from config.settings import get_settings

    try:
        return get_settings()
    except ValidationError as exc:
        print("ZeeGoblin configuration error - check your .env file:\n", file=sys.stderr)
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"]).upper() or "(settings)"
            print(f"  - {field}: {error['msg']}", file=sys.stderr)
        print("\nSee .env.example and README.md for every setting.", file=sys.stderr)
        raise SystemExit(2) from None


async def run() -> None:
    import discord

    from bot.client import ZeeGoblinBot
    from database.migrate import upgrade_to_head
    from database.session import Database
    from services.container import build_services
    from utils.logging import configure_logging

    settings = _load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(
        settings.log_level,
        settings.data_dir / "logs",
        secrets=[settings.discord_token.get_secret_value()],
    )
    log = logging.getLogger("zeegoblin")
    log.info("Starting ZeeGoblin with %s", settings.safe_summary())

    # Schema migrations are additive and backed up first; safe to run on every start.
    await asyncio.to_thread(upgrade_to_head, settings.database_url, settings.data_dir / "backups")

    db = Database(settings.database_url)
    services = build_services(db, settings.limits())
    token = settings.discord_token.get_secret_value()
    try:
        try:
            async with ZeeGoblinBot(settings, services) as bot:
                await bot.start(token)
        except discord.PrivilegedIntentsRequired:
            if not settings.enable_message_checkin:
                raise
            # Typed "present" check-ins are optional; keep the bot running with buttons only.
            log.warning(
                "ENABLE_MESSAGE_CHECKIN=true but the Message Content intent is off in the "
                "Developer Portal (Bot > Privileged Gateway Intents). Continuing with button "
                "check-ins only."
            )
            settings = settings.model_copy(update={"enable_message_checkin": False})
            async with ZeeGoblinBot(settings, services) as bot:
                await bot.start(token)
    except discord.LoginFailure:
        log.error(
            "Discord rejected the bot token. Reset it in the Developer Portal and update .env."
        )
        raise SystemExit(3) from None
    except discord.PrivilegedIntentsRequired:
        log.error("Discord requires a privileged intent that is not enabled for this bot.")
        raise SystemExit(4) from None
    finally:
        await db.dispose()
        log.info("ZeeGoblin stopped")


def main() -> None:
    if sys.version_info < MIN_PYTHON:
        raise SystemExit(f"ZeeGoblin needs Python {'.'.join(map(str, MIN_PYTHON))}+")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
