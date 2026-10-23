"""Shared fixtures: every test gets its own migrated SQLite file and a controllable clock."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config.permissions import Actor, Role
from config.settings import Limits
from database.migrate import upgrade_to_head
from database.session import Database
from services.accounts import DiscordIdentity
from services.container import Services, build_services
from utils.validation import BASE58_ALPHABET

OWNER_ID = 1000
HEAD_MOD_ID = 2000
MOD_ID = 3000
PAYMENT_MOD_ID = 4000
PAYMENT_MOD_2_ID = 4001


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def make_address(seed: int) -> str:
    """Deterministic valid Solana address (Base58 of 32 bytes)."""
    number = int.from_bytes(bytes([(seed * 7 + i) % 251 + 1 for i in range(32)]), "big")
    chars = []
    while number:
        number, rem = divmod(number, 58)
        chars.append(BASE58_ALPHABET[rem])
    return "".join(reversed(chars))


def make_signature(seed: int) -> str:
    number = int.from_bytes(bytes([(seed * 13 + i) % 251 + 1 for i in range(64)]), "big")
    chars = []
    while number:
        number, rem = divmod(number, 58)
        chars.append(BASE58_ALPHABET[rem])
    return "".join(reversed(chars))


def member(discord_id: int, name: str | None = None) -> DiscordIdentity:
    return DiscordIdentity(discord_id, name or f"member{discord_id}")


OWNER = Actor(OWNER_ID, frozenset({Role.OWNER, Role.MEMBER}), "zee")
HEAD_MOD = Actor(HEAD_MOD_ID, frozenset({Role.HEAD_MOD, Role.MEMBER}), "headmod")
MOD = Actor(MOD_ID, frozenset({Role.MOD, Role.MEMBER}), "mod")
PAYMENT_MOD = Actor(PAYMENT_MOD_ID, frozenset({Role.PAYMENT_MOD, Role.MEMBER}), "payer")
PAYMENT_MOD_2 = Actor(PAYMENT_MOD_2_ID, frozenset({Role.PAYMENT_MOD, Role.MEMBER}), "payer2")


def plain_member(discord_id: int) -> Actor:
    return Actor(discord_id, frozenset({Role.MEMBER}), f"member{discord_id}")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def limits() -> Limits:
    return Limits()


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    upgrade_to_head(url)
    return url


@pytest.fixture
async def db(db_url: str) -> AsyncIterator[Database]:
    database = Database(db_url)
    yield database
    await database.dispose()


@pytest.fixture
def services(db: Database, limits: Limits, clock: FakeClock) -> Services:
    return build_services(db, limits, clock)
