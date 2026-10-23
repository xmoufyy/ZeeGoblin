from __future__ import annotations

from services.community_service import RANKS, _runs, rank_for
from tests.conftest import HEAD_MOD, MOD, member


def test_streak_runs() -> None:
    assert _runs([], 4) == (0, 0)
    assert _runs([0, 1, 2], 2) == (3, 3)
    assert _runs([0, 1, 2, 4], 4) == (1, 3)  # missed #3, came back for #4
    assert _runs([0, 1, 3, 4, 5], 6) == (0, 3)  # missed the latest roll call


def test_ranks() -> None:
    assert rank_for(0)[0].title == RANKS[0].title
    assert rank_for(1)[0].title == "🧌 Goblin Peasant"
    current, following = rank_for(9)
    assert current.title == "🍄 Goblin Scavenger" and following.min_checkins == 10
    assert rank_for(250) == (RANKS[-1], None)


async def test_leaderboard_from_real_rollcalls(services, clock) -> None:
    attendance = [{1, 2, 3}, {1, 2}, {1, 3}]  # member 1 attends everything
    for index, present in enumerate(attendance):
        event = await services.rollcalls.start(
            MOD, guild_id=1, channel_id=1, duration_minutes=1, reward_lamports=1_000
        )
        for discord_id in present:
            await services.rollcalls.check_in(event.id, member(discord_id))
        if index == 2:
            # A cancelled roll call never counts toward stats.
            await services.rollcalls.end(HEAD_MOD, event.id, award=False)
        else:
            clock.advance(minutes=2)
            await services.rollcalls.finalize_due()

    stats = await services.community.all_stats()
    assert [(s.discord_user_id, s.checkins, s.current_streak, s.position) for s in stats] == [
        (1, 2, 2, 1),
        (2, 2, 2, 2),
        (3, 1, 0, 3),
    ]
    assert (await services.community.member_stats(3)).best_streak == 1
    assert await services.community.member_stats(999) is None
