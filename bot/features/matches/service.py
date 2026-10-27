"""Match rules: report, confirm, contest, void and correct a result; rating updates.

    in_progress --report--> reported --confirm (other team / admin / automatic)--> confirmed
         |                      |  contest: blocks the automatic confirmation until an admin decides
         +---- void (any time, admin; creator while in_progress; automatic after 12 h) ----> voided

Only *confirmed* matches feed the ratings, and every change to the set of confirmed matches (a
confirmation, a correction, a void) rewrites the rating caches by replaying all of them in the
same database transaction, so a result and the ratings it produced appear together or not at all.
Replaying everything is cheap at this scale and cannot drift from an incremental update.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from bot.db import Database
from bot.features.balance.model import Slot
from bot.features.lobby.repository import CANCELLED, COMPLETED, LobbyRepository, LobbyRow
from bot.features.lobby.repository import IN_PROGRESS as LOBBY_IN_PROGRESS
from bot.features.matches.repository import (
    CONFIRMED,
    IN_PROGRESS,
    REPORTED,
    VOIDED,
    MatchRepository,
    MatchRow,
    ParticipantRow,
)
from bot.features.players.service import PlayerError, PlayerService
from bot.features.ratings.repository import StoredRating
from bot.features.ratings.service import RatingService


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class MatchParams:
    auto_confirm_minutes: int = 30  # a reported, uncontested result is confirmed after this
    reminder_minutes: int = 90  # nudge the players to report a result (once)
    expire_hours: float = 12.0  # a game nobody reported is voided after this


@dataclass(frozen=True)
class StandingChange:
    """A player whose games or visible standing moved when the ratings were rebuilt."""

    player_id: int
    before: StoredRating | None
    after: StoredRating


@dataclass(frozen=True)
class MatchChange:
    match: MatchRow
    events: list[str] = field(default_factory=list)
    changes: list[StandingChange] = field(default_factory=list)


def _visible(stored: StoredRating | None) -> tuple:
    if stored is None:
        return ()
    return (stored.games, stored.placed, stored.tier_key, stored.division)


class MatchService:
    def __init__(
        self,
        repo: MatchRepository,
        lobbies: LobbyRepository,
        players: PlayerService,
        ratings: RatingService,
        is_admin: Callable[[int], bool],
        db: Database,
        params: MatchParams | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.repo = repo
        self.lobbies = lobbies
        self.players = players
        self.ratings = ratings
        self.is_admin = is_admin
        self.db = db
        self.params = params or MatchParams()
        self.clock = clock

    # --- creation (called by the lobby, inside its transaction) ----

    async def start(
        self, lobby: LobbyRow, blue: tuple[Slot, ...], red: tuple[Slot, ...], blue_win_prob: float
    ) -> MatchRow:
        """Record the confirmed teams, freezing what the balancer saw for each player."""
        people = []
        for side, slots in (("blue", blue), ("red", red)):
            for slot in slots:
                inspection = await self.ratings.inspect(slot.player_id)
                rank = inspection.resolution.rank
                people.append(
                    ParticipantRow(
                        0,
                        slot.player_id,
                        side,
                        slot.role,
                        inspection.stored.mu,
                        inspection.stored.sigma,
                        inspection.estimate.mu,
                        inspection.estimate.sigma,
                        rank.mu(self.ratings.params) if rank else None,
                    )
                )
        return await self.repo.create(lobby.id, lobby.guild_id, blue_win_prob, self.clock(), people)

    # --- player actions ----

    async def report(self, match_id: int, discord_id: int, winner: str) -> MatchChange:
        """A participant (or an admin, whose report is final at once) says who won."""
        if winner not in ("blue", "red"):
            raise PlayerError("O vencedor tem de ser azul ou vermelho.")
        player = await self.players.require(discord_id)
        admin = self.is_admin(discord_id)
        sides = {p.player_id: p.side for p in await self.repo.participants(match_id)}
        if not admin and player.id not in sides:
            raise PlayerError("Só quem jogou (ou um admin) pode reportar o resultado.")
        now = self.clock()
        async with self.db.transaction():
            match = await self._require(match_id)
            if match.status not in (IN_PROGRESS, REPORTED):
                raise PlayerError("O resultado deste jogo já está fechado.")
            if match.status == REPORTED and not admin and match.reported_by != player.id:
                raise PlayerError("Já há um resultado reportado. Usa Contestar se discordas.")
            if not await self.repo.transition(
                match_id,
                [IN_PROGRESS, REPORTED],
                REPORTED,
                winner=winner,
                reported_by=player.id,
                reported_at=now,
                contested=0,
            ):
                raise PlayerError("O estado do jogo mudou. Tenta outra vez.")
            await self.repo.add_audit(match_id, "report", discord_id, match.winner, winner, now)
            if admin:
                return await self._confirm(
                    match_id, discord_id, player.id, False, now, ["reported"]
                )
        return MatchChange(await self._require(match_id), ["reported"])

    async def confirm(self, match_id: int, discord_id: int) -> MatchChange:
        """Someone on the other team (or an admin) agrees with the reported result."""
        player = await self.players.require(discord_id)
        admin = self.is_admin(discord_id)
        sides = {p.player_id: p.side for p in await self.repo.participants(match_id)}
        if not admin and player.id not in sides:
            raise PlayerError("Só quem jogou (ou um admin) pode confirmar o resultado.")
        now = self.clock()
        async with self.db.transaction():
            match = await self._require(match_id)
            if match.status != REPORTED:
                raise PlayerError(
                    "Este resultado já está confirmado."
                    if match.status == CONFIRMED
                    else "Não há resultado por confirmar."
                )
            if not admin:
                if match.contested:
                    raise PlayerError("O resultado foi contestado: só um admin pode decidir.")
                if sides.get(match.reported_by or 0) == sides[player.id]:
                    raise PlayerError("A confirmação tem de vir de alguém da outra equipa.")
            return await self._confirm(match_id, discord_id, player.id, False, now, ["confirmed"])

    async def contest(self, match_id: int, discord_id: int) -> MatchChange:
        """Block the automatic confirmation: an admin has to decide."""
        player = await self.players.require(discord_id)
        sides = {p.player_id for p in await self.repo.participants(match_id)}
        if not self.is_admin(discord_id) and player.id not in sides:
            raise PlayerError("Só quem jogou (ou um admin) pode contestar o resultado.")
        now = self.clock()
        async with self.db.transaction():
            match = await self._require(match_id)
            if match.status != REPORTED:
                raise PlayerError("Só se pode contestar um resultado por confirmar.")
            if not await self.repo.set_flag(match_id, "contested", [REPORTED]):
                raise PlayerError("Este resultado já foi contestado.")
            await self.repo.add_audit(match_id, "contest", discord_id, None, None, now)
        return MatchChange(await self._require(match_id), ["contested"])

    # --- admin (and creator) actions ----

    async def void(
        self, match_id: int, discord_id: int, reason: str, creator_id: int | None = None
    ) -> MatchChange:
        """Cancel the game: no rating effect. An admin can do it any time (undoing a confirmed
        result too); the lobby's creator only while no result has been reported."""
        player = await self.players.require(discord_id)
        admin = self.is_admin(discord_id)
        async with self.db.transaction():
            match = await self._require(match_id)
            if not admin and not (match.status == IN_PROGRESS and discord_id == creator_id):
                raise PlayerError("Só um admin pode anular um jogo com resultado.")
            return await self._void(match, discord_id, player.id, reason, self.clock())

    async def fix(self, match_id: int, discord_id: int, winner: str) -> MatchChange:
        """Admin: decide a pending or contested result, or correct a confirmed one."""
        if winner not in ("blue", "red"):
            raise PlayerError("O vencedor tem de ser azul ou vermelho.")
        await self.players.require(discord_id)
        if not self.is_admin(discord_id):
            raise PlayerError("Só um admin pode corrigir um resultado.")
        now = self.clock()
        match = await self._require(match_id)
        if match.status in (IN_PROGRESS, REPORTED):
            return await self.report(match_id, discord_id, winner)
        async with self.db.transaction():
            match = await self._require(match_id)
            if match.status != CONFIRMED:
                raise PlayerError("Este jogo foi anulado.")
            if match.winner == winner:
                raise PlayerError("Esse já é o resultado registado.")
            await self.repo.transition(match_id, [CONFIRMED], CONFIRMED, winner=winner)
            await self.repo.add_audit(match_id, "set_winner", discord_id, match.winner, winner, now)
            changes = await self._rebuild()
        return MatchChange(await self._require(match_id), ["fixed"], changes)

    # --- time-driven (called by the sweeper through the lobby service) ----

    async def process(self, now: datetime | None = None) -> list[MatchChange]:
        """Auto-confirm quiet results, nudge unreported games once, void abandoned ones."""
        now = now or self.clock()
        auto = timedelta(minutes=self.params.auto_confirm_minutes)
        remind = timedelta(minutes=self.params.reminder_minutes)
        expire = timedelta(hours=self.params.expire_hours)
        out: list[MatchChange] = []
        for match in await self.repo.list_by_status([REPORTED]):
            if match.contested or match.reported_at is None or now - match.reported_at < auto:
                continue
            async with self.db.transaction():
                fresh = await self._require(match.id)
                if fresh.status != REPORTED or fresh.contested:
                    continue
                out.append(await self._confirm(match.id, None, None, True, now, ["auto_confirmed"]))
        for match in await self.repo.list_by_status([IN_PROGRESS]):
            age = now - match.created_at
            if age >= expire:
                async with self.db.transaction():
                    fresh = await self._require(match.id)
                    if fresh.status == IN_PROGRESS:
                        out.append(await self._void(fresh, None, None, "sem resultado", now))
            elif age >= remind and await self.repo.set_flag(
                match.id, "reminder_sent", [IN_PROGRESS]
            ):
                out.append(MatchChange(await self._require(match.id), ["result_reminder"]))
        return out

    # --- internals (inside a transaction) ----

    async def _require(self, match_id: int) -> MatchRow:
        match = await self.repo.get(match_id)
        if match is None:
            raise PlayerError("Esse jogo não existe.")
        return match

    async def _confirm(
        self,
        match_id: int,
        discord_id: int | None,
        player_id: int | None,
        auto: bool,
        now: datetime,
        events: list[str],
    ) -> MatchChange:
        match = await self._require(match_id)
        if not await self.repo.transition(
            match_id,
            [REPORTED],
            CONFIRMED,
            confirmed_by=player_id,
            confirmed_at=now,
            auto_confirmed=auto,
        ):
            raise PlayerError("Este resultado já foi confirmado.")
        await self.repo.add_audit(
            match_id, "auto_confirm" if auto else "confirm", discord_id, None, match.winner, now
        )
        changes = await self._rebuild()
        await self.lobbies.transition(
            match.lobby_id, [LOBBY_IN_PROGRESS], COMPLETED, now, closed_at=now
        )
        return MatchChange(await self._require(match_id), [*events, "result_confirmed"], changes)

    async def _void(
        self,
        match: MatchRow,
        discord_id: int | None,
        player_id: int | None,
        reason: str,
        now: datetime,
    ) -> MatchChange:
        if not await self.repo.transition(
            match.id,
            [IN_PROGRESS, REPORTED, CONFIRMED],
            VOIDED,
            voided_by=player_id,
            voided_at=now,
            void_reason=reason,
        ):
            raise PlayerError("Este jogo já foi anulado.")
        await self.repo.add_audit(match.id, "void", discord_id, match.status, reason, now)
        changes = await self._rebuild() if match.status == CONFIRMED else []
        await self.lobbies.transition(
            match.lobby_id,
            [LOBBY_IN_PROGRESS, COMPLETED],
            CANCELLED,
            now,
            closed_at=now,
            cancel_reason=reason,
        )
        return MatchChange(await self._require(match.id), ["voided"], changes)

    async def _rebuild(self) -> list[StandingChange]:
        before = {r.player_id: r for r in await self.ratings.repo.list_stored()}
        await self.ratings.rebuild()
        return [
            StandingChange(r.player_id, before.get(r.player_id), r)
            for r in await self.ratings.repo.list_stored()
            if _visible(before.get(r.player_id)) != _visible(r)
        ]
