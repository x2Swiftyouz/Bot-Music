"""All settings come from environment variables (.env)."""

import os
import shutil

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _cpu_limit():
    """CPUs this container may use (cgroup v2 or v1), or None when not limited."""
    try:
        with open("/sys/fs/cgroup/cpu.max") as fh:
            quota, period = fh.read().split()[:2]
            return None if quota == "max" else int(quota) / int(period)
    except (OSError, ValueError):
        pass
    try:
        with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us") as fq, \
                open("/sys/fs/cgroup/cpu/cpu.cfs_period_us") as fp:
            quota = int(fq.read())
            return quota / int(fp.read()) if quota > 0 else None
    except (OSError, ValueError):
        return None


CPU_LIMIT = _cpu_limit()
# Low-CPU mode (auto when the bot may use less than one CPU): drawing a moving card every
# few seconds would take the CPU the music needs. Cards then move only at the start of a
# song and refresh every 20 s. LOW_CPU=true / false forces it on or off.
_low = os.getenv("LOW_CPU", "auto").strip().lower()
LOW_CPU = ((CPU_LIMIT is not None and CPU_LIMIT < 1.0) or (os.cpu_count() or 2) < 2
           if _low == "auto" else _low in ("1", "true", "yes", "on"))


TOKEN = os.getenv("DISCORD_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "data/musicbot.db")

# Playback limits
IDLE_TIMEOUT = _int("IDLE_TIMEOUT", 300)          # seconds before leaving an empty queue
ALONE_TIMEOUT = _int("ALONE_TIMEOUT", 30)         # seconds before leaving an empty channel
AWAY_TIMEOUT = _int("AWAY_TIMEOUT", 300)          # everyone left mid-song: pause and wait this long (0 = off)
MAX_QUEUE = _int("MAX_QUEUE", 500)
MAX_PER_USER = _int("MAX_PER_USER", 100)          # 0 = unlimited
MAX_DURATION = _int("MAX_DURATION", 0)            # seconds, 0 = unlimited
PLAY_COOLDOWN = _int("PLAY_COOLDOWN", 3)          # seconds between /play per user
HISTORY_SIZE = _int("HISTORY_SIZE", 20)
DEFAULT_VOLUME = _int("DEFAULT_VOLUME", 50)

# yt-dlp
AUTOCOMPLETE = _bool("AUTOCOMPLETE", True)       # live search while typing /play
YTDL_TIMEOUT = _int("YTDL_TIMEOUT", 45)       # seconds per extraction
LOG_FILE = os.getenv("LOG_FILE", "data/logs/bot.log")  # "" = console only
LOG_MAX_MB = max(_int("LOG_MAX_MB", 5), 1)       # per file; LOG_BACKUPS older files are kept
LOG_BACKUPS = max(_int("LOG_BACKUPS", 3), 0)
LOG_FRESH = _bool("LOG_FRESH", True)              # start a new log file on every start
CARD_SERVER_ICON = _bool("CARD_SERVER_ICON", True)  # server icon in the card's corner
CARD_MARQUEE = _bool("CARD_MARQUEE", not LOW_CPU)  # long titles scroll on moving cards
CARD_PROCESS = _bool("CARD_PROCESS", True)     # draw cards in a worker process (no stutter)
YTDL_PROCESSES = _int("YTDL_PROCESSES", 2)    # yt-dlp worker processes (0 = threads, old way)
YTDL_DEBUG = _bool("YTDL_DEBUG", False)         # verbose yt-dlp log
YTDLP_COOKIES = os.getenv("YTDLP_COOKIES") or None

# Privileged intent, needed for prefix commands (!p, !s ...).
# Enable "Message Content Intent" in the Developer Portal when this is true.
MESSAGE_CONTENT = _bool("MESSAGE_CONTENT", True)

# Optional integrations
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

# Smooth audio (needs libopus, auto-loaded from PyAV). 0 = instant.
VOLUME_RAMP_MS = _int("VOLUME_RAMP_MS", 800)     # volume change ramp
# Loudness normalization: every track plays at about the same loudness.
# Default for servers that never ran /settings normalize.
NORMALIZE = _bool("NORMALIZE", True)
NORMALIZE_FILTER = os.getenv("NORMALIZE_FILTER", "loudnorm=I=-14:LRA=11:TP=-1.5")
FADE_MS = _int("FADE_MS", 400)                   # fade on pause/skip/stop/seek

# Playback stability
CROSSFADE_SECONDS = max(_int("CROSSFADE_SECONDS", 4), 0)  # songs overlap this long (0 = off)
PRELOAD_SECONDS = _int("PRELOAD_SECONDS", 12)    # gapless: start next FFmpeg this early
WATCHDOG_SECONDS = _int("WATCHDOG_SECONDS", 15)  # restart audio stuck this long

# Prefix commands (needs MESSAGE_CONTENT). Mentioning the bot also works.
PREFIX = os.getenv("PREFIX", "!")
OWNER_ID = _int("OWNER_ID", 0)
PLAYLIST_LIMIT = _int("PLAYLIST_LIMIT", 10)       # playlists per user
MUSIC_CARD = _bool("MUSIC_CARD", True)           # image card on now playing
CARD_REFRESH = _int("CARD_REFRESH", 20 if LOW_CPU else 10)  # seconds between card re-uploads
# Moving equalizer on the card (animated WebP): always | start (only a song's first card,
# so Discord's "GIF" label goes away) | off
CARD_ANIMATION = os.getenv("CARD_ANIMATION", "off" if not _bool("CARD_ANIMATED", True)
                           else "start" if LOW_CPU else "always")
CARD_ANIMATION = CARD_ANIMATION.strip().lower()
CARD_ANIMATION_SECONDS = 8  # "start": cards drawn in the first seconds of a song move
YTDLP_UPDATE_HOURS = _int("YTDLP_UPDATE_HOURS", 24)  # check for a new yt-dlp (0 = off)
NIGHT_MODE = _bool("NIGHT_MODE", True)            # darker card late at night (TIMEZONE)
NIGHT_HOURS = os.getenv("NIGHT_HOURS", "22-6")     # start-end hour, wraps past midnight
STICKY_PANEL = _int("STICKY_PANEL", 10)           # re-post the panel below after N chat messages (0 = off)
REQUEST_DELETE_DELAY = max(_int("REQUEST_DELETE_DELAY", 2), 1)  # request channel: seconds
LYRICS_ON_CARD = _bool("LYRICS_ON_CARD", True)   # live lyrics drawn on the card, not as text
LYRICS_PREFETCH = _bool("LYRICS_PREFETCH", True)  # look lyrics up when a song starts (🎤 state)
SESSION_SUMMARY = _bool("SESSION_SUMMARY", True)  # recap card when the session ends
HOT_THRESHOLD = _int("HOT_THRESHOLD", 5)         # plays in a server for the "hit" badge
TIMEZONE = os.getenv("TIMEZONE", "Asia/Bangkok")  # for birthday badges

# Auto-clean: command replies and bot messages delete themselves after this many
# seconds, and the "queue ended" panel is removed. Servers can change it with /settings autoclean
AUTO_CLEAN = _bool("AUTO_CLEAN", True)
AUTO_CLEAN_SECONDS = max(_int("AUTO_CLEAN_SECONDS", 20), 3)

# Message look: "groove" = every message is one Components V2 container (like the Groove
# bot), "classic" = embeds as before
UI_STYLE = os.getenv("UI_STYLE", "groove").strip().lower()

# Health endpoint (0 = disabled)
HEALTH_PORT = _int("HEALTH_PORT", 8080)

# Now-playing panel refresh interval in seconds
PANEL_REFRESH = _int("PANEL_REFRESH", 10)
# Faster refresh while the panel shows live (karaoke) lyrics
LYRICS_REFRESH = max(_int("LYRICS_REFRESH", 3), 2)


# ---------------------------------------------------------------- binaries
def _find_ffmpeg() -> str:
    """FFMPEG_PATH env, then system PATH, then the bundled imageio-ffmpeg binary."""
    path = os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return ""


def _ensure_deno():
    """yt-dlp needs a JS runtime for YouTube. Use the pip 'deno' package if none on PATH."""
    if shutil.which("deno"):
        return
    try:
        from deno import find_deno_bin
        os.environ["PATH"] = os.path.dirname(find_deno_bin()) + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        pass


FFMPEG = _find_ffmpeg()
_ensure_deno()
DENO = shutil.which("deno") or ""

# auto: pipe mode for the bundled imageio-ffmpeg (its HTTPS crashes), direct otherwise
STREAM_MODE = os.getenv("STREAM_MODE", "auto").lower()
if STREAM_MODE not in ("pipe", "direct"):
    STREAM_MODE = "pipe" if "imageio_ffmpeg" in FFMPEG else "direct"
