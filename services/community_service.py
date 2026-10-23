"""Roll-call attendance stats, streaks and goblin ranks. Kept separate from money on purpose."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from database.models import DiscordAccount, Event, EventEntry, EventKind, EventStatus
from database.session import Database


@dataclass(frozen=True)
class Rank:
    min_checkins: int
    title: str


# Earned purely by showing up to roll calls.
RANKS: tuple[Rank, ...] = (
    Rank(0, "🪨 Rock Dweller"),
    Rank(1, "🧌 Goblin Peasant"),
    Rank(5, "🍄 Goblin Scavenger"),
    Rank(10, "🗡️ Goblin Raider"),
    Rank(25, "💎 Goblin Treasurer"),
    Rank(50, "🔥 Goblin Warlord"),
    Rank(100, "👑 Goblin King"),
)


def rank_for(checkins: int) -> tuple[Rank, Rank | None]:
    """The member's rank and the next one (None at the top)."""
    current = RANKS[0]
    for rank in RANKS:
        if checkins >= rank.min_checkins:
            current = rank
    following = next((r for r in RANKS if r.min_checkins > checkins), None)
    return current, following


@dataclass(frozen=True)
class MemberStats:
    discord_user_id: int
    username: str
    checkins: int
    current_streak: int  # consecutive most-recent roll calls attended
    best_streak: int
    position: int  # 1-based leaderboard position

    @property
    def rank(self) -> Rank:
        return rank_for(self.checkins)[0]


def _runs(indexes: list[int], last_index: int) -> tuple[int, int]:
    """(current, best) streak from sorted roll-call positions a member attended."""
    best = run = 0
    previous = None
    for index in indexes:
        run = run + 1 if previous is not None and index == previous + 1 else 1
        best = max(best, run)
        previous = index
    current = run if indexes and indexes[-1] == last_index else 0
    return current, best


class CommunityService:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def all_stats(self) -> list[MemberStats]:
        """Everyone who has attended a completed roll call, best first."""
        async with self._db.transaction() as session:
            event_ids = list(
                await session.scalars(
                    select(Event.id)
                    .where(Event.kind == EventKind.ROLLCALL, Event.status == EventStatus.CLOSED)
                    .order_by(Event.id)
                )
            )
            if not event_ids:
                return []
            rows = await session.execute(
                select(EventEntry.event_id, DiscordAccount.discord_user_id, DiscordAccount.username)
                .join(DiscordAccount, DiscordAccount.user_id == EventEntry.user_id)
                .where(EventEntry.event_id.in_(event_ids))
            )
            position_of = {event_id: index for index, event_id in enumerate(event_ids)}
            attended: dict[int, list[int]] = {}
            names: dict[int, str] = {}
            for event_id, discord_id, username in rows:
                attended.setdefault(discord_id, []).append(position_of[event_id])
                names[discord_id] = username

        stats = []
        for discord_id, indexes in attended.items():
            indexes.sort()
            current, best = _runs(indexes, len(event_ids) - 1)
            stats.append((discord_id, names[discord_id], len(indexes), current, best))
        stats.sort(key=lambda s: (-s[2], -s[3], -s[4], s[1].lower()))
        return [
            MemberStats(discord_id, name, checkins, current, best, position)
            for position, (discord_id, name, checkins, current, best) in enumerate(stats, 1)
        ]

    async def member_stats(self, discord_user_id: int) -> MemberStats | None:
        return next(
            (s for s in await self.all_stats() if s.discord_user_id == discord_user_id), None
        )
