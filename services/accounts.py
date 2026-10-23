"""Shared helpers for resolving Discord members to ZeeVerse users."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import DiscordAccount, User, Wallet


@dataclass(frozen=True)
class DiscordIdentity:
    discord_id: int
    username: str
    display_name: str | None = None


async def ensure_user(session: AsyncSession, identity: DiscordIdentity) -> User:
    """Return the user for a Discord account, creating it on first sight."""
    account = await session.scalar(
        select(DiscordAccount).where(DiscordAccount.discord_user_id == identity.discord_id)
    )
    if account is not None:
        username = identity.username[:100]
        display = identity.display_name[:100] if identity.display_name else None
        if username and (account.username != username or account.display_name != display):
            account.username = username
            account.display_name = display
        user = await session.get(User, account.user_id)
        assert user is not None
        return user

    user = User()
    session.add(user)
    await session.flush()
    session.add(
        DiscordAccount(
            user_id=user.id,
            discord_user_id=identity.discord_id,
            username=(identity.username or str(identity.discord_id))[:100],
            display_name=identity.display_name[:100] if identity.display_name else None,
        )
    )
    await session.flush()
    return user


async def find_user_id(session: AsyncSession, discord_id: int) -> int | None:
    return await session.scalar(
        select(DiscordAccount.user_id).where(DiscordAccount.discord_user_id == discord_id)
    )


async def active_wallet(session: AsyncSession, user_id: int) -> Wallet | None:
    return await session.scalar(
        select(Wallet).where(Wallet.user_id == user_id, Wallet.is_active.is_(True))
    )


async def active_wallets(session: AsyncSession, user_ids: list[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    rows = await session.execute(
        select(Wallet.user_id, Wallet.address).where(
            Wallet.user_id.in_(user_ids), Wallet.is_active.is_(True)
        )
    )
    return {user_id: address for user_id, address in rows}


async def discord_ids_for(session: AsyncSession, user_ids: list[int]) -> dict[int, tuple[int, str]]:
    """user_id -> (discord_user_id, username)."""
    if not user_ids:
        return {}
    rows = await session.execute(
        select(
            DiscordAccount.user_id, DiscordAccount.discord_user_id, DiscordAccount.username
        ).where(DiscordAccount.user_id.in_(user_ids))
    )
    return {user_id: (discord_id, username) for user_id, discord_id, username in rows}
