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
from bot.features.champions.service import ChampionService
from bot.features.lobby.repository import CANCELLED, COMPLETED, LobbyRepository, LobbyRow
from bot.features.lobby.repository import IN_PROGRESS as LOBBY_IN_PROGRESS
from bot.features.matches.repository import (
    CONFIRMED,
    IN_PROGRESS,
    PENDING,
    REPORTED,
    VALIDATED,
    VOIDED,
    MatchRepository,
    MatchRow,
    ParticipantRow,
)
from bot.features.players.proficiency import ROLE_NAMES_PT, ROLES
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


MAX_STAT = 99  # kills, deaths or assists in one game: above this is a typo


@dataclass(frozen=True)
class StatsResult:
    """One participant's stats after an entry, a correction or a validation."""

    match_id: int
    participant: ParticipantRow
    champion_name: str | None


def kda_text(kills: int | None, deaths: int | None, assists: int | None) -> str:
    return f"{kills}/{deaths}/{assists}"


def stats_text(p: ParticipantRow, champion_name: str | None) -> str:
    """ "Ahri 8/2/11" (plus the role actually played, when an admin recorded one)."""
    if not p.has_stats:
        return "sem stats"
    text = f"{champion_name or p.champion} {kda_text(p.kills, p.deaths, p.assists)}"
    return f"{text} ({ROLE_NAMES_PT[p.played_role]})" if p.played_role else text


