"""Riot account rules: linking, main account, rank refresh, ranked-history sync, role estimates.

Optional and isolated: without a client (no API key) linking and refreshing are refused with a
clear message, while everything already stored (and every manual alternative) keeps working.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from bot.features.players.service import PlayerError, PlayerService
from bot.features.ratings.service import RatingService
from bot.features.riot.client import (
    RiotAuthError,
    RiotClient,
    RiotError,
    RiotNotFound,
    RiotRateLimited,
    SoloRank,
)
from bot.features.riot.platforms import normalise_platform
from bot.features.riot.repository import (
    AccountRow,
    PuuidAlreadyLinked,
    RiotRepository,
    StoredGame,
)
from bot.features.riot.roles import (
    RoleHistoryParams,
    estimate_proficiency,
    role_experience,
)

log = logging.getLogger(__name__)

CONSENT_NOTICE = (
    "**Dados da Riot: o que acontece**\n"
    "• **O que é obtido:** o rank atual de Solo/Duo da conta principal e a lista de jogos ranked "
    "Solo/Duo de todas as contas ligadas (role, resultado, KDA, campeão, duração).\n"
    "• **Para que serve:** rating inicial in-house, estimativa de rank para o matchmaking e "
    "estimativa de experiência por role. Não são usados jogos custom da Riot.\n"
    "• **Visibilidade:** os nomes das contas ligadas e os teus últimos jogos ranked Solo/Duo "
    "(campeão, resultado, KDA, ...; /riot history) são visíveis a todos os membros deste "
    "servidor.\n"
    "• Isto é consentimento para o uso dos dados, **não** prova de que a conta é tua.\n"
    "• Podes remover tudo a qualquer momento com /riot unlink."
)
DISABLED_MESSAGE = "A integração com a Riot está desativada (falta a RIOT_API_KEY)."


def parse_riot_id(text: str) -> tuple[str, str]:
    """Split "Name#TAG". Only shape is checked here; Riot is the authority on validity."""
    name, sep, tag = text.strip().rpartition("#")
    name, tag = name.strip(), tag.strip()
    if not sep or not name or not tag or len(name) > 32 or len(tag) > 8 or not tag.isalnum():
        raise PlayerError("Riot ID inválido. Usa o formato Nome#TAG.")
    if not (name + tag).isprintable():
        raise PlayerError("Riot ID inválido.")
    return name, tag


def user_error(exc: RiotError) -> PlayerError:
    """Translate a Riot failure into a message that is safe and useful to show."""
    if isinstance(exc, RiotNotFound):
        return PlayerError("Não encontrei esse Riot ID.")
    if isinstance(exc, RiotAuthError):
        log.error("Riot rejected the API key (expired, revoked or not allowed)")
        return PlayerError("A chave da API da Riot está inválida ou expirou. Avisa um admin.")
    if isinstance(exc, RiotRateLimited):
        return PlayerError(
            f"A Riot está a limitar pedidos. Tenta de novo daqui a {int(exc.retry_after) + 1} s."
        )
    log.warning("Riot request failed: %s", exc)
    return PlayerError("Não consegui contactar a Riot. Tenta novamente mais tarde.")


@dataclass(frozen=True)
class RankOutcome:
    status: str  # ranked | unranked | pending (the lookup failed; nothing was recorded)
    label: str | None = None


@dataclass(frozen=True)
class LinkResult:
    account: AccountRow
    is_main: bool
    rank: RankOutcome | None  # only the main account is looked up


HISTORY_LIMIT = 10


@dataclass(frozen=True)
class HistoryView:
    nickname: str
    games: list[StoredGame]
    last_synced: datetime | None  # the oldest sync among the accounts shown; None = unfinished
    account: AccountRow | None  # set when one account was asked for
    multiple_accounts: bool  # games come from several accounts: show which one each is from


@dataclass(frozen=True)
class UnlinkResult:
    removed: AccountRow
    new_main: AccountRow | None  # set when the removed account was the main and others remain
    rank: RankOutcome | None  # the new main's rank lookup


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RiotService:
    def __init__(
        self,
        repo: RiotRepository,
        ratings: RatingService,
        players: PlayerService,
        client: RiotClient | None,
        default_platform: str = "euw1",
        clock: Callable[[], datetime] = _utcnow,
        history: RoleHistoryParams | None = None,
        refresh_cooldown: timedelta = timedelta(minutes=10),
    ) -> None:
        self.repo = repo
        self.ratings = ratings
        self.players = players
        self.client = client
        self.default_platform = normalise_platform(default_platform)
        self.clock = clock
        self.history = history or RoleHistoryParams()
        self.refresh_cooldown = refresh_cooldown
        self._last_refresh: dict[int, datetime] = {}
        self._syncing: set[int] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def _client(self) -> RiotClient:
        if self.client is None:
            raise PlayerError(DISABLED_MESSAGE)
        return self.client

    # --- accounts -------------------------------------------------------------------------

    async def link(
        self, discord_id: int, riot_id: str, platform: str | None, linked_by: int
    ) -> LinkResult:
        client = self._client()
        player = await self.players.require(discord_id)
        name, tag = parse_riot_id(riot_id)
        platform = normalise_platform(platform or self.default_platform)
        try:
            found = await client.account_by_riot_id(name, tag, platform)
            if not await client.has_summoner(found.puuid, platform):
                raise PlayerError(
                    f"Essa conta não tem perfil de League na plataforma {platform.upper()}. "
                    "Escolhe a plataforma certa."
                )
        except RiotError as exc:
            raise user_error(exc) from exc
        if await self.repo.get_by_puuid(found.puuid) is not None:
            raise PlayerError(f"A conta {found.game_name}#{found.tag_line} já está ligada.")
        is_main = not await self.repo.list_accounts(player.id)
        try:
            account = await self.repo.add_account(
                player.id,
                found.puuid,
                found.game_name,
                found.tag_line,
                platform,
                is_main,
                linked_by,
                self.clock(),
            )
        except PuuidAlreadyLinked:
            raise PlayerError(
                f"A conta {found.game_name}#{found.tag_line} já está ligada."
            ) from None
        rank = await self._try_refresh_rank(player.id, linked_by) if is_main else None
        self.spawn_history_sync(account)
        return LinkResult(account, is_main, rank)

    async def find_account(self, discord_id: int, riot_id: str) -> tuple[int, AccountRow]:
        player = await self.players.require(discord_id)
        wanted = riot_id.strip().casefold()
        for account in await self.repo.list_accounts(player.id):
            if account.riot_id.casefold() == wanted:
                return player.id, account
        raise PlayerError("Essa conta não está ligada a este jogador.")

    async def set_main(
        self, discord_id: int, riot_id: str, by: int
    ) -> tuple[AccountRow, RankOutcome]:
        player_id, account = await self.find_account(discord_id, riot_id)
        await self.repo.set_main(player_id, account.id)
        updated = await self.repo.get(account.id)
        assert updated is not None
        # The previous main's rank can no longer be the active R; look the new main up now.
        return updated, await self._try_refresh_rank(player_id, by)

    async def unlink(self, discord_id: int, riot_id: str, by: int | None = None) -> "UnlinkResult":
        """Delete the account and everything keyed by its puuid (role games, Riot rank signals).

        If it was the main, the earliest-linked remaining account (the first one `/riot list`
        shows) becomes the new main and its rank is looked up, so a player who still has
        accounts is never left without a main.
        """
        player_id, account = await self.find_account(discord_id, riot_id)
        await self.ratings.repo.delete_riot_signals(account.puuid)
        await self.repo.delete_account(account.id)
        if not account.is_main:
            return UnlinkResult(account, None, None)
        remaining = await self.repo.list_accounts(player_id)
        if not remaining:
            return UnlinkResult(account, None, None)
        await self.repo.set_main(player_id, remaining[0].id)
        new_main = await self.repo.get(remaining[0].id)
        assert new_main is not None
        return UnlinkResult(account, new_main, await self._try_refresh_rank(player_id, by))

    async def accounts(self, discord_id: int) -> list[tuple[AccountRow, int]]:
        player = await self.players.require(discord_id)
        return [
            (a, await self.repo.count_games(a.id)) for a in await self.repo.list_accounts(player.id)
        ]

    async def account_labels(self, player_id: int) -> list[str]:
        return [
            f"{a.riot_id}{' ★' if a.is_main else ''}"
            for a in await self.repo.list_accounts(player_id)
        ]

    async def champion_pool(self, player_id: int, limit: int) -> list[tuple[str, int]]:
        """The most played champions in the stored ranked games: (Data Dragon id, games)."""
        return await self.repo.champion_pool(player_id, limit)

    async def recent_history(
        self, discord_id: int, limit: int = HISTORY_LIMIT, riot_id: str | None = None
    ) -> "HistoryView":
        """The newest stored ranked Solo/Duo games of a player (all accounts, or one).

        Reads what the last sync stored, so it costs no Riot requests and works without a key.
        """
        player = await self.players.require(discord_id)
        accounts = await self.repo.list_accounts(player.id)
        if not accounts:
            raise PlayerError("Este jogador não tem contas ligadas.")
        chosen: AccountRow | None = None
        if riot_id is not None:
            wanted = riot_id.strip().casefold()
            chosen = next((a for a in accounts if a.riot_id.casefold() == wanted), None)
            if chosen is None:
                raise PlayerError("Essa conta não está ligada a este jogador.")
        relevant = [chosen] if chosen else accounts
        games = await self.repo.recent_games(player.id, limit, chosen.id if chosen else None)
        if not games:
            if any(a.history_synced_at is None for a in relevant):
                raise PlayerError(
                    "O histórico ainda está a sincronizar. Tenta daqui a uns minutos."
                )
            raise PlayerError("Não há jogos ranked Solo/Duo guardados nos últimos 12 meses.")
        synced = [a.history_synced_at for a in relevant]
        oldest_sync = None if any(s is None for s in synced) else min(s for s in synced if s)
        return HistoryView(
            nickname=player.nickname,
            games=games,
            last_synced=oldest_sync,
            account=chosen,
            multiple_accounts=len(accounts) > 1 and chosen is None,
        )

    async def main_puuid_of(self, player_id: int) -> str | None:
        return await self.repo.main_puuid(player_id)

    # --- rank ----------------------------------------------------------------------------

    async def refresh_rank(self, player_id: int, triggered_by: int | None) -> RankOutcome:
        """Look up the main account's Solo/Duo rank and record it. A failed lookup records
        nothing (so it can never supersede a good signal) and raises."""
        client = self._client()
        accounts = await self.repo.list_accounts(player_id)
        main = next((a for a in accounts if a.is_main), None)
        if main is None:
            raise PlayerError("Este jogador não tem conta principal.")
        try:
            rank: SoloRank | None = await client.solo_rank(main.puuid, main.platform)
        except RiotError as exc:
            raise user_error(exc) from exc
        if rank is None:
            await self.ratings.record_rank_signal(
                player_id, "riot", "unranked", None, None, triggered_by, account_key=main.puuid
            )
            return RankOutcome("unranked")
        await self.ratings.record_rank_signal(
            player_id,
            "riot",
            "ranked",
            rank.tier,
            rank.division,
            triggered_by,
            account_key=main.puuid,
        )
        resolved = (await self.ratings.resolve(player_id)).rank
        return RankOutcome("ranked", resolved.label() if resolved else rank.tier)

    async def _try_refresh_rank(self, player_id: int, triggered_by: int | None) -> RankOutcome:
        try:
            return await self.refresh_rank(player_id, triggered_by)
        except PlayerError as exc:
            log.info("Rank refresh pending for player %s: %s", player_id, exc)
            return RankOutcome("pending")

    async def refresh(self, discord_id: int, triggered_by: int, force: bool = False) -> RankOutcome:
        """/riot refresh: names + main rank now, ranked history in the background."""
        player = await self.players.require(discord_id)
        now = self.clock()
        last = self._last_refresh.get(player.id)
        if not force and last is not None and now - last < self.refresh_cooldown:
            minutes = int((self.refresh_cooldown - (now - last)).total_seconds() // 60) + 1
            raise PlayerError(f"Já atualizaste há pouco. Tenta de novo daqui a {minutes} min.")
        client = self._client()
        accounts = await self.repo.list_accounts(player.id)
        if not accounts:
            raise PlayerError("Este jogador não tem contas ligadas.")
        self._last_refresh[player.id] = now
        for account in accounts:
            try:
                current = await client.account_by_puuid(account.puuid, account.platform)
            except RiotError as exc:
                raise user_error(exc) from exc
            if (current.game_name, current.tag_line) != (account.game_name, account.tag_line):
                await self.repo.update_identity(account.id, current.game_name, current.tag_line)
        outcome = await self.refresh_rank(player.id, triggered_by)
        for account in await self.repo.list_accounts(player.id):
            self.spawn_history_sync(account)
        return outcome

    # --- ranked history -> role experience ---------------------------------------------------

    async def sync_history(self, account: AccountRow) -> int:
        """Fetch ranked Solo/Duo games not stored yet. Resumable: whatever was stored before an
        error stays, and the next run continues. Returns the number of new games."""
        client = self._client()
        if account.id in self._syncing:
            return 0
        self._syncing.add(account.id)
        try:
            now = self.clock()
            start = int((now - timedelta(days=self.history.window_days)).timestamp())
            try:
                ids = await client.ranked_match_ids(
                    account.puuid, account.platform, start, self.history.max_games_per_account
                )
                known = await self.repo.known_match_ids(account.id)
                stored = 0
                for match_id in ids:
                    if match_id in known:
                        continue
                    try:
                        game = await client.ranked_game(match_id, account.puuid, account.platform)
                    except RiotNotFound:
                        continue  # the match was removed
                    if game is not None:
                        await self.repo.add_game(account.id, game)
                        stored += 1
            except RiotError as exc:
                raise user_error(exc) from exc
            await self.repo.prune(
                account.id, now, self.history.window_days, self.history.max_games_per_account
            )
            await self.repo.mark_synced(account.id, self.clock())
            return stored
        finally:
            self._syncing.discard(account.id)

    def spawn_history_sync(self, account: AccountRow) -> None:
        if self.client is None:
            return

        async def run() -> None:
            try:
                await self.sync_history(account)
            except PlayerError as exc:
                log.warning("History sync stopped for account %s: %s", account.id, exc)
            except Exception:
                log.exception("History sync failed for account %s", account.id)

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def resume_unfinished_syncs(self) -> int:
        """At startup: continue every first-time history sync that a restart or an error cut
        short. Already-stored games are skipped, so this only fetches what is missing."""
        if self.client is None:
            return 0
        accounts = await self.repo.list_unsynced()
        for account in accounts:
            self.spawn_history_sync(account)
        return len(accounts)

    async def wait_for_background(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def role_estimates(self, player_id: int) -> Mapping[str, int | None]:
        games = await self.repo.observations(
            player_id,
            self.clock(),
            self.history.window_days,
            self.history.max_games_per_account,
        )
        return estimate_proficiency(
            role_experience(games, self.clock(), self.history), self.history
        )
