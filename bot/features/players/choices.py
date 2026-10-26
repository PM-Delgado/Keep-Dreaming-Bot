"""Shared slash-command choice lists."""

from discord import app_commands

from bot.features.players.proficiency import ROLE_NAMES_PT, ROLES
from bot.features.ratings.rank import ALL_TIERS

TIER_CHOICES = [app_commands.Choice(name=t.capitalize(), value=t) for t in ALL_TIERS]
DIVISION_CHOICES = [app_commands.Choice(name=d, value=d) for d in ("IV", "III", "II", "I")]
ROLE_CHOICES = [app_commands.Choice(name=ROLE_NAMES_PT[r], value=r) for r in ROLES]
PROFICIENCY_CHOICES = [
    app_commands.Choice(name="Limpar", value=0),
    app_commands.Choice(name="1 - Muito pouco à vontade", value=1),
    app_commands.Choice(name="2 - Pouco à vontade", value=2),
    app_commands.Choice(name="3 - Ok", value=3),
    app_commands.Choice(name="4 - Confortável", value=4),
    app_commands.Choice(name="5 - Main / forte", value=5),
]
