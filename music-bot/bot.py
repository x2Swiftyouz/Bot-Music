"""Entry point: python bot.py"""

import asyncio
import logging
import signal
import time

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

import config
from core import look

if config.UI_STYLE == "groove":
    look.install()  # before anything sends a message

from core.checks import UserError  # noqa: E402
from core import card, clean, clock, lyrics
from core.db import Database
from core.player import GuildPlayer
from core.sources import spotify
from core.ui import CompactPanelView, LegacyVolumeView, PanelView

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
log = logging.getLogger("musicbot")

EXTENSIONS = ("cogs.music", "cogs.settings", "cogs.playlists", "cogs.info", "cogs.cards",
              "cogs.request", "cogs.prefix")


class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.message_content = config.MESSAGE_CONTENT
        super().__init__(command_prefix=commands.when_mentioned, intents=intents,
                         help_command=None)
        self.db = Database()
        self.players: dict[int, GuildPlayer] = {}
        self.panel_view: PanelView | None = None
        self.shutting_down = False
        self.started_at = time.time()
        self._restored = False
        self._health_runner: web.AppRunner | None = None

    async def get_player(self, guild: discord.Guild) -> GuildPlayer:
        p = self.players.get(guild.id)
        if p is None or p.destroyed:
            settings = await self.db.get_settings(guild.id)
            p = GuildPlayer(self, guild, settings)
            self.players[guild.id] = p
        return p

    async def setup_hook(self):
        await self.db.connect()
        self.panel_view = PanelView()
        self.add_view(self.panel_view)  # buttons keep working after restart
        self.add_view(CompactPanelView())
        self.add_view(LegacyVolumeView())
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        self.tree.on_error = self.on_app_error
        synced = await self.tree.sync()
        log.info("Synced %d slash commands", len(synced))
        await self._start_health()
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.close()))
        except (NotImplementedError, RuntimeError):
            pass  # Windows

    async def on_ready(self):
        log.info("Logged in as %s (%s) in %d guilds", self.user, self.user.id, len(self.guilds))
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.listening, name="/play"))
        if not self._restored:
            self._restored = True
            asyncio.create_task(self.get_cog("Music").restore_all())

    async def on_interaction(self, inter: discord.Interaction):
        clock.observe(inter)  # carries Discord's time: keeps countdowns right

    async def on_app_command_completion(self, inter: discord.Interaction, command):
        """Audit log: every successful slash command with its options."""
        if not inter.guild_id:
            return
        try:
            detail = " ".join(f"{k}={v}" for k, v in inter.namespace if v is not None)
            await self.db.audit(inter.guild_id, inter.user.id, command.qualified_name, detail)
        except Exception:
            pass
        delay = clean.delay_for(command.qualified_name)
        if delay and await clean.enabled(self, inter.guild_id):
            clean.later(delay, inter)

    async def on_app_error(self, inter: discord.Interaction, error: app_commands.AppCommandError):
        original = getattr(error, "original", error)
        if isinstance(original, UserError):
            msg = f"⚠️ {original}"
        elif isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ ใจเย็น รออีก {error.retry_after:.1f} วินาที"
        elif isinstance(error, app_commands.MissingPermissions):
            msg = "ไม่มีสิทธิ์ใช้คำสั่งนี้"
        else:
            log.exception("Command error", exc_info=error)
            msg = "❌ เกิดข้อผิดพลาด ลองใหม่"
        try:
            if inter.response.is_done():
                await inter.followup.send(msg, ephemeral=True)
            else:
                await inter.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    # ----------------------------------------------------------- health
    async def _start_health(self):
        if not config.HEALTH_PORT:
            return

        async def health(_req):
            ok = self.is_ready() and not self.is_closed()
            return web.json_response({
                "status": "ok" if ok else "starting",
                "guilds": len(self.guilds),
                "players": len(self.players),
                "playing": sum(1 for p in self.players.values() if p.current),
                "latency_ms": round(self.latency * 1000) if ok else None,
                "uptime_s": int(time.time() - self.started_at),
            }, status=200 if ok else 503)

        app = web.Application()
        app.router.add_get("/health", health)
        self._health_runner = web.AppRunner(app)
        await self._health_runner.setup()
        await web.TCPSite(self._health_runner, "0.0.0.0", config.HEALTH_PORT).start()
        log.info("Health endpoint on :%d/health", config.HEALTH_PORT)

    # --------------------------------------------------------- shutdown
    async def close(self):
        if self.shutting_down:
            return
        self.shutting_down = True
        log.info("Shutting down, saving queues…")
        for p in list(self.players.values()):
            try:
                await p.save_state()
            except Exception:
                log.exception("save on shutdown failed")
        if self._health_runner:
            await self._health_runner.cleanup()
        await spotify.close()
        await lyrics.close()
        await card.close()
        await super().close()
        await self.db.close()


def main():
    if not config.TOKEN:
        raise SystemExit("DISCORD_TOKEN missing. Copy .env.example to .env and fill it.")
    if not config.FFMPEG:
        raise SystemExit("FFmpeg not found. Run: pip install imageio-ffmpeg, "
                         "or install FFmpeg, or set FFMPEG_PATH in .env")
    from core.player import OPUS_LOADED
    log.info("FFmpeg: %s (stream mode: %s)", config.FFMPEG, config.STREAM_MODE)
    log.info("Opus: %s", "libopus loaded" if OPUS_LOADED else "not found, FFmpeg encodes Opus")
    if config.DENO:
        log.info("Deno: %s", config.DENO)
    else:
        log.warning("Deno not found. YouTube may fail. Run: pip install deno")
    bot = MusicBot()
    try:
        bot.run(config.TOKEN, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        raise SystemExit(
            "Enable 'Message Content Intent' in the Developer Portal, "
            "or set MESSAGE_CONTENT=false in .env")


if __name__ == "__main__":
    main()
