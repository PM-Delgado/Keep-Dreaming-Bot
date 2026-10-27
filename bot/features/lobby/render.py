"""Lobby messages (embeds and text). All wording and layout live here so the look can be
reworked later without touching lobby rules. Never shows ratings, only the coarse balance band."""

from collections.abc import Mapping, Sequence
from datetime import timedelta

import discord

from bot.features.balance.model import BAND_CLEAR, BAND_EVEN, BAND_LOPSIDED, BAND_SLIGHT
from bot.features.icons.roles import role_prefix
from bot.features.lobby.repository import (
    CANCELLED,
    COMPLETED,
    IN_PROGRESS,
    OPEN,
    READY,
    READY_CHECK,
    TEAMS_PROPOSED,
)
from bot.features.lobby.service import EntryView, LobbySnapshot, ProposalView, SlotView
from bot.features.matches.repository import PENDING, MatchRow, ParticipantRow
from bot.features.matches.service import StandingChange, kda_text
from bot.features.ratings.ladder import LadderConfig, step_rank

STATUS_TEXT = {
    OPEN: "Aberta",
    READY_CHECK: "Ready-check",
    TEAMS_PROPOSED: "Equipas propostas",
    IN_PROGRESS: "Em jogo",
    CANCELLED: "Cancelada",
    COMPLETED: "Terminada",
}
STATUS_COLOR = {
    OPEN: discord.Color.blurple(),
    READY_CHECK: discord.Color.orange(),
    TEAMS_PROPOSED: discord.Color.gold(),
    IN_PROGRESS: discord.Color.green(),
    CANCELLED: discord.Color.dark_grey(),
    COMPLETED: discord.Color.dark_grey(),
}
SIDE_NAME = {"blue": "Azul", "red": "Vermelho"}
SIDE_EMOJI = {"blue": "🔵", "red": "🔴"}
ROSTER_SIZE = 10


def _entry_line(index: int, entry: EntryView, show_ready: bool) -> str:
    mark = ""
    if show_ready:
        mark = "✅ " if entry.status == READY else "⏳ "
    name = discord.utils.escape_markdown(entry.nickname)
    tag = " 🤖" if entry.is_mock else ""
    return f"`{index:>2}.` {mark}**{name}**{tag}"


def roster_text(snapshot: LobbySnapshot) -> str:
    show_ready = snapshot.lobby.status in (READY_CHECK, TEAMS_PROPOSED)
    lines = [_entry_line(i, e, show_ready) for i, e in enumerate(snapshot.roster, start=1)]
    for i in range(len(snapshot.roster) + 1, ROSTER_SIZE + 1):
        lines.append(f"`{i:>2}.` —")
    return "\n".join(lines)


def reserves_text(snapshot: LobbySnapshot) -> str:
    return "\n".join(_entry_line(i, e, False) for i, e in enumerate(snapshot.reserves, start=1))


def band_text(proposal: ProposalView) -> str:
    side = SIDE_NAME.get(proposal.favoured or "", "")
    return {
        BAND_EVEN: "Equilibrado",
        BAND_SLIGHT: f"Ligeira vantagem {side}",
        BAND_CLEAR: f"Vantagem clara {side}",
        BAND_LOPSIDED: f"⚠ Muito desequilibrado ({side} favorecido)",
    }[proposal.band]


def fit_note(slot: SlotView) -> str:
    """Shown only when someone is not on their best role, so the line-up is easy to audit."""
    if slot.best is None or slot.fit == slot.best:
        return ""
    return f" · {slot.fit if slot.fit is not None else '?'}/5"


def team_text(slots: list[SlotView], roles: Mapping[str, str] | None = None) -> str:
    """One line per slot: the role icon (or the role's name) and the player."""
    return "\n".join(
        f"{role_prefix(s.role, roles)}{discord.utils.escape_markdown(s.nickname)}{fit_note(s)}"
        for s in slots
    )


