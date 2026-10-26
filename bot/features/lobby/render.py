"""Lobby messages (embeds and text). All wording and layout live here so the look can be
reworked later without touching lobby rules. Never shows ratings, only the coarse balance band."""

import discord

from bot.features.balance.model import BAND_CLEAR, BAND_EVEN, BAND_LOPSIDED, BAND_SLIGHT
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
from bot.features.players.proficiency import ROLE_NAMES_PT

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


def team_text(slots: list[SlotView]) -> str:
    return "\n".join(
        f"**{ROLE_NAMES_PT[s.role]}** — {discord.utils.escape_markdown(s.nickname)}{fit_note(s)}"
        for s in slots
    )


def build_lobby_embed(snapshot: LobbySnapshot) -> discord.Embed:
    lobby = snapshot.lobby
    embed = discord.Embed(
        title=f"In-house #{lobby.id} · {STATUS_TEXT[lobby.status]}",
        color=STATUS_COLOR[lobby.status],
    )
    status = lobby.status

    if status in (OPEN, READY_CHECK):
        count = len(snapshot.roster)
        embed.add_field(name=f"Jogadores ({count}/{ROSTER_SIZE})", value=roster_text(snapshot))
        if snapshot.reserves:
            embed.add_field(name="Reservas", value=reserves_text(snapshot), inline=False)

    if status == OPEN:
        embed.description = "Carrega em **Entrar**. Com 10 jogadores começa o ready-check."
    elif status == READY_CHECK and lobby.ready_deadline is not None:
        stamp = int(lobby.ready_deadline.timestamp())
        embed.description = (
            f"Todos têm de carregar em **Pronto** <t:{stamp}:R>. Quem não confirmar sai e "
            "é substituído pelas reservas."
        )

    if status in (TEAMS_PROPOSED, IN_PROGRESS):
        _add_teams(embed, snapshot)

    if status == CANCELLED:
        embed.description = f"Lobby encerrada ({lobby.cancel_reason or 'cancelada'})."
    elif status == IN_PROGRESS:
        embed.set_footer(text="Boa sorte! O registo do resultado chega numa próxima versão.")
    elif status == TEAMS_PROPOSED:
        embed.set_footer(text="Só o criador ou um admin confirma ou cancela. N/5 = nível na role.")
    return embed


def _add_teams(embed: discord.Embed, snapshot: LobbySnapshot) -> None:
    proposal = snapshot.proposal
    if proposal is None:
        embed.description = "Não consegui formar equipas. Um admin pode cancelar e recomeçar."
        return
    embed.add_field(name=f"{SIDE_EMOJI['blue']} Azul", value=team_text(proposal.blue))
    embed.add_field(name=f"{SIDE_EMOJI['red']} Vermelho", value=team_text(proposal.red))
    embed.add_field(name="Equilíbrio", value=band_text(proposal), inline=False)


def ready_check_ping(snapshot: LobbySnapshot, restarted: bool) -> str | None:
    """Text for the message that pings players when a ready-check (re)starts."""
    waiting = [e for e in snapshot.roster if e.status != READY and not e.is_mock]
    if not waiting or snapshot.lobby.ready_deadline is None:
        return None
    stamp = int(snapshot.lobby.ready_deadline.timestamp())
    head = "Mudança na lobby: ready-check reiniciado" if restarted else "Ready-check!"
    mentions = " ".join(f"<@{e.discord_id}>" for e in waiting)
    return f"{head} {mentions} — carreguem em **Pronto** <t:{stamp}:R>."
