"""Discord components for lobbies: the persistent action buttons and the role-choice menus.

The buttons use fixed `custom_id`s and no timeout, so one registered `LobbyView` handles every
lobby message, including those posted before a restart; which lobby a click belongs to is found
from `interaction.message.id` in the database.
"""

from typing import TYPE_CHECKING

import discord

from bot.features.lobby.repository import (
    IN_PROGRESS,
    OPEN,
    READY_CHECK,
    TEAMS_PROPOSED,
)

if TYPE_CHECKING:
    from bot.features.lobby.cog import Lobby
    from bot.features.lobby.service import LobbySnapshot

# action -> (label, style)
ACTIONS: dict[str, tuple[str, discord.ButtonStyle]] = {
    "join": ("Entrar", discord.ButtonStyle.success),
    "leave": ("Sair", discord.ButtonStyle.secondary),
    "ready": ("Pronto", discord.ButtonStyle.success),
    "decline": ("Recusar", discord.ButtonStyle.danger),
    "confirm": ("Confirmar", discord.ButtonStyle.success),
    "cancel": ("Cancelar", discord.ButtonStyle.danger),
    "report_blue": ("Azul ganhou", discord.ButtonStyle.primary),
    "report_red": ("Vermelho ganhou", discord.ButtonStyle.danger),
    "confirm_result": ("Confirmar resultado", discord.ButtonStyle.success),
    "contest": ("Contestar", discord.ButtonStyle.secondary),
}
# A started game has sub-states that depend on its match, not only on the lobby status.
REPORTED, CONTESTED = "REPORTED", "CONTESTED"
# Which buttons each state shows.
VISIBLE: dict[str, tuple[str, ...]] = {
    OPEN: ("join", "leave", "cancel"),
    READY_CHECK: ("ready", "decline", "join", "cancel"),
    TEAMS_PROPOSED: ("confirm", "leave", "cancel"),
    IN_PROGRESS: ("report_blue", "report_red", "cancel"),
    REPORTED: ("confirm_result", "contest", "report_blue", "report_red", "cancel"),
    CONTESTED: ("report_blue", "report_red", "confirm_result", "cancel"),
}


def state_of(snapshot: "LobbySnapshot") -> str:
    """The key into VISIBLE for a lobby: its status, refined by its match once started."""
    match = snapshot.match
    if snapshot.lobby.status == IN_PROGRESS and match is not None and match.status == "reported":
        return CONTESTED if match.contested else REPORTED
    return snapshot.lobby.status


class ActionButton(discord.ui.Button["LobbyView"]):
    def __init__(self, cog: "Lobby", action: str) -> None:
        label, style = ACTIONS[action]
        super().__init__(label=label, style=style, custom_id=f"lobby:{action}")
        self.cog = cog
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.on_action(interaction, self.action)


class LobbyView(discord.ui.View):
    """`status=None` builds the view with every button, used once to register persistence."""

    def __init__(self, cog: "Lobby", status: str | None) -> None:
        super().__init__(timeout=None)
        actions = tuple(ACTIONS) if status is None else VISIBLE.get(status, ())
        for action in actions:
            self.add_item(ActionButton(cog, action))