def _check_kda(kills: int | None, deaths: int | None, assists: int | None) -> None:
    for value in (kills, deaths, assists):
        if value is not None and not 0 <= value <= MAX_STAT:
            raise PlayerError(f"Abates, mortes e assistências têm de estar entre 0 e {MAX_STAT}.")


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
        champions: ChampionService | None = None,
    ) -> None:
        self.repo = repo
        self.lobbies = lobbies
        self.players = players
        self.ratings = ratings
        self.is_admin = is_admin
        self.db = db
        self.params = params or MatchParams()
        self.clock = clock
        self.champions = champions

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

    # --- champion and KDA (M3b) ----
    # Only a *confirmed* game takes stats. A player's entry waits for an admin (`pending`); what an
    # admin enters is validated at once. Editing validated stats sends them back to pending.

    async def enter_stats(
        self,
        discord_id: int,
        champion: str,
        kills: int,
        deaths: int,
        assists: int,
        match_id: int | None = None,
    ) -> StatsResult:
        """A player records (or corrects) their own champion and KDA.

        Without `match_id` it is their latest confirmed game whose stats are not validated yet.
        """
        player = await self.players.require(discord_id)
        found = await self._champion(champion)
        _check_kda(kills, deaths, assists)
        if match_id is None:
            match_id = await self.repo.latest_match_needing_stats(player.id)
            if match_id is None:
                raise PlayerError(
                    "Não tens nenhum jogo confirmado por preencher. Indica o número do jogo."
                )
        now = self.clock()
        async with self.db.transaction():
            await self._require_confirmed(match_id)
            mine = await self.repo.participant(match_id, player.id)
            if mine is None:
                raise PlayerError("Não jogaste nesse jogo.")
            if (mine.champion, mine.kills, mine.deaths, mine.assists) == (
                found.id,
                kills,
                deaths,
                assists,
            ):
                raise PlayerError(
                    "Esses valores já estão validados."
                    if mine.stats_status == VALIDATED
                    else "Já registaste esses valores; falta um admin validar."
                )
            old = stats_text(mine, await self._champion_name(mine.champion))
            await self.repo.write_stats(
                match_id, player.id, found.id, kills, deaths, assists, PENDING, player.id, None
            )
            after = await self._participant(match_id, player.id)
            await self.repo.add_audit(
                match_id,
                "stats",
                discord_id,
                old if mine.has_stats else None,
                stats_text(after, found.name),
                now,
                player.id,
            )
        return StatsResult(match_id, after, found.name)

    async def set_stats(
        self,
        discord_id: int,
        match_id: int,
        target_discord_id: int,
        champion: str | None = None,
        kills: int | None = None,
        deaths: int | None = None,
        assists: int | None = None,
        played_role: str | None = None,
    ) -> StatsResult:
        """Admin: set or correct someone's stats (validated at once) and/or the role they played.

        Fields left out keep their value; a player with no stats yet needs all four.
        """
        admin = await self.players.require(discord_id)
        if not self.is_admin(discord_id):
            raise PlayerError("Só um admin pode definir stats.")
        target = await self.players.repo.get_by_discord(target_discord_id)
        if target is None:
            raise PlayerError("Esse jogador não está registado.")
        if played_role is not None and played_role not in ROLES:
            raise PlayerError("Role inválida.")
        _check_kda(kills, deaths, assists)
        given = (champion, kills, deaths, assists)
        if all(v is None for v in given) and played_role is None:
            raise PlayerError("Diz o que corrigir: campeão, abates, mortes, assistências ou role.")
        found = await self._champion(champion) if champion is not None else None
        now = self.clock()
        async with self.db.transaction():
            await self._require_confirmed(match_id)
            before = await self.repo.participant(match_id, target.id)
            if before is None:
                raise PlayerError("Esse jogador não jogou esse jogo.")
            old = stats_text(before, await self._champion_name(before.champion))
            if any(v is not None for v in given):
                merged = (
                    found.id if found else before.champion,
                    before.kills if kills is None else kills,
                    before.deaths if deaths is None else deaths,
                    before.assists if assists is None else assists,
                )
                if any(v is None for v in merged):
                    raise PlayerError(
                        "Este jogador ainda não tem stats: indica campeão, abates, mortes e "
                        "assistências."
                    )
                champion_id, k, d, a = merged
                assert champion_id is not None and k is not None  # for the type checker
                assert d is not None and a is not None
                await self.repo.write_stats(
                    match_id, target.id, champion_id, k, d, a, VALIDATED, admin.id, admin.id
                )
            if played_role is not None:
                await self.repo.set_played_role(match_id, target.id, played_role)
            after = await self._participant(match_id, target.id)
            name = await self._champion_name(after.champion)
            await self.repo.add_audit(
                match_id,
                "stats_set",
                discord_id,
                old if before.has_stats or before.played_role else None,
                stats_text(after, name),
                now,
                target.id,
            )
        return StatsResult(match_id, after, name)

    async def validate_stats(
        self, discord_id: int, match_id: int, target_discord_id: int | None = None
    ) -> list[StatsResult]:
        """Admin: approve the pending stats of one player, or of everyone in the game."""
        admin = await self.players.require(discord_id)
        if not self.is_admin(discord_id):
            raise PlayerError("Só um admin pode validar stats.")
        target_id = None
        if target_discord_id is not None:
            target = await self.players.repo.get_by_discord(target_discord_id)
            if target is None:
                raise PlayerError("Esse jogador não está registado.")
            target_id = target.id
        now = self.clock()
        results = []
        async with self.db.transaction():
            await self._require_confirmed(match_id)
            for p in await self.repo.participants(match_id):
                if p.stats_status != PENDING or (
                    target_id is not None and p.player_id != target_id
                ):
                    continue
                if await self.repo.validate_stats(match_id, p.player_id, admin.id):
                    name = await self._champion_name(p.champion)
                    await self.repo.add_audit(
                        match_id,
                        "stats_validate",
                        discord_id,
                        None,
                        stats_text(p, name),
                        now,
                        p.player_id,
                    )
                    results.append(
                        StatsResult(match_id, await self._participant(match_id, p.player_id), name)
                    )
        if not results:
            raise PlayerError("Não há stats por validar nesse jogo.")
        return results

    async def pending_stats(
        self, guild_id: int, discord_id: int, limit: int = 25
    ) -> list[ParticipantRow]:
        """Admin: stats waiting for validation, oldest game first."""
        if not self.is_admin(discord_id):
            raise PlayerError("Só um admin pode ver as stats por validar.")
        return await self.repo.pending_stats(guild_id, limit)

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

    async def _require_confirmed(self, match_id: int) -> MatchRow:
        match = await self._require(match_id)
        if match.status != CONFIRMED:
            raise PlayerError(
                "Este jogo foi anulado."
                if match.status == VOIDED
                else "Só se registam stats depois de o resultado estar confirmado."
            )
        return match

    async def _participant(self, match_id: int, player_id: int) -> ParticipantRow:
        row = await self.repo.participant(match_id, player_id)
        assert row is not None
        return row

    async def _champion(self, text: str):
        if self.champions is None:
            raise PlayerError("A lista de campeões não está disponível.")
        found = await self.champions.find(text)
        if found is None:
            raise PlayerError(f"Não conheço o campeão «{text[:40]}». Escolhe um da lista.")
        return found

    async def _champion_name(self, champion_id: str | None) -> str | None:
        if champion_id is None or self.champions is None:
            return champion_id
        return (await self.champions.names([champion_id])).get(champion_id, champion_id)

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
