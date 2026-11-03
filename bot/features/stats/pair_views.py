"""The `/rivalry` tab buttons: ⚔️ Confrontos / 🤝 Sinergia / 📜 Jogos.

Each button carries everything in its `custom_id` (`pair:<tab>:<player a>:<player b>:<owner>`,
Discord ids), so one registered `DynamicItem` answers every `/rivalry` message, also the ones posted
before a restart, and the views have no timeout. The owner is whoever may flip the public message;
a click by anybody else gets a private copy on the tab they chose.
"""

import discord
from discord.ext import commands

from bot.features.stats.pair_render import TAB_EMOJIS, TAB_LABELS
from bot.features.stats.pairs import TABS

PAIR_ID = r"pair:(?P<tab>h2h|synergy|games):(?P<a>\d+):(?P<b>\d+):(?P<owner>\d+)"


class PairTabButton(discord.ui.DynamicItem[discord.ui.Button], template=PAIR_ID):  # type: ignore[type-arg]
    def __init__(self, tab: str, a: int, b: int, owner: int, current: bool = False) -> None:
        self.tab, self.a, self.b, self.owner = tab, a, b, owner
        super().__init__(
            discord.ui.Button(
                label=TAB_LABELS[tab],
                emoji=TAB_EMOJIS[tab],
                style=discord.ButtonStyle.primary if current else discord.ButtonStyle.secondary,
                disabled=current,
                custom_id=f"pair:{tab}:{a}:{b}:{owner}",
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button, match
    ) -> "PairTabButton":
        return cls(match["tab"], int(match["a"]), int(match["b"]), int(match["owner"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        cog = bot.get_cog("Stats") if isinstance(bot, commands.Bot) else None
        if cog is not None:
            await cog.flip_pair(  # type: ignore[attr-defined]
                interaction, self.tab, self.a, self.b, self.owner
            )


def pair_view(tab: str, a: int, b: int, owner: int) -> discord.ui.View:
    """The three tab buttons; the current tab's is highlighted and disabled."""
    view = discord.ui.View(timeout=None)
    for name in TABS:
        view.add_item(PairTabButton(name, a, b, owner, current=name == tab))
    return view
