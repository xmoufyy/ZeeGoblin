"""Offline checks of the Discord layer: command tree, button callback, closure publishing.

These never connect to Discord. Real-server behaviour is covered by the manual checklist in
README.md ("Verifying a new install").
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import discord
import pytest

from bot.client import EXTENSIONS, ZeeGoblinBot
from bot.events import on_app_command_error
from bot.views.batch_view import batch_summary_embed
from bot.views.rollcall_view import CheckInButton, active_embed, closed_embed
from config.settings import Settings
from services.container import Services
from services.errors import PermissionDenied
from tests.conftest import HEAD_MOD, MOD, PAYMENT_MOD, make_address, member

GUILD_ID = 123456789

EXPECTED_COMMANDS = {
    "wallet": {"set", "view", "remove"},
    "profile": set(),
    "rollcall": {"start", "status", "end", "history"},
    "tip": {
        "list",
        "view",
        "status",
        "history",
        "add",
        "approve",
        "approve-event",
        "void",
        "flag",
        "rewallet",
        "export-batch",
    },
    "dispute": {"list", "resolve"},
    "batch": {"list", "view", "export", "cancel"},
    "payment": {"record", "confirm", "history"},
    "admin": {"audit", "presence", "whoami"},
}


@pytest.fixture
async def bot(services: Services) -> AsyncIterator[ZeeGoblinBot]:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        discord_token="test.token.value",
        discord_guild_id=GUILD_ID,
        owner_user_id=1,
        head_mod_role_id=2,
        mod_role_id=3,
        payment_mod_role_id=4,
    )
    client = ZeeGoblinBot(settings, services)
    async with client:  # initialises the client without logging in
        for extension in EXTENSIONS:
            await client.load_extension(extension)
        yield client
        for extension in list(client.extensions):
            await client.unload_extension(extension)


def test_every_declared_command_is_registered(bot: ZeeGoblinBot) -> None:
    tree = {}
    for command in bot.tree.get_commands():
        children = getattr(command, "commands", [])
        tree[command.name] = {child.name for child in children}
        payload = command.to_dict(bot.tree)  # validates names, descriptions, option types
        assert payload["name"] == command.name
    assert tree == EXPECTED_COMMANDS


class FakeResponse:
    def __init__(self) -> None:
        self.done = False
        self.messages: list[str] = []

    def is_done(self) -> bool:
        return self.done

    async def defer(self, **_: object) -> None:
        self.done = True

    async def send_message(self, content: str | None = None, **kwargs: object) -> None:
        self.done = True
        embed = kwargs.get("embed")
        self.messages.append(content or (embed.description if embed else ""))


class FakeFollowup:
    def __init__(self, sink: list[str]) -> None:
        self.sink = sink

    async def send(self, content: str | None = None, **kwargs: object) -> None:
        embed = kwargs.get("embed")
        self.sink.append(content or (embed.description if embed else ""))


def fake_interaction(bot: ZeeGoblinBot, user_id: int, guild_id: int = GUILD_ID):  # type: ignore[no-untyped-def]
    response = FakeResponse()
    user = SimpleNamespace(
        id=user_id, name=f"user{user_id}", display_name=f"User {user_id}", roles=[]
    )
    return SimpleNamespace(
        client=bot,
        guild_id=guild_id,
        user=user,
        response=response,
        followup=FakeFollowup(response.messages),
        command=None,
        id=1,
    )


async def test_checkin_button_flow(bot: ZeeGoblinBot, services: Services, clock) -> None:
    event = await services.rollcalls.start(
        MOD, guild_id=GUILD_ID, channel_id=1, duration_minutes=3, reward_lamports=10_000_000
    )
    button = CheckInButton(event.id)
    assert button.item.custom_id == f"zeegoblin:rollcall:{event.id}"

    first = fake_interaction(bot, 50)
    await button.callback(first)
    assert "YOU'RE HERE" in first.response.messages[0]
    assert "/wallet set" in first.response.messages[0]  # no wallet yet

    again = fake_interaction(bot, 50)
    await button.callback(again)
    assert "already checked in" in again.response.messages[0]

    clock.advance(minutes=5)
    late = fake_interaction(bot, 51)
    await button.callback(late)
    assert "closed" in late.response.messages[0]

    wrong_guild = fake_interaction(bot, 52, guild_id=999)
    await button.callback(wrong_guild)
    assert "isn't for this server" in wrong_guild.response.messages[0]


async def test_dynamic_button_restores_from_custom_id(bot: ZeeGoblinBot) -> None:
    match = CheckInButton.__discord_ui_compiled_template__.fullmatch("zeegoblin:rollcall:42")
    restored = await CheckInButton.from_custom_id(None, None, match)  # type: ignore[arg-type]
    assert restored.event_id == 42


async def test_closure_is_published_once(bot: ZeeGoblinBot, services: Services, clock) -> None:
    cog = bot.get_cog("RollCallCog")
    edits: list[discord.Embed] = []

    class FakeMessage:
        async def edit(self, **kwargs: object) -> None:
            edits.append(kwargs["embed"])  # type: ignore[arg-type]
            assert kwargs.get("view", "missing") is None  # button removed on close

    cog._message = lambda event: FakeMessage()  # type: ignore[method-assign]
    event = await services.rollcalls.start(
        MOD, guild_id=GUILD_ID, channel_id=1, duration_minutes=1, reward_lamports=10_000_000
    )
    await services.rollcalls.attach_message(event.id, 777)
    await services.rollcalls.check_in(event.id, member(5))
    clock.advance(minutes=2)

    await cog.scheduler.coro(cog)  # one scheduler tick
    assert len(edits) == 1 and "CLOSED" in edits[0].title
    await cog.scheduler.coro(cog)
    assert len(edits) == 1  # synced flag prevents repeats
    assert await services.rollcalls.unsynced_closed_events() == []


async def test_error_handler_messages(bot: ZeeGoblinBot) -> None:
    interaction = fake_interaction(bot, 5)
    error = discord.app_commands.CommandInvokeError(
        SimpleNamespace(name="approve", qualified_name="tip approve"),  # type: ignore[arg-type]
        PermissionDenied("You don't have permission to do that."),
    )
    await on_app_command_error(interaction, error)  # type: ignore[arg-type]
    assert "permission" in interaction.response.messages[0].lower()

    crash = fake_interaction(bot, 5)
    boom = discord.app_commands.CommandInvokeError(
        SimpleNamespace(name="x", qualified_name="x"),  # type: ignore[arg-type]
        RuntimeError("secret internals"),
    )
    await on_app_command_error(crash, boom)  # type: ignore[arg-type]
    assert "secret internals" not in crash.response.messages[0]


async def test_embeds_fit_discord_limits(services: Services, clock) -> None:
    event = await services.rollcalls.start(
        MOD, guild_id=GUILD_ID, channel_id=1, duration_minutes=3, reward_lamports=10_000_000
    )
    assert len(active_embed(event)) < 6000
    clock.advance(minutes=4)
    [result] = await services.rollcalls.finalize_due()
    assert len(closed_embed(result.event)) < 6000

    for i in range(40):
        await services.wallets.set_wallet(member(100 + i), make_address(100 + i))
        award = await services.tips.add_manual(MOD, member(100 + i), 1_000 + i, "x" * 300)
        await services.tips.approve(HEAD_MOD, award.id)
    detail = await services.payments.create_batch(PAYMENT_MOD)
    embed = batch_summary_embed(detail)
    assert len(embed) < 6000 and all(len(f.value) <= 1024 for f in embed.fields)
