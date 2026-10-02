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


TOKEN = os.getenv("DISCORD_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "data/musicbot.db")

# Playback limits
IDLE_TIMEOUT = _int("IDLE_TIMEOUT", 300)          # seconds before leaving an empty queue
ALONE_TIMEOUT = _int("ALONE_TIMEOUT", 30)         # seconds before leaving an empty channel
MAX_QUEUE = _int("MAX_QUEUE", 500)
MAX_PER_USER = _int("MAX_PER_USER", 100)          # 0 = unlimited
MAX_DURATION = _int("MAX_DURATION", 0)            # seconds, 0 = unlimited
PLAY_COOLDOWN = _int("PLAY_COOLDOWN", 3)          # seconds between /play per user
HISTORY_SIZE = _int("HISTORY_SIZE", 20)
DEFAULT_VOLUME = _int("DEFAULT_VOLUME", 50)

# yt-dlp
AUTOCOMPLETE = _bool("AUTOCOMPLETE", True)       # live search while typing /play
YTDL_TIMEOUT = _int("YTDL_TIMEOUT", 45)       # seconds per extraction
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
PRELOAD_SECONDS = _int("PRELOAD_SECONDS", 12)    # gapless: start next FFmpeg this early
WATCHDOG_SECONDS = _int("WATCHDOG_SECONDS", 15)  # restart audio stuck this long

# Prefix commands (needs MESSAGE_CONTENT). Mentioning the bot also works.
PREFIX = os.getenv("PREFIX", "!")
OWNER_ID = _int("OWNER_ID", 0)
PLAYLIST_LIMIT = _int("PLAYLIST_LIMIT", 10)       # playlists per user
MUSIC_CARD = _bool("MUSIC_CARD", True)           # image card on now playing
CARD_REFRESH = _int("CARD_REFRESH", 20)          # seconds between card re-uploads while playing
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
