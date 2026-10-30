"""Lobby messages (embeds and text). All wording and layout live here so the look can be
reworked later without touching lobby rules. Never shows ratings or the balance of the teams."""

from collections.abc import Mapping, Sequence
from datetime import timedelta

import discord

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
from bot.features.lobby.service import EntryView, LobbySnapshot, SlotView
from bot.features.matches.repository import PENDING, MatchRow, ParticipantRow
from bot.features.matches.service import StandingChange, kda_text
from bot.features.players.text import progress_bar
from bot.features.ratings.ladder import LadderConfig, step_rank

STATUS_TEXT = {
    OPEN: "Aberta",
    READY_CHECK: "Ready-check",
    TEAMS_PROPOSED: "Equipas propostas",
    IN_PROGRESS: "Em jogo",
    CANCELLED: "Cancelada",
    COMPLETED: "Terminada",
}
STATUS_EMOJI = {
    OPEN: "🟢",
    READY_CHECK: "⏱️",
    TEAMS_PROPOSED: "⚖️",
    IN_PROGRESS: "⚔️",
    CANCELLED: "🚫",
    COMPLETED: "🏆",
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


def game_label(snapshot: LobbySnapshot) -> str:
    """One number for the group: the game's once it exists ("Jogo #1"), the lobby's before."""
    if snapshot.match is not None:
        return f"Jogo #{snapshot.match.id}"
    return f"Scrim #{snapshot.lobby.id}"


def team_text(slots: list[SlotView], roles: Mapping[str, str] | None = None) -> str:
    """One line per slot: the role icon (or the role's name) and the player."""
    return "\n".join(
        f"{role_prefix(s.role, roles)}{discord.utils.escape_markdown(s.nickname)}" for s in slots
    )


def build_lobby_embed(
    snapshot: LobbySnapshot,
    auto_confirm_minutes: int = 30,
    roles: Mapping[str, str] | None = None,
) -> discord.Embed:
    """A large heading with the state, a line saying what happens next, the teams, and a small
    note at the bottom (no footer)."""
    lobby = snapshot.lobby
    status = lobby.status
    scheduled_open = lobby.kind == "scheduled" and status == OPEN
    emoji = "📅" if scheduled_open else STATUS_EMOJI[status]
    state = "Agendado" if scheduled_open else STATUS_TEXT[status]
    embed = discord.Embed(color=STATUS_COLOR[status])
    lines = [f"# {emoji} {game_label(snapshot)} · {state}"]
    if lobby.note:
        embed.add_field(name="Nota", value=discord.utils.escape_markdown(lobby.note), inline=False)

    if status in (OPEN, READY_CHECK):
        count = len(snapshot.roster)
        bar = progress_bar(count / ROSTER_SIZE)
        embed.add_field(
            name=f"Jogadores ({count}/{ROSTER_SIZE})", value=f"{bar}\n{roster_text(snapshot)}"
        )
        if snapshot.reserves:
            embed.add_field(name="Reservas", value=reserves_text(snapshot), inline=False)

    if scheduled_open and lobby.scheduled_for is not None:
        stamp = int(lobby.scheduled_for.timestamp())
        lines.append(f"- **Quando:** <t:{stamp}:F> (<t:{stamp}:R>)")
        lines.append("- Carrega em **Entrar** para guardares lugar.")
        if lobby.reminder_stage >= 2:
            lines.append("- A hora já passou: o ready-check começa assim que houver 10 jogadores.")
        else:
            lines.append("- À hora marcada, com 10 jogadores, começa o ready-check.")
    elif status == OPEN:
        lines.append("- Carrega em **Entrar** para guardares lugar.")
        lines.append("- Com 10 jogadores começa o ready-check.")
    elif status == READY_CHECK and lobby.ready_deadline is not None:
        stamp = int(lobby.ready_deadline.timestamp())
        lines.append(f"- **Prazo:** todos têm de carregar em **Pronto** <t:{stamp}:R>.")
        lines.append("- Quem não confirmar sai e é substituído pelas reservas.")

    if status in (TEAMS_PROPOSED, IN_PROGRESS, COMPLETED):
        failure = _add_teams(embed, snapshot, roles)
        if failure:
            lines.append(f"- {failure}")
    if status in (IN_PROGRESS, COMPLETED):
        lines.extend(_describe_game(snapshot, auto_confirm_minutes))

    if status == CANCELLED:
        lines.append(f"- Lobby encerrada ({lobby.cancel_reason or 'cancelada'}).")
    elif status == IN_PROGRESS:
        lines.append("-# Só o resultado confirmado conta para o rank.")
    elif status == TEAMS_PROPOSED:
        lines.append("-# Só o criador ou um admin confirma ou cancela.")
    embed.description = "\n".join(lines)
    return embed


def _add_teams(
    embed: discord.Embed, snapshot: LobbySnapshot, roles: Mapping[str, str] | None = None
) -> str | None:
    """Adds the team fields; returns a sentence when no teams could be formed."""
    proposal = snapshot.proposal
    if proposal is None:
        return "Não consegui formar equipas. Um admin pode cancelar e recomeçar."
    embed.add_field(name=f"{SIDE_EMOJI['blue']} Azul", value=team_text(proposal.blue, roles))
    embed.add_field(name=f"{SIDE_EMOJI['red']} Vermelho", value=team_text(proposal.red, roles))
    return None


def _describe_game(snapshot: LobbySnapshot, auto_minutes: int) -> list[str]:
    """The bullets under the heading of a started game: playing, waiting for a result, done."""
    match = snapshot.match
    if match is None:
        return []
    winner = SIDE_NAME.get(match.winner or "", "?")
    if match.status == "confirmed":
        how = " (confirmado automaticamente)" if match.auto_confirmed else ""
        return [f"- **Resultado:** {winner} ganhou{how}."]
    if match.status == "reported" and match.reported_at is not None:
        who = discord.utils.escape_markdown(snapshot.reporter or "?")
        if match.contested:
            return [
                f"- **{who}** reportou que o **{winner}** ganhou, mas o resultado foi contestado.",
                "- Um admin tem de decidir (botões da equipa vencedora).",
            ]
        stamp = int((match.reported_at + timedelta(minutes=auto_minutes)).timestamp())
        return [
            f"- **{who}** reportou: **{winner} ganhou**.",
            "- Alguém da outra equipa confirma, ou contesta se não é verdade.",
            f"- Sem objeções, confirma <t:{stamp}:R>.",
        ]
    if match.status == "in_progress":
        return [
            "- **Estado:** jogo em curso.",
            "- Quando acabar, alguém carrega no botão da equipa vencedora.",
        ]
    return []


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
        f"📣 {mentions} — foi reportado que o **{SIDE_NAME[match.winner]} ganhou** o "
        f"{game_label(snapshot)}. Confirmem ou contestem na mensagem da lobby.",
        targets,
    )


