"""Roster rules: registration, role proficiency and the profile view model."""

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from bot.features.players.proficiency import ROLES, EffectiveProficiency, effective_proficiency
from bot.features.players.repository import (
    AlreadyRegistered,
    NicknameTaken,
    Player,
    PlayerRepository,
)
from bot.features.ratings.ladder import Standing
from bot.features.ratings.rank import RankResolution
from bot.features.ratings.service import RatingService

NICKNAME_MIN, NICKNAME_MAX = 2, 24


class PlayerError(Exception):
    """A user-facing problem (message is shown to the user)."""


class AlreadyRegisteredError(PlayerError):
    """The Discord user already has a player row; nothing was changed."""

    def __init__(self, player: Player) -> None:
        super().__init__("Já estás registado. Usa /profile view.")
        self.player = player


def _utcnow() -> datetime:
    return datetime.now(UTC)


def clean_nickname(raw: str) -> str:
    nickname = re.sub(r"\s+", " ", raw).strip()
    if not NICKNAME_MIN <= len(nickname) <= NICKNAME_MAX:
        raise PlayerError(
            f"O nickname tem de ter entre {NICKNAME_MIN} e {NICKNAME_MAX} caracteres."
        )
    if not nickname.isprintable():
        raise PlayerError("O nickname tem caracteres inválidos.")
    return nickname


@dataclass(frozen=True)
class RoleLine:
    role: str
    proficiency: EffectiveProficiency


@dataclass(frozen=True)
class Profile:
    player: Player
    standing: Standing
    resolution: RankResolution
    rank_age_days: float | None
    rank_stale: bool
    roles: list[RoleLine]
    accounts: list[str]  # linked Riot accounts, visible to the group


class PlayerService:
    def __init__(
        self,
        repo: PlayerRepository,
        ratings: RatingService,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.repo = repo
        self.ratings = ratings
        self.clock = clock
        # Hooks set by the optional Riot module; absent means "no estimates, no accounts".
        self.role_estimator: Callable[[int], Awaitable[Mapping[str, int | None]]] | None = None
        self.account_labeler: Callable[[int], Awaitable[list[str]]] | None = None

    async def register(
        self, discord_id: int, nickname: str, registered_by: int | None = None
    ) -> Player:
        """Create the player and its default rating. Used by /register (self) and by the admin
        command (`registered_by` is then the admin, recorded on the seed event).

        An existing player is never modified: the check runs before any validation or write.
        """
        existing = await self.repo.get_by_discord(discord_id)
        if existing is not None:
            raise AlreadyRegisteredError(existing)
        nickname = clean_nickname(nickname)
        try:
            player = await self.repo.create(discord_id, nickname, self.clock())
        except AlreadyRegistered:  # lost a race with another registration of the same user
            raise PlayerError("Já estás registado. Usa /profile view.") from None
        except NicknameTaken:
            raise PlayerError(f"O nickname «{nickname}» já está em uso.") from None
        await self.ratings.ensure_default_seed(player.id, registered_by or discord_id)
        return player

    async def require(self, discord_id: int) -> Player:
        player = await self.repo.get_by_discord(discord_id)
        if player is None or player.left_at is not None:
            raise PlayerError("Esse jogador não está registado. Usa /register primeiro.")
        return player

    async def set_declared(self, discord_id: int, role: str, value: int | None) -> Player:
        player = await self.require(discord_id)
        await self.repo.set_declared(player.id, _check_role(role, value), value, self.clock())
        return player

    async def set_admin_override(self, discord_id: int, role: str, value: int | None) -> Player:
        player = await self.require(discord_id)
        await self.repo.set_admin_override(player.id, _check_role(role, value), value, self.clock())
        return player

    async def profile(self, discord_id: int) -> Profile:
        player = await self.require(discord_id)
        inspection = await self.ratings.inspect(player.id)
        stored_roles = await self.repo.roles(player.id)
        estimates = await self.role_estimator(player.id) if self.role_estimator else {}
        lines = []
        for role in ROLES:
            row = stored_roles.get(role)
            lines.append(
                RoleLine(
                    role,
                    effective_proficiency(
                        row.declared if row else None,
                        row.admin_override if row else None,
                        estimates.get(role),
                    ),
                )
            )
        accounts = await self.account_labeler(player.id) if self.account_labeler else []
        return Profile(
            player,
            inspection.standing,
            inspection.resolution,
            inspection.rank_age_days,
            inspection.rank_stale,
            lines,
            accounts,
        )


def _check_role(role: str, value: int | None) -> str:
    role = role.upper()
    if role not in ROLES:
        raise PlayerError("Role inválida.")
    if value is not None and not 1 <= value <= 5:
        raise PlayerError("A proficiência tem de estar entre 1 e 5.")
    return role
