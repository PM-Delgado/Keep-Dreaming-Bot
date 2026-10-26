"""Admin allowlist check, driven by `Settings.admin_ids` (no Discord role needed)."""

import discord
from discord import app_commands

from bot.config import Settings


def is_admin(settings: Settings, user_id: int) -> bool:
    return user_id in settings.admin_ids


async def _admin_predicate(interaction: discord.Interaction) -> bool:
    settings: Settings = interaction.client.settings  # type: ignore[attr-defined]
    return is_admin(settings, interaction.user.id)


# Fails closed: with an empty allowlist nobody passes. `KDBot.on_tree_error` reports the denial.
admin_only = app_commands.check(_admin_predicate)
