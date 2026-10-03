"""Keep yt-dlp fresh while the bot runs.

YouTube changes often and an old yt-dlp stops playing anything. entrypoint.sh updates it on
every start; this does the same every YTDLP_UPDATE_HOURS for bots that run for weeks. A new
version only takes effect after a restart, so when one was installed the bot waits until
nobody is listening, then restarts itself in place (see bot.main).
"""

import asyncio
import logging
import sys
from typing import Optional

import config

log = logging.getLogger("musicbot.updater")

FIRST_CHECK = 15 * 60   # seconds after start (entrypoint.sh already updated at start)
IDLE_RETRY = 10 * 60    # waiting for everyone to stop listening before a restart
PIP = (sys.executable, "-m", "pip", "install", "--upgrade", "--quiet",
       "--disable-pip-version-check", "yt-dlp[default]")


def loaded_version() -> str:
    try:
        from yt_dlp.version import __version__
        return __version__
    except Exception:
        return ""


async def _run(*cmd: str, timeout: float = 300) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "timed out"
    return proc.returncode or 0, out.decode(errors="replace").strip()


async def installed_version() -> str:
    """Version a fresh Python process would load (after pip), not the one in memory."""
    code, out = await _run(sys.executable, "-c",
                           "from yt_dlp.version import __version__ as v; print(v)", timeout=60)
    return out.splitlines()[-1].strip() if code == 0 and out else ""


async def update() -> Optional[str]:
    """pip install -U yt-dlp. Returns the newly installed version, or None if unchanged
    or the update failed."""
    code, out = await _run(*PIP)
    if code != 0:
        log.warning("yt-dlp update failed (%s): %s", code, out[-300:])
        return None
    new = await installed_version()
    if new and new != loaded_version():
        return new
    return None


def idle(bot) -> bool:
    """Nobody would notice a restart: nothing playing, queued or held 24/7."""
    return all(not p.current and not p.queue and not p.stay_247 for p in bot.players.values())


async def loop(bot):
    if config.YTDLP_UPDATE_HOURS <= 0:
        return
    await asyncio.sleep(FIRST_CHECK)
    while not bot.is_closed():
        new = None
        try:
            new = await update()
        except Exception:
            log.exception("yt-dlp update check failed")
        if new:
            log.info("yt-dlp %s installed (running %s); restarting when nobody is listening",
                     new, loaded_version())
            while not idle(bot):
                await asyncio.sleep(IDLE_RETRY)
            log.info("Restarting to load yt-dlp %s", new)
            bot.restart_requested = True
            await bot.close()
            return
        await asyncio.sleep(config.YTDLP_UPDATE_HOURS * 3600)
