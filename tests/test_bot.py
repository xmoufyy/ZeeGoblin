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
from database.models import AwardStatus
from services.container import Services
from services.errors import PermissionDenied
from tests.conftest import HEAD_MOD, MOD, PAYMENT_MOD, make_address, member
from tests.conftest import OWNER as OWNER_ACTOR

GUILD_ID = 123456789

EXPECTED_COMMANDS = {
    "wallet": {"set", "view", "remove"},
    "profile": set(),
    "rollcall": {"start", "status", "end", "history"},
    "tip": {
        "list",
        "view",
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
    "batch": {"list", "view", "cancel"},
    "payment": {"record", "confirm", "history"},
    "admin": {"audit", "presence", "whoami"},
    "solpump": set(),
    "staff": set(),
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
        self.edits: list[dict[str, object]] = []

    def is_done(self) -> bool:
        return self.done

    async def defer(self, **_: object) -> None:
        self.done = True

    async def send_modal(self, modal: object) -> None:
        self.done = True
        self.modal = modal

    async def edit_message(self, **kwargs: object) -> None:
        self.done = True
        self.edits.append(kwargs)

    async def send_message(self, content: str | None = None, **kwargs: object) -> None:
        self.done = True
        embed = kwargs.get("embed")
        self.messages.append(content or (embed.description if embed else ""))


class FakeFollowup:
    def __init__(self, sink: list[str]) -> None:
        self.sink = sink

        self.views: list[object] = []

    async def send(self, content: str | None = None, **kwargs: object) -> None:
        embed = kwargs.get("embed")
        self.sink.append(content or (embed.description if embed else ""))
        if kwargs.get("view") is not None:
            self.views.append(kwargs["view"])


def fake_interaction(  # type: ignore[no-untyped-def]
    bot: ZeeGoblinBot, user_id: int, guild_id: int = GUILD_ID, role_ids: tuple[int, ...] = ()
):
    response = FakeResponse()
    user = SimpleNamespace(
        id=user_id,
        name=f"user{user_id}",
        display_name=f"User {user_id}",
        roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
    )
    return SimpleNamespace(
        client=bot,
        guild_id=guild_id,
        user=user,
        response=response,
        followup=FakeFollowup(response.messages),
        command=None,
        id=1,
        edit_original_response=response.edit_message,
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


def test_solpump_promo_card(tmp_path) -> None:
    from bot.cogs.promo import find_artwork, promo_embed, promo_view

    assert find_artwork(tmp_path) is None
    (tmp_path / "solpump.png").write_bytes(b"\x89PNG")
    art = find_artwork(tmp_path)
    assert art is not None and promo_embed(art).image.url == "attachment://solpump.png"
    [button] = promo_view("https://solpump.io/a/ZEEDUDE").children
    assert button.label == "Gamble On Solpump" and button.url == "https://solpump.io/a/ZEEDUDE"


def test_custom_status_activity() -> None:
    from bot.presence import build_activity
    from config.settings import ActivityType

    activity = build_activity(ActivityType.CUSTOM, "🎰 Gamble on Solpump")
    assert isinstance(activity, discord.CustomActivity) and activity.name == "🎰 Gamble on Solpump"


async def test_tip_list_approve_buttons(bot: ZeeGoblinBot, services: Services) -> None:
    head_mod_id, head_mod_role = 2000, 2  # role ID 2 = HEAD_MOD_ROLE_ID in the bot fixture
    for discord_id in (61, 62, head_mod_id):
        await services.wallets.set_wallet(member(discord_id), make_address(discord_id))
        await services.tips.add_manual(MOD, member(discord_id), 10_000_000, f"Call {discord_id}")

    cog = bot.get_cog("TipCog")
    interaction = fake_interaction(bot, head_mod_id, role_ids=(head_mod_role,))
    await cog.list_awards.callback(cog, interaction, None, None, 1)
    [view] = interaction.followup.views
    # The head mod's own award is listed but can't be picked for approval.
    assert sorted(o.value for o in view.picker.options) == sorted(
        str(row.id)
        for row in (await services.tips.list_awards(OWNER_ACTOR)).items
        if row.discord_user_id != head_mod_id
    )

    click = fake_interaction(bot, head_mod_id, role_ids=(head_mod_role,))
    await view.approve_page.callback(click)
    assert "Approved 2 award(s)" in click.response.messages[0]
    remaining = await services.tips.list_awards(OWNER_ACTOR, status=AwardStatus.PENDING_REVIEW)
    assert [row.discord_user_id for row in remaining.items] == [head_mod_id]
    assert view.picker not in view.children  # nothing left this user may approve

    # A member who somehow clicks gets nothing approved: roles are re-checked per click.
    await services.tips.add_manual(MOD, member(63), 10_000_000, "Call 63")
    await services.wallets.set_wallet(member(63), make_address(63))
    sneaky = fake_interaction(bot, head_mod_id)  # same user, head-mod role removed
    summary = await view._approve(
        sneaky,
        [
            (await services.tips.list_awards(OWNER_ACTOR, status=AwardStatus.PENDING_REVIEW))
            .items[0]
            .id
        ],
    )
    assert "Not approved" in summary


HEAD_MOD_ROLE, PAYMENT_MOD_ROLE, MOD_ROLE = 2, 4, 3  # role IDs used by the bot fixture


async def test_staff_dashboard_buttons_follow_permissions(bot, services) -> None:
    from bot.cogs.staff import build_dashboard

    await services.wallets.set_wallet(member(70), make_address(70))
    award = await services.tips.add_manual(MOD, member(70), 10_000_000, "Clip")
    _, mod_buttons = await build_dashboard(bot, MOD, GUILD_ID)
    labels = [label for label, *_ in mod_buttons]
    assert labels[0] == "🕵️ Review pending (1)"
    assert not any("Export" in label for label in labels)  # mods can't export

    await services.tips.approve(HEAD_MOD, award.id)
    _, payer_buttons = await build_dashboard(bot, PAYMENT_MOD, GUILD_ID)
    export = next(b for b in payer_buttons if b[0].startswith("📦 Export"))
    assert export[0] == "📦 Export batch (1)" and export[3] is False  # enabled

    # Clicking "Review pending" runs the real /tip list handler.
    click = fake_interaction(bot, MOD.discord_id, role_ids=(MOD_ROLE,))
    await mod_buttons[0][2](click)
    assert click.response.messages  # list was sent


async def test_pay_batch_from_menus(bot, services) -> None:
    from bot.views.payment_view import BatchPayView, ConfirmPaymentView
    from tests.conftest import make_signature

    for discord_id in (71, 72):
        await services.wallets.set_wallet(member(discord_id), make_address(discord_id))
        award = await services.tips.add_manual(MOD, member(discord_id), 20_000_000, "Win")
        await services.tips.approve(HEAD_MOD, award.id)
    detail = await services.payments.create_batch(PAYMENT_MOD)
    view = BatchPayView(PAYMENT_MOD.discord_id, detail, PAYMENT_MOD)
    download, picker = view.children
    assert download.label == "📄 Download CSVs"
    assert {o.value for o in picker.options} == {make_address(71), make_address(72)}
    assert not BatchPayView(HEAD_MOD.discord_id, detail, HEAD_MOD).has_actions  # view-only

    pick = fake_interaction(bot, PAYMENT_MOD.discord_id, role_ids=(PAYMENT_MOD_ROLE,))
    picker._values = [make_address(71)]  # what Discord sets when an option is chosen
    await picker.callback(pick)
    modal = pick.response.modal
    assert modal.lamports == 20_000_000  # exact amount prefilled, nothing to type
    modal.signature._value = make_signature(71)  # what Discord fills in on submit
    submit = fake_interaction(bot, PAYMENT_MOD.discord_id, role_ids=(PAYMENT_MOD_ROLE,))
    await modal.on_submit(submit)
    assert submit.followup.views, submit.response.messages
    [confirm_view] = submit.followup.views
    assert isinstance(confirm_view, ConfirmPaymentView)

    click = fake_interaction(bot, PAYMENT_MOD.discord_id, role_ids=(PAYMENT_MOD_ROLE,))
    await confirm_view.confirm_button.callback(click)
    rows = (await services.tips.list_awards(OWNER_ACTOR, status=AwardStatus.PAID)).items
    assert [r.display_wallet for r in rows] == [make_address(71)]


async def test_staff_summary_approve_all_button(bot, services, clock) -> None:
    from bot.views.approval_view import ApproveEventButton, approve_event_view

    await services.wallets.set_wallet(member(80), make_address(80))
    event = await services.rollcalls.start(
        MOD, guild_id=GUILD_ID, channel_id=1, duration_minutes=1, reward_lamports=10_000_000
    )
    await services.rollcalls.check_in(event.id, member(80))
    await services.rollcalls.check_in(event.id, member(81))  # no wallet
    clock.advance(minutes=2)
    [result] = await services.rollcalls.finalize_due()
    [button] = approve_event_view(result.event).children
    assert button.item.label == "✅ Approve all (1)"

    denied = fake_interaction(bot, MOD.discord_id, role_ids=(MOD_ROLE,))
    await ApproveEventButton(event.id).callback(denied)
    assert "permission" in denied.response.messages[0].lower()

    ok = fake_interaction(bot, HEAD_MOD.discord_id, role_ids=(HEAD_MOD_ROLE,))
    await ApproveEventButton(event.id).callback(ok)
    assert "Approved **1**" in ok.response.messages[0]


async def test_id_autocomplete(bot, services) -> None:
    from bot.autocomplete import batch_choices

    await services.wallets.set_wallet(member(90), make_address(90))
    award = await services.tips.add_manual(MOD, member(90), 10_000_000, "x")
    await services.tips.approve(HEAD_MOD, award.id)
    detail = await services.payments.create_batch(PAYMENT_MOD)
    payer = fake_interaction(bot, PAYMENT_MOD.discord_id, role_ids=(PAYMENT_MOD_ROLE,))
    [choice] = await batch_choices(payer, "")
    assert choice.value == detail.batch.id and "open" in choice.name
    assert await batch_choices(fake_interaction(bot, 5), "") == []  # members see nothing


async def test_review_fixes(bot, services, clock, monkeypatch) -> None:
    """Regression tests for issues found in the full review."""
    from bot.views.approval_view import ApproveEventButton
    from bot.views.payment_view import BatchPayView, confirm_and_report
    from tests.conftest import make_signature

    dms: list[dict[int, discord.Embed]] = []
    monkeypatch.setattr(bot, "dm_members", lambda messages: dms.append(messages))
    staff_posts: list[discord.Embed] = []

    async def fake_notify(embed, view=None):  # type: ignore[no-untyped-def]
        staff_posts.append(embed)
        return True

    monkeypatch.setattr(bot, "notify_staff", fake_notify)
    monkeypatch.setattr(bot.settings, "staff_channel_id", 999)

    # 1. Closing a roll call DMs participants who still need a wallet, once.
    cog = bot.get_cog("RollCallCog")

    class FakeMessage:
        async def edit(self, **kwargs: object) -> None:
            pass

    cog._message = lambda event: FakeMessage()  # type: ignore[method-assign]
    await services.wallets.set_wallet(member(100), make_address(100))
    event = await services.rollcalls.start(
        MOD, guild_id=GUILD_ID, channel_id=1, duration_minutes=1, reward_lamports=10_000_000
    )
    await services.rollcalls.check_in(event.id, member(100))
    await services.rollcalls.check_in(event.id, member(101))  # no wallet
    clock.advance(minutes=2)
    await cog.scheduler.coro(cog)
    assert [list(m) for m in dms] == [[101]]
    assert "You've got SOL waiting" in dms[0][101].title

    # 2. Publishing again (e.g. /rollcall end racing the scheduler) never re-posts to staff.
    closed = await services.rollcalls.get(event.id)
    await cog.publish_closure(closed, None)
    await cog.scheduler.coro(cog)
    assert len(staff_posts) == 1
    assert "pending review when they add one" in staff_posts[0].fields[-1].value

    # 3. The Approve-all button retires itself after a successful click.
    click = fake_interaction(bot, HEAD_MOD.discord_id, role_ids=(HEAD_MOD_ROLE,))
    await ApproveEventButton(event.id).callback(click)
    [edit] = click.response.edits
    [retired] = edit["view"].children
    assert retired.disabled and retired.label.startswith("✅ Approved by")

    # 4. Confirming a payment DMs the member with a Solscan link.
    dms.clear()
    detail = await services.payments.create_batch(PAYMENT_MOD)
    record = await services.payments.record_payment(
        PAYMENT_MOD,
        batch_id=detail.batch.id,
        wallet=make_address(100),
        amount_lamports=10_000_000,
        signature=make_signature(100),
    )
    payer = fake_interaction(bot, PAYMENT_MOD.discord_id, role_ids=(PAYMENT_MOD_ROLE,))
    await confirm_and_report(payer, record.payment.id)
    [paid_dm] = dms
    assert "solscan.io/tx/" in paid_dm[100].description and "0.01 SOL" in paid_dm[100].description

    # 5. "Download CSVs" sends both files privately (replaces /batch export).
    sent: dict[str, object] = {}

    async def capture(content=None, **kwargs):  # type: ignore[no-untyped-def]
        sent.update(kwargs)

    download_click = fake_interaction(bot, PAYMENT_MOD.discord_id, role_ids=(PAYMENT_MOD_ROLE,))
    download_click.followup.send = capture
    view = BatchPayView(PAYMENT_MOD.discord_id, detail, PAYMENT_MOD)
    await view.children[0].callback(download_click)
    assert [f.filename for f in sent["files"]] == [
        f"zeegoblin-batch-{detail.batch.id}.csv",
        f"zeegoblin-batch-{detail.batch.id}-wallet-summary.csv",
    ]
