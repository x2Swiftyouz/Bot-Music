"""Shared permission checks. Raise UserError to show a friendly ephemeral message."""

from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from core.player import GuildPlayer


class UserError(app_commands.AppCommandError):
    """Error with a message safe to show to the user."""


def get_player(inter: discord.Interaction) -> "GuildPlayer":
    player = inter.client.players.get(inter.guild_id)  # type: ignore[attr-defined]
    if not player or player.destroyed:
        raise UserError("ไม่มีเพลงเล่นอยู่")
    return player


def require_same_channel(inter: discord.Interaction):
    vc = inter.guild.voice_client
    voice = getattr(inter.user, "voice", None)
    if not vc or not voice or voice.channel != vc.channel:
        raise UserError("ต้องอยู่ห้องเสียงเดียวกับบอท")


def control(inter: discord.Interaction) -> "GuildPlayer":
    """Player must exist and the user must be in the bot's voice channel."""
    return control_member(inter.client, inter.user)  # type: ignore[arg-type]


def control_member(bot, member: discord.Member) -> "GuildPlayer":
    """Same as control(), for prefix commands and other non-interaction callers."""
    player = bot.players.get(member.guild.id)
    if not player or player.destroyed:
        raise UserError("ไม่มีเพลงเล่นอยู่")
    vc = member.guild.voice_client
    voice = member.voice
    if not vc or not voice or voice.channel != vc.channel:
        raise UserError("ต้องอยู่ห้องเสียงเดียวกับบอท")
    return player
