"""The `/riot duo` tab buttons: 📊 Resumo / 🎮 Jogos / 🏆 Recordes.

Each button carries everything in its `custom_id` (`duo:<tab>:<player a>:<player b>:<owner>`,
Discord ids), so one registered `DynamicItem` answers every `/riot duo` message, also the ones
posted before a restart, and the views have no timeout. The owner is whoever may flip the public
message; a click by anybody else gets a private copy on the tab they chose (as in `/rivalry`).
"""

import re

import discord
from discord.ext import commands

from bot.features.riot.duo_render import TAB_EMOJIS, TAB_LABELS, TABS

DUO_ID = r"duo:(?P<tab>summary|games|records):(?P<a>\d+):(?P<b>\d+):(?P<owner>\d+)"


class DuoTabButton(discord.ui.DynamicItem[discord.ui.Button], template=DUO_ID):  # type: ignore[type-arg]
    def __init__(self, tab: str, a: int, b: int, owner: int, current: bool = False) -> None:
        self.tab, self.a, self.b, self.owner = tab, a, b, owner
        super().__init__(
            discord.ui.Button(
                label=TAB_LABELS[tab],
                emoji=TAB_EMOJIS[tab],
                style=discord.ButtonStyle.primary if current else discord.ButtonStyle.secondary,
                disabled=current,
                custom_id=f"duo:{tab}:{a}:{b}:{owner}",
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ) -> "DuoTabButton":
        return cls(match["tab"], int(match["a"]), int(match["b"]), int(match["owner"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        cog = bot.get_cog("Riot") if isinstance(bot, commands.Bot) else None
        if cog is not None:
            await cog.flip_duo(  # type: ignore[attr-defined]
                interaction, self.tab, self.a, self.b, self.owner
            )


def duo_view(tab: str, a: int, b: int, owner: int) -> discord.ui.View:
    """The three tab buttons; the current tab's is highlighted and disabled."""
    view = discord.ui.View(timeout=None)
    for name in TABS:
        view.add_item(DuoTabButton(name, a, b, owner, current=name == tab))
    return view
