"""Mock players: fake participants for testing lobbies on a server with too few real people.

They are real rows in `players` (flagged `is_mock`, with negative Discord IDs that can never match
a real user) so every code path is the real one. They are driven by admin commands instead of
buttons, never get pinged, and can be removed with `clear`. Disabled unless explicitly enabled.
"""

import random
from dataclasses import dataclass

from bot.features.lobby.repository import READY_CHECK
from bot.features.lobby.service import LobbyService, LobbyUpdate
from bot.features.players.proficiency import ROLE_NAMES_PT, ROLES
from bot.features.players.repository import Player
from bot.features.players.service import PlayerError, PlayerService
from bot.features.ratings.rank import DIVISIONS, TIERS
from bot.features.ratings.service import RatingService


@dataclass(frozen=True)
class FillResult:
    players: list[Player]
    events: list[str]  # lobby events caused by the joins (e.g. ready_check_started)


DISABLED = "Os jogadores de teste estão desativados (ENABLE_MOCK_PLAYERS)."


class MockPlayers:
    def __init__(
        self,
        players: PlayerService,
        ratings: RatingService,
        lobbies: LobbyService,
        enabled: bool,
        rng: random.Random | None = None,
    ) -> None:
        self.players = players
        self.ratings = ratings
        self.lobbies = lobbies
        self.enabled = enabled
        self.rng = rng or random.Random()

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise PlayerError(DISABLED)

    async def all_players(self) -> list[Player]:
        self._require_enabled()
        return await self.players.repo.list_mock()

    async def describe(self, player: Player) -> str:
        """ "Mock 01 — Jungle 5, Top 4 · Gold II": the profile a test is about."""
        lines = await self.players.role_lines(player.id)
        known = sorted(
            ((line.proficiency.value, line.role) for line in lines if line.proficiency.value),
            key=lambda item: (-item[0], ROLES.index(item[1])),
        )
        roles = ", ".join(f"{ROLE_NAMES_PT[role]} {value}" for value, role in known[:3])
        rank = (await self.ratings.resolve(player.id)).rank
        return f"{player.nickname} — {roles} · {rank.label() if rank else 'sem rank'}"

    async def create(self, count: int, admin_id: int, main_role: str | None = None) -> list[Player]:
        """Create `count` mock players with a random League rank and role comfort.

        With `main_role` they all main that role (proficiency 5), to test lobbies where many
        people want the same role; their other roles are random.
        """
        self._require_enabled()
        if not 1 <= count <= 30:
            raise PlayerError("Escolhe entre 1 e 30 jogadores de teste.")
        if main_role is not None and main_role not in ROLES:
            raise PlayerError("Role inválida.")
        existing = await self.players.repo.list_mock()
        number = max((-p.discord_id for p in existing), default=0)
        created = []
        for _ in range(count):
            number += 1
            player = await self.players.register(
                -number, f"Mock {number:02d}", registered_by=admin_id, is_mock=True
            )
            tier = self.rng.choices(range(len(TIERS)), weights=[1, 3, 5, 6, 5, 3, 2], k=1)[0]
            await self.ratings.set_self_reported_rank(
                player.id, TIERS[tier], self.rng.choice(DIVISIONS), admin_id
            )
            await self._random_proficiency(player, main_role)
            created.append(player)
        return created

    async def _random_proficiency(self, player: Player, main_role: str | None) -> None:
        main = main_role or self.rng.choice(ROLES)
        for role in ROLES:
            value = 5 if role == main else self.rng.randint(1, 4)
            await self.players.repo.set_declared(player.id, role, value, self.players.clock())

    async def fill(self, lobby_id: int, count: int, admin_id: int) -> "FillResult":
        """Put `count` mock players into the lobby, creating more mocks if needed."""
        self._require_enabled()
        free = []
        already_in = {
            e.player_id for e in await self.lobbies.repo.entries(lobby_id) if e.status != "dropped"
        }
        for player in await self.players.repo.list_mock():
            busy = await self.lobbies.repo.busy_lobby_of(player.id, lobby_id)
            if busy is None and player.id not in already_in:
                free.append(player)
        if len(free) < count:
            free += await self.create(count - len(free), admin_id)
        joined = []
        events: list[str] = []
        for player in free[:count]:
            update = await self.lobbies.join_player(lobby_id, player)
            joined.append(player)
            events += [e for e in update.events if e != "joined"]
        return FillResult(joined, events)

    async def ready_all(self, lobby_id: int) -> LobbyUpdate:
        """Confirm every mock player in the current ready-check (real players still press Ready)."""
        self._require_enabled()
        snapshot = await self.lobbies.snapshot(lobby_id)
        if snapshot.lobby.status != READY_CHECK:
            raise PlayerError("Não há ready-check a decorrer.")
        update = None
        for entry in snapshot.roster:
            if entry.is_mock and entry.status == "signed_up":
                update = await self.lobbies.ready_player(lobby_id, entry.player_id)
        if update is None:
            raise PlayerError("Não há jogadores de teste por confirmar.")
        return update

    async def drop_all(self, lobby_id: int) -> LobbyUpdate:
        """Make every mock leave (to test backfill and the return to OPEN)."""
        self._require_enabled()
        snapshot = await self.lobbies.snapshot(lobby_id)
        update = None
        for entry in [*snapshot.roster, *snapshot.reserves]:
            if entry.is_mock:
                update = await self.lobbies.leave_player(lobby_id, entry.player_id)
        if update is None:
            raise PlayerError("Não há jogadores de teste nesta lobby.")
        return update

    async def clear(self) -> int:
        """Delete all mock players. Refused while any of them is in an unfinished lobby."""
        self._require_enabled()
        for player in await self.players.repo.list_mock():
            if await self.lobbies.repo.active_lobby_of(player.id) is not None:
                raise PlayerError("Há jogadores de teste em lobbies ativas. Cancela-as primeiro.")
        return await self.players.repo.delete_mock_players()
