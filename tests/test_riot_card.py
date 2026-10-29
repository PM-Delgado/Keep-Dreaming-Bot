"""The post-game card: match and timeline parsing, the embed, the service and /riot card."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import discord
import pytest
import pytest_asyncio
from riot_fakes import NOW, FakeRiot, Stack, make_client
from test_riot_cog import ENV, FakeInteraction, member
from test_riot_leaderboard import Channel

from bot.config import load_settings
from bot.features.champions.service import ChampionService
from bot.features.riot.card import (
    CardData,
    build_card_embed,
    stat_fields,
    title_text,
)
from bot.features.riot.client import (
    Laning,
    RankedGame,
    RiotUnavailable,
    SoloRank,
    parse_laning,
    parse_ranked_game,
)
from bot.features.riot.cog import NO_FEED_CHANNEL, NOT_ADMIN_CARD, Riot

PLAYED = datetime(2026, 6, 1, 10, 13, tzinfo=UTC)

# --- match payloads ------------------------------------------------------------------------


def participant(pid: int, team: int, position: str, **extra: Any) -> dict[str, Any]:
    return {
        "participantId": pid,
        "teamId": team,
        "teamPosition": position,
        "individualPosition": position,
        "puuid": f"puuid-{pid}",
        "championName": f"Champ{pid}",
        "win": team == 100,
        "kills": 1,
        "deaths": 1,
        "assists": 1,
        "timePlayed": 1828,
        "totalDamageDealtToChampions": 1000,
        **extra,
    }


def match(participants: list[dict[str, Any]], queue: int = 420) -> dict[str, Any]:
    return {
        "metadata": {"matchId": "EUW1_8028021803"},
        "info": {
            "queueId": queue,
            "gameCreation": 1_780_000_000_000,
            "gameEndTimestamp": 1_780_001_828_000,
            "participants": participants,
        },
    }


def ten_players(me: dict[str, Any]) -> list[dict[str, Any]]:
    """Blue 1-5 (with `me` as the mid, id 3), red 6-10."""
    others = [
        participant(1, 100, "TOP", kills=3, totalDamageDealtToChampions=10000),
        participant(2, 100, "JUNGLE", kills=5, totalDamageDealtToChampions=20000),
        participant(4, 100, "BOTTOM", kills=2, totalDamageDealtToChampions=15000),
        participant(5, 100, "UTILITY", kills=1, totalDamageDealtToChampions=5000),
        participant(6, 200, "TOP"),
        participant(7, 200, "JUNGLE"),
        participant(8, 200, "MIDDLE", championName="Zed"),
        participant(9, 200, "BOTTOM"),
        participant(10, 200, "UTILITY"),
    ]
    return sorted([me, *others], key=lambda p: p["participantId"])


MINE = {
    "participantId": 3,
    "teamId": 100,
    "teamPosition": "MIDDLE",
    "individualPosition": "MIDDLE",
    "puuid": "me",
    "championName": "Yone",
    "win": True,
    "kills": 8,
    "deaths": 3,
    "assists": 11,
    "timePlayed": 1828,
    "totalDamageDealtToChampions": 24100,
    "allInPings": 2,
    "basicPings": 3,
    "retreatPings": 4,
    "doubleKills": 2,
    "tripleKills": 1,
    "quadraKills": 1,
    "pentaKills": 0,
}


def test_the_card_stats_are_read_from_the_match():
    game = parse_ranked_game(match(ten_players(dict(MINE))), "me")
    assert game is not None
    assert game.pings == 9  # every *Pings counter added up
    assert game.team_kills == 8 + 3 + 5 + 2 + 1  # my team only
    assert game.team_damage == 24100 + 10000 + 20000 + 15000 + 5000
    assert (game.double_kills, game.triple_kills, game.quadra_kills, game.penta_kills) == (
        2,
        1,
        1,
        0,
    )
    assert game.participant_id == 3
    assert game.opponent_participant_id == 8  # same position, other team
    assert game.opponent_champion == "Zed"


def test_a_ping_flag_is_not_counted_as_a_ping():
    mine = {**MINE, "someFlagPings": True}
    game = parse_ranked_game(match(ten_players(mine)), "me")
    assert game is not None and game.pings == 9


def test_an_old_style_payload_still_parses_with_the_card_stats_empty():
    bare = {
        "puuid": "me",
        "teamPosition": "MIDDLE",
        "win": True,
        "kills": 1,
        "deaths": 1,
        "assists": 1,
    }
    game = parse_ranked_game(match([bare]), "me")
    assert game is not None
    assert game.pings is None and game.team_kills is None and game.team_damage is None
    assert game.participant_id is None and game.opponent_participant_id is None
    assert game.double_kills is None and game.opponent_champion is None


def test_no_opponent_when_the_position_is_unknown():
    mine = {**MINE, "teamPosition": ""}
    game = parse_ranked_game(match(ten_players(mine)), "me")
    assert game is not None and game.opponent_participant_id is None


# --- the 15 minute numbers -----------------------------------------------------------------


def frame(
    ms: int, me: tuple[int, int, int, int], them: tuple[int, int, int, int]
) -> dict[str, Any]:
    """(minions, jungle, gold, xp) for participants 3 and 8."""

    def stats(pid: int, v: tuple[int, int, int, int]) -> dict[str, Any]:
        return {
            "participantId": pid,
            "minionsKilled": v[0],
            "jungleMinionsKilled": v[1],
            "totalGold": v[2],
            "xp": v[3],
            "level": 9,
        }

    return {
        "timestamp": ms,
        "participantFrames": {"3": stats(3, me), "8": stats(8, them)},
        "events": [],
    }


def level_two(participant_id: int, ms: int) -> dict[str, Any]:
    return {"type": "LEVEL_UP", "participantId": participant_id, "level": 2, "timestamp": ms}


def timeline(me_level2: int | None = 100_000, them_level2: int | None = 103_000) -> dict[str, Any]:
    frames = [
        frame(840_000, (1, 1, 1, 1), (2, 2, 2, 2)),  # 14:00: not the one
        frame(900_421, (90, 10, 5000, 6000), (101, 0, 4854, 5620)),  # 15:00
        frame(960_000, (500, 0, 9999, 9999), (1, 0, 1, 1)),  # 16:00: not the one
    ]
    frames[0]["events"] = [
        *([level_two(3, me_level2)] if me_level2 is not None else []),
        *([level_two(8, them_level2)] if them_level2 is not None else []),
        {"type": "LEVEL_UP", "participantId": 3, "level": 3, "timestamp": 50_000},  # not level 2
        {"type": "ITEM_PURCHASED", "participantId": 3, "timestamp": 1},
    ]
    return {"info": {"frameInterval": 60000, "frames": frames}}


def test_the_difference_is_taken_at_the_first_frame_from_15_minutes_on():
    laning = parse_laning(timeline(), me=3, opponent=8)
    # CS 100 vs 101, gold 5000 vs 4854, xp 6000 vs 5620: the DPM example.
    assert laning == Laning(cs_diff=-1, gold_diff=146, xp_diff=380, first_lvl2=True)


def test_who_reached_level_two_first():
    assert parse_laning(timeline(100_000, 103_000), 3, 8) == Laning(-1, 146, 380, True)
    assert parse_laning(timeline(110_000, 103_000), 3, 8) == Laning(-1, 146, 380, False)
    assert parse_laning(timeline(100_000, 100_000), 3, 8) == Laning(-1, 146, 380, None)  # a tie
    assert parse_laning(timeline(None, 103_000), 3, 8) == Laning(-1, 146, 380, None)


def test_a_game_that_ended_before_15_minutes_has_no_laning_numbers():
    short = {
        "info": {
            "frames": [
                frame(0, (0, 0, 500, 0), (0, 0, 500, 0)),
                frame(840_000, (1, 1, 1, 1), (2, 2, 2, 2)),
            ]
        }
    }
    assert parse_laning(short, 3, 8) is None


def test_a_timeline_in_an_unexpected_shape_is_an_error_not_a_guess():
    for broken in ({}, {"info": {}}, {"info": {"frames": [{"timestamp": 900_000}]}}):
        with pytest.raises(RiotUnavailable):
            parse_laning(broken, 3, 8)
    wrong_ids = timeline()
    with pytest.raises(RiotUnavailable):
        parse_laning(wrong_ids, 3, 99)  # nobody has that id in the frame


async def test_the_client_asks_for_the_timeline_only_when_there_is_an_opponent():
    world = FakeRiot()
    world.timelines["EUW1_1"] = timeline()
    client = make_client(world)
    game = parse_ranked_game(match(ten_players(dict(MINE))), "me")
    assert game is not None
    assert await client.laning(game, "EUW1_1", "euw1") == Laning(-1, 146, 380, True)
    assert world.calls == ["/lol/match/v5/matches/EUW1_1/timeline"]
    world.calls.clear()
    alone = parse_ranked_game(match([{**MINE, "teamPosition": ""}]), "me")
    assert alone is not None
    assert await client.laning(alone, "EUW1_1", "euw1") is None
    assert world.calls == []  # nothing to compare with: no request


# --- the embed -----------------------------------------------------------------------------


def game(**changes: Any) -> RankedGame:
    base: dict[str, Any] = {
        "match_id": "EUW1_8028021803",
        "played_at": PLAYED,
        "team_position": "MIDDLE",
        "individual_position": "MIDDLE",
        "win": True,
        "kills": 8,
        "deaths": 3,
        "assists": 11,
        "duration_s": 1828,
        "early_surrender": False,
        "champion": "Yone",
        "cs": 212,
        "damage": 24100,
        "gold": 14200,
        "vision_score": 38,
        "pings": 9,
        "team_kills": 25,
        "team_damage": 75000,
        "double_kills": 2,
        "quadra_kills": 1,
        "penta_kills": 0,
    }
    return RankedGame(**{**base, **changes})


def card(**changes: Any) -> CardData:
    base: dict[str, Any] = {
        "nickname": "Exduardo",
        "riot_id": "Exduardo#EUW",
        "game": game(),
        "champion_name": "Yone",
        "laning": Laning(-1, 146, 380, False),
        "opponent_name": "Zed",
        "rank": SoloRank("MASTER", None, 790),
        "lp_delta": 27,
        "champion_icon_url": "https://ddragon.invalid/yone.png",
        "avatar_url": "https://cdn.invalid/avatar.png",
    }
    return CardData(**{**base, **changes})


def desc(embed: discord.Embed) -> str:
    return embed.description or ""


def last_value(embed: discord.Embed) -> str:
    return embed.fields[-1].value or ""


def fields(embed: discord.Embed) -> list[tuple[str | None, str | None]]:
    return [(f.name, f.value) for f in embed.fields]


def test_the_stats_have_the_agreed_values():
    assert stat_fields(game()) == [
        ("KDA", "8/3/11 (6.3)"),
        ("Duração", "30:28"),
        ("Participação", "76%"),
        ("CS", "212 (7.0/min)"),
        ("Dano", "24.1k (791/min)\n32% da equipa"),
        ("Visão", "38 (1.25/min)"),
        ("Ouro", "14.2k"),
        ("Pings", "9"),
    ]


def test_the_card_has_the_agreed_layout():
    embed = build_card_embed(card(), {"MID": "<:role_mid:1>"}, {"MASTER": "<:tier_master:2>"})
    assert embed.author.name == "Exduardo (Exduardo#EUW)"
    assert embed.author.icon_url == "https://cdn.invalid/avatar.png"
    assert embed.title == "Ganhou 27 LP (Solo/Duo)"
    assert embed.description == "\n".join(
        ["<:role_mid:1> **Yone**", "<:tier_master:2> MASTER · 790 LP", "🔥 Quadra kill"]
    )
    assert embed.thumbnail.url == "https://ddragon.invalid/yone.png"
    assert embed.color == discord.Color.green()
    assert embed.timestamp == PLAYED
    assert embed.footer.text == "Solo/Duo · 8028021803"  # no region prefix
    assert [f.name for f in embed.fields] == [
        "KDA", "Duração", "Participação", "CS", "Dano", "Visão", "Ouro", "Pings",
        "Stats @ 15 (vs Zed)",
    ]  # fmt: skip
    assert [f.inline for f in embed.fields] == [True] * 8 + [False]
    assert embed.fields[-1].value == (
        "**CS** -1 · **Ouro** +146 · **XP** +380 · **Primeiro nível 2:** Não"
    )


def test_the_title_follows_the_lp_change_and_the_side_bar_the_result():
    assert build_card_embed(card(lp_delta=-19, game=game(win=False))).title == (
        "Perdeu 19 LP (Solo/Duo)"
    )
    assert build_card_embed(card(lp_delta=-19, game=game(win=False))).color == discord.Color.red()
    # Unknown or zero LP change: the number is left out, never guessed.
    assert title_text(game(), None) == "Ganhou (Solo/Duo)"
    assert title_text(game(), 0) == "Ganhou (Solo/Duo)"
    assert title_text(game(win=False), None) == "Perdeu (Solo/Duo)"


def test_a_remake_is_grey_and_has_no_lp():
    for remake in (game(early_surrender=True), game(duration_s=200)):
        embed = build_card_embed(card(game=remake, lp_delta=12))
        assert embed.title == "Remake (Solo/Duo)"
        assert embed.color == discord.Color.light_grey()


def test_jungle_and_support_get_gold_and_xp_but_no_cs_difference():
    for position in ("JUNGLE", "UTILITY"):
        embed = build_card_embed(card(game=game(team_position=position)))
        assert last_value(embed).startswith("**Ouro** +146")
        assert "**CS**" not in last_value(embed)
    top = build_card_embed(card(game=game(team_position="TOP")))
    assert last_value(top).startswith("**CS** -1")


def test_an_unknown_first_level_two_is_left_out_of_the_block():
    embed = build_card_embed(card(laning=Laning(-1, 146, 380, None)))
    assert "Primeiro nível 2" not in last_value(embed)


def test_the_laning_block_is_left_out_without_numbers_or_names_the_opponent_only_when_known():
    assert "Stats @ 15" not in [f.name for f in build_card_embed(card(laning=None)).fields]
    unnamed = build_card_embed(card(opponent_name=None))
    assert unnamed.fields[-1].name == "Stats @ 15"


def test_what_riot_did_not_send_is_left_out_not_shown_as_zero():
    bare = game(
        cs=None, damage=None, gold=None, vision_score=None, pings=None, team_kills=None,
        team_damage=None, double_kills=None, quadra_kills=None, penta_kills=None,
    )  # fmt: skip
    embed = build_card_embed(card(game=bare, laning=None, rank=None, lp_delta=None))
    assert [f.name for f in embed.fields] == ["KDA", "Duração"]
    assert embed.description == "Mid **Yone**"  # no rank line, no multikill line
    assert embed.title == "Ganhou (Solo/Duo)"


def test_only_the_best_multikill_is_shown_and_a_count_above_one_is_stated():
    assert "Penta kill" in desc(build_card_embed(card(game=game(penta_kills=1))))
    assert "🔥 2× Quadra kill" in desc(build_card_embed(card(game=game(quadra_kills=2))))
    only_double = desc(build_card_embed(card(game=game(quadra_kills=0, double_kills=1))))
    assert "🔥 Double kill" in only_double and "Quadra" not in only_double
    none = desc(build_card_embed(card(game=game(double_kills=0, quadra_kills=0))))
    assert "🔥" not in none


def test_the_rank_line_falls_back_to_the_colour_dot_and_names_are_escaped():
    embed = build_card_embed(card(champion_name="*Wukong*"))
    assert "🟣 MASTER · 790 LP" in desc(embed)
    assert "\\*Wukong\\*" in desc(embed)


# --- the service ---------------------------------------------------------------------------


@pytest.fixture
def world() -> FakeRiot:
    w = FakeRiot()
    w.add_account("Alpha", "EUW", "puuid-alpha")
    w.add_account("Beta", "EUW", "puuid-beta")
    w.set_rank("puuid-alpha", "MASTER", "I", 790)
    return w


@pytest_asyncio.fixture
async def stack(db, world) -> Stack:
    s = Stack(db, world)
    await s.register(1, "Ana")
    await s.register(2, "Rui")
    return s


async def linked(stack: Stack, world: FakeRiot) -> None:
    """Ana has Alpha (main) with a stored ranked game on a full ten-player match."""
    world.add_game("puuid-alpha", "EUW1_1", "MIDDLE", NOW, champion="Yone", kda=(8, 3, 11))
    await stack.riot.link(1, "Alpha#EUW", None, 1)
    await stack.riot.wait_for_background()
    mine = {**MINE, "puuid": "puuid-alpha"}
    world.matches["EUW1_1"] = match(ten_players(mine))
    world.matches["EUW1_1"]["metadata"]["matchId"] = "EUW1_1"
    world.timelines["EUW1_1"] = timeline()


async def test_the_card_source_has_the_game_the_laning_numbers_and_the_current_rank(stack, world):
    await linked(stack, world)
    world.calls.clear()
    source = await stack.riot.game_card(1)
    assert source.nickname == "Ana" and source.riot_id == "Alpha#EUW"
    assert source.game.champion == "Yone" and source.game.pings == 9
    assert source.laning == Laning(-1, 146, 380, True)
    assert source.rank == SoloRank("MASTER", None, 790)  # apex tiers have no division
    assert len(world.calls) == 3  # the match, its timeline, the rank


async def test_a_missing_timeline_or_rank_still_gives_the_card(stack, world):
    await linked(stack, world)
    world.forced["/lol/match/v5/matches/EUW1_1/timeline"] = 500
    world.forced["/lol/league"] = 500
    source = await stack.riot.game_card(1)
    assert source.game.champion == "Yone"
    assert source.laning is None and source.rank is None


async def test_a_game_riot_no_longer_has_is_a_clear_error(stack, world):
    await linked(stack, world)
    del world.matches["EUW1_1"]
    with pytest.raises(Exception, match="Riot"):
        await stack.riot.game_card(1)


async def test_card_source_refusals(stack, world):
    from bot.features.players.service import PlayerError

    with pytest.raises(PlayerError, match="não tem contas"):
        await stack.riot.game_card(2)
    await linked(stack, world)
    with pytest.raises(PlayerError, match="não está ligada"):
        await stack.riot.game_card(1, "Nobody#EUW")
    world.add_account("Gamma", "EUW", "puuid-gamma")
    await stack.riot.link(1, "Beta#EUW", None, 1)
    await stack.riot.wait_for_background()
    with pytest.raises(PlayerError, match="Não há jogos"):
        await stack.riot.game_card(1, "Beta#EUW")  # linked, but no stored game


async def test_the_main_can_be_chosen_by_riot_id_case_insensitively(stack, world):
    await linked(stack, world)
    assert (await stack.riot.game_card(1, "alpha#euw")).riot_id == "Alpha#EUW"


# --- /riot card ----------------------------------------------------------------------------


class FakeChampions:
    async def names(self, ids):
        return {"Yone": "Yone", "Zed": "Zed"}

    async def icon_link(self, champion_id):
        return f"https://ddragon.invalid/{champion_id}.png"


def make_cog(stack: Stack, channel: Channel | None = None, with_channel: bool = True) -> Riot:
    env = {**ENV, "RIOT_FEED_CHANNEL_ID": "555"} if with_channel else dict(ENV)
    bot = SimpleNamespace(settings=load_settings(env), get_channel=lambda _id: channel)
    champions: Any = FakeChampions()
    return Riot(bot, stack.riot, None, champions)  # type: ignore[arg-type]


async def test_only_admins_can_preview_cards(stack, world):
    await linked(stack, world)
    cog = make_cog(stack)
    ix = FakeInteraction(5)
    await cog.card.callback(cog, ix, member(1), None, False)  # type: ignore[arg-type]
    assert ix.sent[0]["content"] == NOT_ADMIN_CARD and ix.sent[0]["ephemeral"] is True
    assert world.calls.count("/lol/match/v5/matches/EUW1_1") == 1  # only the setup's sync


async def test_an_admin_sees_the_card_privately_and_nothing_is_posted(stack, world):
    await linked(stack, world)
    channel = Channel()
    cog = make_cog(stack, channel)
    ix = FakeInteraction(99)
    await cog.card.callback(cog, ix, member(1), None, False)  # type: ignore[arg-type]
    assert ix.deferred
    reply = ix.sent[0]
    assert reply["ephemeral"] is True
    embed = reply["embed"]
    assert embed.author.name == "Ana (Alpha#EUW)"
    assert embed.fields[-1].name == "Stats @ 15 (vs Zed)"
    assert embed.thumbnail.url == "https://ddragon.invalid/Yone.png"
    assert channel.sent == []


async def test_post_sends_the_card_to_the_feed_channel(stack, world):
    await linked(stack, world)
    channel = Channel()
    cog = make_cog(stack, channel)
    ix = FakeInteraction(99)
    await cog.card.callback(cog, ix, member(1), "Alpha#EUW", True)  # type: ignore[arg-type]
    assert "publicado" in ix.sent[0]["content"]
    assert channel.sent[0]["embed"].title.endswith("(Solo/Duo)")


async def test_post_without_a_feed_channel_says_so(stack, world):
    await linked(stack, world)
    cog = make_cog(stack, None, with_channel=False)
    ix = FakeInteraction(99)
    await cog.card.callback(cog, ix, member(1), None, True)  # type: ignore[arg-type]
    assert ix.sent[0]["content"] == NO_FEED_CHANNEL


async def test_a_player_without_games_gets_a_private_explanation(stack, world):
    cog = make_cog(stack)
    ix = FakeInteraction(99)
    await cog.card.callback(cog, ix, member(2), None, False)  # type: ignore[arg-type]
    assert "não tem contas" in ix.sent[0]["content"] and ix.sent[0]["ephemeral"] is True


async def test_the_champion_service_gives_an_icon_link_once_the_list_is_cached(db):
    from bot.features.champions.repository import ChampionRepository

    service = ChampionService(ChampionRepository(db), lambda url: None, lambda url: None)  # type: ignore[arg-type]
    assert await service.icon_link("Yone") is None  # nothing cached yet
    await service.repo.seed_if_empty()
    link = await service.icon_link("Yone")
    assert link is not None and link.endswith("/img/champion/Yone.png")