def build_lobby_embed(
    snapshot: LobbySnapshot,
    auto_confirm_minutes: int = 30,
    roles: Mapping[str, str] | None = None,
) -> discord.Embed:
    lobby = snapshot.lobby
    status = lobby.status
    scheduled_open = lobby.kind == "scheduled" and status == OPEN
    embed = discord.Embed(
        title=f"In-house #{lobby.id} · {'Agendada' if scheduled_open else STATUS_TEXT[status]}",
        color=STATUS_COLOR[status],
    )
    if lobby.note:
        embed.add_field(name="Nota", value=discord.utils.escape_markdown(lobby.note), inline=False)

    if status in (OPEN, READY_CHECK):
        count = len(snapshot.roster)
        embed.add_field(name=f"Jogadores ({count}/{ROSTER_SIZE})", value=roster_text(snapshot))
        if snapshot.reserves:
            embed.add_field(name="Reservas", value=reserves_text(snapshot), inline=False)

    if scheduled_open and lobby.scheduled_for is not None:
        stamp = int(lobby.scheduled_for.timestamp())
        embed.description = (
            f"Começa <t:{stamp}:F> (<t:{stamp}:R>). Carrega em **Entrar** para guardares lugar."
        )
        if lobby.reminder_stage >= 2:
            embed.description += (
                "\nA hora já passou: o ready-check começa assim que houver 10 jogadores."
            )
        else:
            embed.description += "\nÀ hora marcada, com 10 jogadores, começa o ready-check."
    elif status == OPEN:
        embed.description = "Carrega em **Entrar**. Com 10 jogadores começa o ready-check."
    elif status == READY_CHECK and lobby.ready_deadline is not None:
        stamp = int(lobby.ready_deadline.timestamp())
        embed.description = (
            f"Todos têm de carregar em **Pronto** <t:{stamp}:R>. Quem não confirmar sai e "
            "é substituído pelas reservas."
        )

    if status in (TEAMS_PROPOSED, IN_PROGRESS, COMPLETED):
        _add_teams(embed, snapshot, roles)
    if status in (IN_PROGRESS, COMPLETED):
        _describe_game(embed, snapshot, auto_confirm_minutes)

    if status == CANCELLED:
        embed.description = f"Lobby encerrada ({lobby.cancel_reason or 'cancelada'})."
    elif status == IN_PROGRESS:
        embed.set_footer(text="Só o resultado confirmado conta para o rank.")
    elif status == TEAMS_PROPOSED:
        embed.set_footer(text="Só o criador ou um admin confirma ou cancela. N/5 = nível na role.")
    return embed


def _add_teams(
    embed: discord.Embed, snapshot: LobbySnapshot, roles: Mapping[str, str] | None = None
) -> None:
    proposal = snapshot.proposal
    if proposal is None:
        embed.description = "Não consegui formar equipas. Um admin pode cancelar e recomeçar."
        return
    embed.add_field(name=f"{SIDE_EMOJI['blue']} Azul", value=team_text(proposal.blue, roles))
    embed.add_field(name=f"{SIDE_EMOJI['red']} Vermelho", value=team_text(proposal.red, roles))
    embed.add_field(name="Equilíbrio", value=band_text(proposal), inline=False)


def _describe_game(embed: discord.Embed, snapshot: LobbySnapshot, auto_minutes: int) -> None:
    """The line under the title of a started game: playing, waiting for a confirmation, or done."""
    match = snapshot.match
    if match is None:
        return
    winner = SIDE_NAME.get(match.winner or "", "?")
    if match.status == "confirmed":
        how = " (confirmado automaticamente)" if match.auto_confirmed else ""
        embed.description = f"🏆 **{winner} ganhou**{how}."
    elif match.status == "reported" and match.reported_at is not None:
        who = discord.utils.escape_markdown(snapshot.reporter or "?")
        if match.contested:
            embed.description = (
                f"**{who}** reportou que o **{winner}** ganhou, mas o resultado foi contestado. "
                "Um admin tem de decidir (botões da equipa vencedora)."
            )
        else:
            stamp = int((match.reported_at + timedelta(minutes=auto_minutes)).timestamp())
            embed.description = (
                f"**{who}** reportou: **{winner} ganhou**. Alguém da outra equipa confirma, ou "
                f"contesta se não é verdade; sem objeções confirma <t:{stamp}:R>."
            )
    elif match.status == "in_progress":
        embed.description = (
            "Jogo em curso. Quando acabar, alguém carrega no botão da equipa vencedora."
        )


def report_ping(snapshot: LobbySnapshot) -> tuple[str, list[EntryView]] | None:
    """Ask the team that did not report to confirm (real players only)."""
    match, proposal = snapshot.match, snapshot.proposal
    if match is None or proposal is None or match.winner is None:
        return None
    blue_ids = {s.player_id for s in proposal.blue}
    reporter_blue = match.reported_by in blue_ids
    other = {s.player_id for s in (proposal.red if reporter_blue else proposal.blue)}
    targets = [e for e in snapshot.roster if e.player_id in other and not e.is_mock]
    if not targets:
        return None
    mentions = " ".join(f"<@{e.discord_id}>" for e in targets)
    return (
        f"{mentions} — foi reportado que o **{SIDE_NAME[match.winner]} ganhou** a in-house "
        f"#{snapshot.lobby.id}. Confirmem ou contestem na mensagem da lobby.",
        targets,
    )


