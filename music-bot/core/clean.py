"""Auto-clean: command replies and bot chatter in text channels delete themselves,
so the channel only keeps the live now-playing panel. Per server: /settings autoclean."""

import asyncio
import logging
from typing import Optional

import discord

import config

log = logging.getLogger("musicbot.clean")

# Commands whose replies (and the user's prefix message) are removed after a while.
QUICK = {
    "play", "playnext", "skip", "previous", "pause", "resume", "stop", "volume", "loop",
    "shuffle", "remove", "move", "jump", "clear", "seek", "forward", "backward", "replay",
    "undo", "playlist load",
}
# Replies people read for longer.
SLOW = {"queue", "nowplaying"}


def delay_for(command: str) -> Optional[int]:
    if command in QUICK:
        return config.AUTO_CLEAN_SECONDS
    if command in SLOW:
        return max(config.AUTO_CLEAN_SECONDS * 6, 120)
    return None


async def enabled(bot, guild_id: Optional[int]) -> bool:
    if not guild_id:
        return False
    p = bot.players.get(guild_id)
    if p is not None:
        return p.auto_clean
    try:
        return bool((await bot.db.get_settings(guild_id))["auto_clean"])
    except Exception:
        return config.AUTO_CLEAN


def later(delay: float, *targets):
    """Delete messages (or an interaction's original response) after a delay."""
    async def run():
        await asyncio.sleep(delay)
        for t in targets:
            try:
                if isinstance(t, discord.Interaction):
                    await t.delete_original_response()
                else:
                    await t.delete()
            except discord.HTTPException:
                pass  # already gone, or no Manage Messages for someone else's message
            except Exception as exc:
                log.debug("auto-clean failed: %s", exc)
    asyncio.create_task(run())
