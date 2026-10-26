"""Lobby rules: sign-up queue, ready-check, team proposals, confirmation, cancellation.

State machine (spontaneous lobby):

    OPEN --(10 signed up)--> READY_CHECK --(all 10 ready)--> TEAMS_PROPOSED --confirm--> IN_PROGRESS
      ^                          |  deadline / decline: drop, backfill from reserves, new round
      +--- fewer than 10 left ---+          anyone may be cancelled by the creator or an admin

The roster is *derived*: the first ten non-dropped sign-ups by time, so there is no race over who
is tenth. Every transition is a compare-and-set in the repository, and each lobby also has an
asyncio lock so multi-step reactions (reconcile) cannot interleave.
"""

import asyncio
import json
import logging
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from bot.features.balance.engine import balance
from bot.features.balance.model import BalanceParams, PlayerInput, Proposal, balance_band
from bot.features.lobby.repository import (
    ACTIVE,
    BLOCKING,
    CANCELLED,
    IN_PROGRESS,
    OPEN,
    READY,
    READY_CHECK,
    SIGNED_UP,
    TEAMS_PROPOSED,
    EntryRow,
    LobbyRepository,
    LobbyRow,
    OpenLobbyExists,
)
from bot.features.players.repository import Player
from bot.features.players.service import PlayerError, PlayerService
from bot.features.ratings.service import RatingService

log = logging.getLogger(__name__)

ROSTER_SIZE = 10


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class LobbyParams:
    ready_check_seconds: int = 120
    idle_hours: float = 3.0  # an unfinished lobby with no activity for this long is cancelled
    in_progress_hours: float = 12.0  # until results exist (M3), a started game expires after this


@dataclass(frozen=True)
class EntryView:
    player_id: int
    discord_id: int
    nickname: str
    is_mock: bool
    status: str


@dataclass(frozen=True)
class SlotView:
    player_id: int
    nickname: str
    role: str
    fit: int | None  # the player's proficiency (1-5) on the role they were given
    best: int | None  # their proficiency on their best role (fit < best = off their best)


@dataclass(frozen=True)
class ProposalView:
    blue: list[SlotView]
    red: list[SlotView]
    band: str  # even | slight | clear | lopsided
    favoured: str | None  # "blue" / "red" / None when even


@dataclass(frozen=True)
class LobbySnapshot:
    lobby: LobbyRow
    roster: list[EntryView]
    reserves: list[EntryView]
    proposal: ProposalView | None


@dataclass(frozen=True)
class LobbyUpdate:
    snapshot: LobbySnapshot
    events: list[str] = field(default_factory=list)