def result_reminder_ping(snapshot: LobbySnapshot) -> tuple[str, list[EntryView]] | None:
    targets = [e for e in snapshot.roster if not e.is_mock]
    if not targets:
        return None
    mentions = " ".join(f"<@{e.discord_id}>" for e in targets)
    return (
        f"{mentions} — a in-house #{snapshot.lobby.id} ainda não tem resultado. "
        "Reportem quem ganhou na mensagem da lobby.",
        targets,
    )


def _step_label(ladder: LadderConfig, tier_key: str | None, division: int | None) -> str:
    return ladder.label(tier_key or "", division)


def standing_lines(
    changes: Sequence[StandingChange], names: Mapping[int, str], ladder: LadderConfig
) -> list[str]:
    """What moved visibly after the ratings were rebuilt: promotions, demotions, placements.

    Never shows ratings, only tiers; players still in placement are grouped on one line.
    """
    lines: list[str] = []
    placing: list[str] = []
    for change in sorted(changes, key=lambda c: names.get(c.player_id, "")):
        name = discord.utils.escape_markdown(names.get(change.player_id, "?"))
        before, after = change.before, change.after
        if not after.placed:
            placing.append(f"{name} {after.games}/{ladder.placement_games}")
            continue
        label = _step_label(ladder, after.tier_key, after.division)
        if before is None or not before.placed:
            lines.append(f"🎯 **{name}** terminou a colocação: {label}")
            continue
        old = step_rank(ladder, before.tier_key, before.division)
        new = step_rank(ladder, after.tier_key, after.division)
        if old is None or new is None or old == new:
            continue
        up = new > old
        lines.append(
            f"{'📈' if up else '📉'} **{name}** {'subiu' if up else 'desceu'} para {label}"
        )
    if placing:
        lines.append("Em colocação: " + " · ".join(placing))
    return lines


def result_summary(
    snapshot: LobbySnapshot,
    events: Sequence[str],
    changes: Sequence[StandingChange],
    names: Mapping[int, str],
    ladder: LadderConfig,
) -> str | None:
    """The message posted when a result was confirmed, corrected or voided (tiers only)."""
    match = snapshot.match
    if match is None:
        return None
    lobby_id = snapshot.lobby.id
    winner = SIDE_NAME.get(match.winner or "", "?")
    if "voided" in events:
        head = f"🚫 In-house #{lobby_id} anulada ({match.void_reason or 'sem motivo'})."
    elif "fixed" in events:
        head = f"✏️ Resultado da in-house #{lobby_id} corrigido: **{winner} ganhou**."
    elif "result_confirmed" in events:
        how = " (confirmado automaticamente)" if match.auto_confirmed else ""
        head = f"🏆 In-house #{lobby_id}: **{winner} ganhou**{how}."
        hint = f"📝 Regista o teu campeão e KDA com `/match stats` (jogo #{match.id})."
        return "\n".join([head, *standing_lines(changes, names, ladder), hint])
    else:
        return None
    return "\n".join([head, *standing_lines(changes, names, ladder)])


MATCH_STATUS_TEXT = {
    "in_progress": "Em jogo",
    "reported": "Resultado por confirmar",
    "confirmed": "Confirmado",
    "voided": "Anulado",
}
PENDING_MARK = "⏳"


def stats_suffix(p: ParticipantRow, champion_name: str | None, icon: str = "") -> str:
    """ " — <icon> 8/2/11" for a player with stats: the champion's icon, else its name, then KDA.

    ⏳ is added while an admin has not validated the stats; validated stats carry no mark (the
    footer says who validated).
    """
    if not p.has_stats:
        return ""
    kda = kda_text(p.kills, p.deaths, p.assists)
    if icon:
        body = f" — {icon} {kda}"
    else:
        body = f" · {discord.utils.escape_markdown(champion_name or p.champion or '?')} {kda}"
    return body + (f" {PENDING_MARK}" if p.stats_status == PENDING else "")


def validation_footer(
    match: MatchRow, confirmed_by: str | None, validators: Sequence[str], pending: int
) -> str | None:
    """Who confirmed the result and who validated the KDAs (only for a confirmed game)."""
    if match.status != "confirmed":
        return None
    result = "Resultado confirmado automaticamente"
    if not match.auto_confirmed:
        result = f"Resultado confirmado por {confirmed_by or '?'}"
    parts = [result]
    if validators:
        parts.append(f"KDAs validados por {', '.join(validators)}")
    if pending:
        parts.append(f"{pending} por validar")
    return " · ".join(parts)


