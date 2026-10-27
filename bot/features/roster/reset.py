"""Take a database back to a clean state: either "roster only" or no league data at all."""

from dataclasses import dataclass

from bot.features.players.repository import PlayerRepository
from bot.features.ratings.service import RatingService
from bot.features.roster.repository import RosterCounts, RosterRepository, WipeCounts


@dataclass(frozen=True)
class ResetPlan:
    games: WipeCounts
    roster: RosterCounts
    mock_players: int
    keep_roster: bool


async def plan_reset(
    repo: RosterRepository, players: PlayerRepository, keep_roster: bool
) -> ResetPlan:
    return ResetPlan(
        await repo.count_games(),
        await repo.count_roster(),
        len(await players.list_mock()),
        keep_roster,
    )


async def reset_league(
    repo: RosterRepository,
    players: PlayerRepository,
    ratings: RatingService,
    keep_roster: bool,
) -> ResetPlan:
    """`keep_roster=False`: delete all league data (players, roles, ranks, Riot accounts, games,
    ratings); seed again afterwards. `keep_roster=True`: delete only the games, lobbies, mock
    players and rating adjustments, and rebuild the ratings from the seeds.

    Either way the name-of-the-day data (names, phrases, picks) is untouched.
    """
    plan = await plan_reset(repo, players, keep_roster)
    if not keep_roster:
        await repo.wipe_league()
        return plan
    await repo.wipe_games()
    await players.delete_mock_players()
    await ratings.rebuild()
    return plan