class LobbyService:
    def __init__(
        self,
        repo: LobbyRepository,
        players: PlayerService,
        ratings: RatingService,
        is_admin: Callable[[int], bool],
        params: LobbyParams | None = None,
        balance_params: BalanceParams | None = None,
        clock: Callable[[], datetime] = _utcnow,
        rng: random.Random | None = None,
    ) -> None:
        self.repo = repo
        self.players = players
        self.ratings = ratings
        self.is_admin = is_admin
        self.params = params or LobbyParams()
        self.balance_params = balance_params or BalanceParams()
        self.clock = clock
        self.rng = rng or random.Random()
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, lobby_id: int) -> asyncio.Lock:
        return self._locks.setdefault(lobby_id, asyncio.Lock())

    # --- opening ----

    async def open(self, guild_id: int, channel_id: int, discord_id: int) -> LobbyUpdate:
        player = await self.players.require(discord_id)
        elsewhere = await self.repo.active_lobby_of(player.id)
        if elsewhere is not None:
            raise PlayerError("Já estás numa lobby ativa. Sai dela primeiro.")
        try:
            lobby = await self.repo.create(
                guild_id, channel_id, "spontaneous", discord_id, self.clock()
            )
        except OpenLobbyExists:
            raise PlayerError(
                "Já existe uma lobby aberta neste servidor. Usa /lobby show."
            ) from None
        return await self.join_player(lobby.id, player)

    async def attach_message(self, lobby_id: int, message_id: int, channel_id: int | None = None):
        await self.repo.set_message(lobby_id, message_id, channel_id)

    async def lobby_for_message(self, message_id: int) -> LobbyRow | None:
        return await self.repo.get_by_message(message_id)

    async def open_lobby(self, guild_id: int) -> LobbyRow | None:
        return await self.repo.open_for_guild(guild_id)

    # --- membership ----

    async def join(self, lobby_id: int, discord_id: int) -> LobbyUpdate:
        return await self.join_player(lobby_id, await self.players.require(discord_id))

    async def join_player(self, lobby_id: int, player: Player) -> LobbyUpdate:
        async with self._lock(lobby_id):
            lobby = await self._require(lobby_id)
            if lobby.status not in BLOCKING:
                raise PlayerError("Esta lobby já não aceita jogadores.")
            elsewhere = await self.repo.active_lobby_of(player.id)
            if elsewhere is not None and elsewhere.id != lobby_id:
                raise PlayerError("Já estás noutra lobby ativa.")
            now = self.clock()
            if not await self.repo.join(lobby_id, player.id, now):
                raise PlayerError("Já estás nesta lobby.")
            await self.repo.touch(lobby_id, now)
            return await self._after_change(lobby_id, ["joined"])

    async def leave(self, lobby_id: int, discord_id: int) -> LobbyUpdate:
        return await self.leave_player(lobby_id, (await self.players.require(discord_id)).id)

    async def leave_player(self, lobby_id: int, player_id: int) -> LobbyUpdate:
        async with self._lock(lobby_id):
            lobby = await self._require(lobby_id)
            if lobby.status not in BLOCKING:
                raise PlayerError("O jogo já começou ou a lobby fechou.")
            now = self.clock()
            if not await self.repo.set_entry_status(
                lobby_id, player_id, (SIGNED_UP, READY), "dropped"
            ):
                raise PlayerError("Não estás nesta lobby.")
            await self.repo.touch(lobby_id, now)
            return await self._after_change(lobby_id, ["left"])

    async def ready(self, lobby_id: int, discord_id: int) -> LobbyUpdate:
        return await self.ready_player(lobby_id, (await self.players.require(discord_id)).id)

    async def ready_player(self, lobby_id: int, player_id: int) -> LobbyUpdate:
        async with self._lock(lobby_id):
            lobby = await self._require(lobby_id)
            if lobby.status != READY_CHECK:
                raise PlayerError("Não há ready-check a decorrer.")
            roster = self._roster(await self.repo.entries(lobby_id))
            if player_id not in {e.player_id for e in roster}:
                raise PlayerError("Não estás entre os 10 jogadores desta lobby.")
            if not await self.repo.set_entry_status(lobby_id, player_id, (SIGNED_UP,), READY):
                raise PlayerError("Já confirmaste.")
            await self.repo.touch(lobby_id, self.clock())
            return await self._after_change(lobby_id, ["readied"])

    # --- creator / admin actions ----

    def _require_actor(self, lobby: LobbyRow, discord_id: int) -> None:
        if discord_id != lobby.created_by and not self.is_admin(discord_id):
            raise PlayerError("Só o criador da lobby ou um admin pode fazer isto.")

    async def confirm(self, lobby_id: int, discord_id: int) -> LobbyUpdate:
        async with self._lock(lobby_id):
            lobby = await self._require(lobby_id)
            self._require_actor(lobby, discord_id)
            data = json.loads(lobby.proposal_json or "{}")
            if lobby.status != TEAMS_PROPOSED or not data.get("current"):
                raise PlayerError("Não há equipas válidas para confirmar.")
            if not await self.repo.transition(
                lobby_id, [TEAMS_PROPOSED], IN_PROGRESS, self.clock(), ready_deadline=None
            ):
                raise PlayerError("Estas equipas já foram confirmadas.")
            return LobbyUpdate(await self.snapshot(lobby_id), ["confirmed"])

    async def cancel(
        self, lobby_id: int, discord_id: int, reason: str = "cancelada"
    ) -> LobbyUpdate:
        async with self._lock(lobby_id):
            lobby = await self._require(lobby_id)
            self._require_actor(lobby, discord_id)
            return await self._cancel(lobby, reason)

    async def _cancel(self, lobby: LobbyRow, reason: str) -> LobbyUpdate:
        now = self.clock()
        if not await self.repo.transition(
            lobby.id,
            ACTIVE,
            CANCELLED,
            now,
            closed_at=now,
            cancel_reason=reason,
            ready_deadline=None,
        ):
            raise PlayerError("Esta lobby já está fechada.")
        return LobbyUpdate(await self.snapshot(lobby.id), ["cancelled"])

    # --- time-driven transitions (called by the sweeper) ----

    async def process_deadlines(self, now: datetime | None = None) -> list[LobbyUpdate]:
        """Ready-checks past their deadline: drop whoever did not confirm and carry on."""
        now = now or self.clock()
        updates = []
        for lobby in await self.repo.list_by_status([READY_CHECK]):
            if lobby.ready_deadline is None or lobby.ready_deadline > now:
                continue
            async with self._lock(lobby.id):
                current = await self.repo.get(lobby.id)
                if (
                    current is None
                    or current.status != READY_CHECK
                    or current.ready_deadline is None
                    or current.ready_deadline > now
                ):
                    continue
                for entry in self._roster(await self.repo.entries(lobby.id)):
                    if entry.status != READY:
                        await self.repo.set_entry_status(
                            lobby.id, entry.player_id, (SIGNED_UP,), "dropped"
                        )
                updates.append(await self._after_change(lobby.id, ["timeout"]))
        return updates

    async def expire_idle(self, now: datetime | None = None) -> list[LobbyUpdate]:
        now = now or self.clock()
        updates = []
        idle = timedelta(hours=self.params.idle_hours)
        started = timedelta(hours=self.params.in_progress_hours)
        for lobby in await self.repo.list_by_status(ACTIVE):
            limit = started if lobby.status == IN_PROGRESS else idle
            if now - lobby.updated_at < limit:
                continue
            async with self._lock(lobby.id):
                current = await self.repo.get(lobby.id)
                if current is None or current.status not in ACTIVE:
                    continue
                reason = "sem resultado" if current.status == IN_PROGRESS else "inatividade"
                try:
                    updates.append(await self._cancel(current, reason))
                except PlayerError:
                    continue
        return updates

    # --- reading ----

    async def snapshot(self, lobby_id: int) -> LobbySnapshot:
        lobby = await self._require(lobby_id)
        entries = [e for e in await self.repo.entries(lobby_id) if e.status != "dropped"]
        people = await self.players.repo.get_many([e.player_id for e in entries])
        views = [
            EntryView(
                e.player_id,
                people[e.player_id].discord_id,
                people[e.player_id].nickname,
                people[e.player_id].is_mock,
                e.status,
            )
            for e in entries
            if e.player_id in people
        ]
        proposal = await self._proposal_view(lobby, views)
        return LobbySnapshot(lobby, views[:ROSTER_SIZE], views[ROSTER_SIZE:], proposal)

    async def _proposal_view(
        self, lobby: LobbyRow, views: Sequence[EntryView]
    ) -> ProposalView | None:
        if lobby.status not in (TEAMS_PROPOSED, IN_PROGRESS) or not lobby.proposal_json:
            return None
        data = json.loads(lobby.proposal_json)
        if not data.get("current"):
            return None
        proposal = Proposal.from_dict(data["current"])
        names = {v.player_id: v.nickname for v in views}
        profiles: dict[int, dict[str, int | None]] = {}
        for slot in (*proposal.blue, *proposal.red):
            lines = await self.players.role_lines(slot.player_id)
            profiles[slot.player_id] = {line.role: line.proficiency.value for line in lines}

        def slots(side):
            out = []
            for s in side:
                known = [v for v in profiles[s.player_id].values() if v is not None]
                out.append(
                    SlotView(
                        s.player_id,
                        names.get(s.player_id, "?"),
                        s.role,
                        profiles[s.player_id].get(s.role),
                        max(known) if known else None,
                    )
                )
            return out

        p = proposal.blue_win_prob
        favoured = None if balance_band(p) == "even" else ("blue" if p > 0.5 else "red")
        return ProposalView(slots(proposal.blue), slots(proposal.red), balance_band(p), favoured)

    # --- internals ----

    async def _require(self, lobby_id: int) -> LobbyRow:
        lobby = await self.repo.get(lobby_id)
        if lobby is None:
            raise PlayerError("Essa lobby não existe.")
        return lobby

    @staticmethod
    def _roster(entries: Sequence[EntryRow]) -> list[EntryRow]:
        return [e for e in entries if e.status != "dropped"][:ROSTER_SIZE]

    async def _after_change(self, lobby_id: int, events: list[str]) -> LobbyUpdate:
        await self._reconcile(lobby_id, events)
        return LobbyUpdate(await self.snapshot(lobby_id), events)

    async def _reconcile(self, lobby_id: int, events: list[str]) -> None:
        """Align the lobby state with its roster after any change (caller holds the lock)."""
        lobby = await self._require(lobby_id)
        now = self.clock()
        roster = self._roster(await self.repo.entries(lobby_id))
        ids = [e.player_id for e in roster]
        full = len(roster) >= ROSTER_SIZE
        deadline = now + timedelta(seconds=self.params.ready_check_seconds)

        if lobby.status == OPEN:
            starting = full and lobby.kind == "spontaneous"
            if starting and await self.repo.transition(
                lobby_id,
                [OPEN],
                READY_CHECK,
                now,
                ready_deadline=deadline,
                roster_json=json.dumps(ids),
            ):
                events.append("ready_check_started")
            return

        if lobby.status not in (READY_CHECK, TEAMS_PROPOSED):
            return
        if not full:
            await self.repo.reset_ready(lobby_id)
            if await self.repo.transition(
                lobby_id,
                [READY_CHECK, TEAMS_PROPOSED],
                OPEN,
                now,
                ready_deadline=None,
                roster_json=None,
                proposal_json=None,
            ):
                events.append("back_to_open")
            return
        previous = set(json.loads(lobby.roster_json or "[]"))
        if set(ids) != previous:
            # Someone left or was replaced from the reserves: everyone gets a fresh round.
            if await self.repo.transition(
                lobby_id,
                [READY_CHECK, TEAMS_PROPOSED],
                READY_CHECK,
                now,
                ready_deadline=deadline,
                roster_json=json.dumps(ids),
                proposal_json=None,
            ):
                events.append("roster_changed")
            return
        everyone_ready = lobby.status == READY_CHECK and all(e.status == READY for e in roster)
        if everyone_ready and await self._propose(lobby, roster):
            events.append("teams_proposed")

    async def _inputs(self, roster: Sequence[EntryRow]) -> list[PlayerInput]:
        inputs = []
        for entry in roster:
            estimate = (await self.ratings.inspect(entry.player_id)).estimate
            lines = await self.players.role_lines(entry.player_id)
            inputs.append(
                PlayerInput(
                    entry.player_id,
                    estimate.mu,
                    estimate.sigma,
                    {line.role: line.proficiency.value for line in lines},
                )
            )
        return inputs

    async def _propose(self, lobby: LobbyRow, roster: Sequence[EntryRow]) -> bool:
        """Balance the roster and store the best proposal; True if there is one to show.

        Roles come only from each player's profile proficiency. The search takes a fraction of a
        second, so it runs in a worker thread instead of stalling the bot.
        """
        inputs = await self._inputs(roster)
        result = await asyncio.to_thread(balance, inputs, self.balance_params, self.rng)
        if not result.proposals:
            log.error("Balancer found no proposal for lobby %s: %s", lobby.id, result.problems)
            return False
        payload = {"current": result.proposals[0].to_dict()}
        return await self.repo.transition(
            lobby.id,
            [READY_CHECK],
            TEAMS_PROPOSED,
            self.clock(),
            proposal_json=json.dumps(payload),
            ready_deadline=None,
        )