def result_reminder_ping(snapshot: LobbySnapshot) -> tuple[str, list[EntryView]] | None:
    targets = [e for e in snapshot.roster if not e.is_mock]
    if not targets:
        return None
    mentions = " ".join(f"<@{e.discord_id}>" for e in targets)
    return (
        f"⏰ {mentions} — o {game_label(snapshot)} ainda não tem resultado. "
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
    label = game_label(snapshot)
    winner = SIDE_NAME.get(match.winner or "", "?")
    if "voided" in events:
        head = f"🚫 {label} anulado ({match.void_reason or 'sem motivo'})."
    elif "fixed" in events:
        head = f"✏️ Resultado do {label} corrigido: **{winner} ganhou**."
    elif "result_confirmed" in events:
        how = " (confirmado automaticamente)" if match.auto_confirmed else ""
        head = f"🏆 {label}: **{winner} ganhou**{how}!"
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
    body = lines or ["Não há stats por validar. Tudo em dia! 🎉"]
    if items:
        body += ["", "-# Valida com /match validate (jogo e, se quiseres, jogador)."]
    return discord.Embed(
        description="\n".join(["# ⏳ Stats por validar", "", *body]), color=discord.Color.orange()
    )


MATCH_STATUS_EMOJI = {"in_progress": "⚔️", "reported": "📣", "confirmed": "🏆", "voided": "🚫"}
SIDE_COLOR = {"blue": discord.Color.blue(), "red": discord.Color.red()}


def build_match_embed(
    match: MatchRow,
    teams: Mapping[str, Sequence[tuple[str, str, str]]],
    footer: str | None = None,
    roles: Mapping[str, str] | None = None,
) -> discord.Embed:
    """/match show: a large heading, the winner, and one line per player: role icon (or name),
    player, then the stats suffix. A confirmed game takes the winning side's colour. `footer`
    (who confirmed the result and validated the KDAs) is a small line at the bottom."""
    color = discord.Color.dark_grey()
    if match.status == "confirmed" and match.winner:
        color = SIDE_COLOR[match.winner]
    embed = discord.Embed(color=color)
    emoji = MATCH_STATUS_EMOJI[match.status]
    lines = [f"# {emoji} Jogo #{match.id} · {MATCH_STATUS_TEXT[match.status]}"]
    if match.winner and match.status != "voided":
        verb = "ganhou" if match.status == "confirmed" else "ganhou (por confirmar)"
        lines.append(f"{SIDE_EMOJI[match.winner]} **{SIDE_NAME[match.winner]}** {verb}")
    elif match.status == "voided":
        lines.append(f"Anulado ({match.void_reason or 'sem motivo'}).")
    for side in ("blue", "red"):
        team = [
            f"{role_prefix(role, roles)}{discord.utils.escape_markdown(name)}{suffix}"
            for role, name, suffix in teams.get(side, ())
        ]
        embed.add_field(name=f"{SIDE_EMOJI[side]} {SIDE_NAME[side]}", value="\n".join(team) or "—")
    if footer:
        lines.append(f"-# {footer}")
    embed.description = "\n".join(lines)
    return embed


def build_list_embed(
    open_lobby: LobbySnapshot | None, scheduled: list[LobbySnapshot]
) -> discord.Embed:
    """/lobby list: the open spontaneous lobby and the upcoming scheduled ones."""
    lines = []
    if open_lobby is not None:
        lobby = open_lobby.lobby
        lines.append(
            f"{STATUS_EMOJI[lobby.status]} **#{lobby.id}** · {STATUS_TEXT[lobby.status]} · "
            f"{len(open_lobby.roster)}/{ROSTER_SIZE} jogadores"
        )
    for snap in scheduled:
        lobby = snap.lobby
        stamp = int(lobby.scheduled_for.timestamp()) if lobby.scheduled_for else 0
        note = f" · {discord.utils.escape_markdown(lobby.note)}" if lobby.note else ""
        reserves = f" (+{len(snap.reserves)})" if snap.reserves else ""
        lines.append(
            f"📅 **#{lobby.id}** · <t:{stamp}:F> (<t:{stamp}:R>) · "
            f"{len(snap.roster)}/{ROSTER_SIZE}{reserves}{note}"
        )
    body = lines or ["Não há lobbies abertas nem agendadas. Usa `/lobby open` para começar! 🎮"]
    return discord.Embed(
        description="\n".join(["# 📋 Scrims", "", *body]), color=discord.Color.blurple()
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
            f"⏰ Lembrete: o Scrim #{lobby.id} começa <t:{stamp}:R>. "
            f"Inscritos: {count}/{ROSTER_SIZE}.{missing} {mentions}",
            recipients,
        )
    return (
        f"⌛ É hora do Scrim #{lobby.id}, mas só há {count}/{ROSTER_SIZE} inscritos. "
        f"Começa assim que houver 10 — chamem quem falta! {mentions}",
        recipients,
    )


def ready_check_ping(snapshot: LobbySnapshot, restarted: bool) -> str | None:
    """Text for the message that pings players when a ready-check (re)starts."""
    waiting = [e for e in snapshot.roster if e.status != READY and not e.is_mock]
    if not waiting or snapshot.lobby.ready_deadline is None:
        return None
    stamp = int(snapshot.lobby.ready_deadline.timestamp())
    head = "🔄 Mudança na lobby: ready-check reiniciado" if restarted else "⏱️ Ready-check!"
    mentions = " ".join(f"<@{e.discord_id}>" for e in waiting)
    return f"{head} {mentions} — carreguem em **Pronto** <t:{stamp}:R>."