def build_pending_embed(items: Sequence[tuple[int, str, str]]) -> discord.Embed:
    """/match pending: (match id, nickname, stats text) waiting for an admin, oldest game first."""
    lines = [
        f"**#{match_id}** · {discord.utils.escape_markdown(who)} · {text}"
        for match_id, who, text in items
    ]
    embed = discord.Embed(
        title="Stats por validar",
        description="\n".join(lines) or "Não há stats por validar.",
        color=discord.Color.orange(),
    )
    if items:
        embed.set_footer(text="Valida com /match validate (jogo e, se quiseres, jogador).")
    return embed


def build_match_embed(
    match: MatchRow,
    teams: Mapping[str, Sequence[tuple[str, str, str]]],
    footer: str | None = None,
    roles: Mapping[str, str] | None = None,
) -> discord.Embed:
    """/match show: status, winner, and one line per player: role icon (or name), player, then the
    stats suffix. `footer` says who confirmed the result and validated the KDAs."""
    embed = discord.Embed(
        title=f"Jogo #{match.id} · {MATCH_STATUS_TEXT[match.status]}",
        color=discord.Color.green() if match.status == "confirmed" else discord.Color.dark_grey(),
    )
    if match.winner and match.status != "voided":
        embed.description = f"Vencedor: **{SIDE_NAME[match.winner]}**."
    elif match.status == "voided":
        embed.description = f"Anulado ({match.void_reason or 'sem motivo'})."
    for side in ("blue", "red"):
        lines = [
            f"{role_prefix(role, roles)}{discord.utils.escape_markdown(name)}{suffix}"
            for role, name, suffix in teams.get(side, ())
        ]
        embed.add_field(name=f"{SIDE_EMOJI[side]} {SIDE_NAME[side]}", value="\n".join(lines) or "—")
    if footer:
        embed.set_footer(text=footer)
    return embed


def build_list_embed(
    open_lobby: LobbySnapshot | None, scheduled: list[LobbySnapshot]
) -> discord.Embed:
    """/lobby list: the open spontaneous lobby and the upcoming scheduled ones."""
    lines = []
    if open_lobby is not None:
        lobby = open_lobby.lobby
        lines.append(
            f"**#{lobby.id}** · {STATUS_TEXT[lobby.status]} · "
            f"{len(open_lobby.roster)}/{ROSTER_SIZE} jogadores"
        )
    for snap in scheduled:
        lobby = snap.lobby
        stamp = int(lobby.scheduled_for.timestamp()) if lobby.scheduled_for else 0
        note = f" · {discord.utils.escape_markdown(lobby.note)}" if lobby.note else ""
        reserves = f" (+{len(snap.reserves)})" if snap.reserves else ""
        lines.append(
            f"**#{lobby.id}** · <t:{stamp}:F> (<t:{stamp}:R>) · "
            f"{len(snap.roster)}/{ROSTER_SIZE}{reserves}{note}"
        )
    return discord.Embed(
        title="Lobbies",
        description="\n".join(lines) or "Não há lobbies abertas nem agendadas.",
        color=discord.Color.blurple(),
    )


def scheduled_ping(snapshot: LobbySnapshot, event: str) -> tuple[str, list[EntryView]] | None:
    """Text and recipients (real signed-up players) for a scheduled lobby's reminder/notice."""
    recipients = [e for e in (*snapshot.roster, *snapshot.reserves) if not e.is_mock]
    lobby = snapshot.lobby
    if not recipients or lobby.scheduled_for is None:
        return None
    stamp = int(lobby.scheduled_for.timestamp())
    count = len(snapshot.roster)
    mentions = " ".join(f"<@{e.discord_id}>" for e in recipients)
    if event == "reminder":
        missing = f" Faltam {ROSTER_SIZE - count}." if count < ROSTER_SIZE else ""
        return (
            f"Lembrete: a in-house #{lobby.id} começa <t:{stamp}:R>. "
            f"Inscritos: {count}/{ROSTER_SIZE}.{missing} {mentions}",
            recipients,
        )
    return (
        f"É hora da in-house #{lobby.id}, mas só há {count}/{ROSTER_SIZE} inscritos. "
        f"Começa assim que houver 10 — chamem quem falta! {mentions}",
        recipients,
    )


def ready_check_ping(snapshot: LobbySnapshot, restarted: bool) -> str | None:
    """Text for the message that pings players when a ready-check (re)starts."""
    waiting = [e for e in snapshot.roster if e.status != READY and not e.is_mock]
    if not waiting or snapshot.lobby.ready_deadline is None:
        return None
    stamp = int(snapshot.lobby.ready_deadline.timestamp())
    head = "Mudança na lobby: ready-check reiniciado" if restarted else "Ready-check!"
    mentions = " ".join(f"<@{e.discord_id}>" for e in waiting)
    return f"{head} {mentions} — carreguem em **Pronto** <t:{stamp}:R>."
