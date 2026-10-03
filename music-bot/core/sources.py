"""Track model and audio source resolution (yt-dlp, Spotify)."""

import asyncio
import base64
import functools
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Optional
from urllib.parse import parse_qs, urlparse

import aiohttp
import yt_dlp

import config

log = logging.getLogger("musicbot.sources")

class _YtdlLogger:
    """Send yt-dlp output to our log, so errors show in the console."""
    _log = logging.getLogger("musicbot.ytdlp")

    def debug(self, msg):
        if config.YTDL_DEBUG:
            self._log.info(msg)

    def info(self, msg):
        if config.YTDL_DEBUG:
            self._log.info(msg)

    def warning(self, msg):
        self._log.warning(msg)

    def error(self, msg):
        self._log.error(msg)


YTDL_BASE = {
    "logger": _YtdlLogger(),
    "verbose": config.YTDL_DEBUG,
    "format": ("bestaudio[acodec=opus][protocol^=http]/bestaudio[protocol^=http]/"
               "bestaudio/best"),
    "quiet": True,
    "no_warnings": False,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "retries": 5,
    "extractor_retries": 3,
    "socket_timeout": 15,
}
if config.YTDLP_COOKIES:
    YTDL_BASE["cookiefile"] = config.YTDLP_COOKIES

YTDL_FLAT = {**YTDL_BASE, "extract_flat": "in_playlist", "noplaylist": False}
YTDL_FULL = {**YTDL_BASE, "noplaylist": True}

# Direct stream URLs from YouTube live for hours. Re-resolve after this.
STREAM_TTL = 60 * 60

SOURCE_COLORS = {
    "youtube": 0xFF0000,
    "soundcloud": 0xFF5500,
    "spotify": 0x1DB954,
    "bandcamp": 0x629AA9,
    "twitch": 0x9146FF,
    "other": 0x5865F2,
}


def detect_source(url: str) -> str:
    host = urlparse(url).netloc.lower()
    for key in ("youtube", "youtu.be", "soundcloud", "spotify", "bandcamp", "twitch"):
        if key in host:
            return "youtube" if key == "youtu.be" else key
    if url.startswith("ytsearch"):
        return "youtube"
    return "other"


# Words that say what kind of upload it is, not what the song is called. A bracketed part
# (or a "| ..." tail) made only of these is dropped from displayed titles.
_TAG_WORDS = {
    "official", "offcial", "music", "video", "audio", "lyric", "lyrics", "visualizer",
    "visualiser", "mv", "m/v", "hd", "hq", "4k", "8k", "1080p", "720p", "full", "version",
    "ver", "m", "v", "clip", "teaser", "color", "coded", "sub", "subs", "thai", "eng", "with", "and",
    "เนื้อเพลง", "คาราโอเกะ", "ซับไทย",
}
_BRACKETS = re.compile(r"\s*[(\[【「『〔]([^()\[\]【】「」『』〔〕]*)[)\]】」』〕]")
_PIPE_TAIL = re.compile(r"\s*[|｜]\s*([^|｜]*)$")


def _only_tags(text: str) -> bool:
    words = [w for w in re.split(r"[\s,.&+/:-]+", text.casefold()) if w]
    return bool(words) and all(w in _TAG_WORDS for w in words)


# "(feat. X)", "[ft. X]", "(with X)" anywhere, or "ft. X" / "feat. X" at the end.
_FEAT_BRACKET = re.compile(r"\s*[(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+([^()\[\]]+?)\s*[)\]]",
                           re.I)
_FEAT_TAIL = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+((?:(?! - )[^()\[\]|])+?)\s*$", re.I)


def split_feat(title: str) -> tuple[str, str]:
    """'Song FT. A & B' -> ('Song', 'A & B'). No featured artists -> (title, '')."""
    m = _FEAT_BRACKET.search(title) or _FEAT_TAIL.search(title)
    if not m:
        return title, ""
    rest = (title[:m.start()] + title[m.end():]).strip()
    return (rest or title), m.group(1).strip()


def clean_title(title: str) -> str:
    """'Song (Official Music Video) [4K]' -> 'Song'. Keeps (Live), (Remix), (feat. X)..."""
    out = _BRACKETS.sub(lambda m: "" if _only_tags(m.group(1)) else m.group(0), title)
    while (tail := _PIPE_TAIL.search(out)) and _only_tags(tail.group(1)):
        out = out[:tail.start()]
    out = re.sub(r"\s{2,}", " ", out).strip(" -–—|｜")
    return out or title


@dataclass
class Track:
    title: str
    url: str                      # page URL or "ytsearch1:..." query
    duration: Optional[int] = None
    requester_id: int = 0
    requester_name: str = ""
    thumbnail: Optional[str] = None
    origin: str = "youtube"       # where the user found it (spotify, youtube, ...)
    artist: str = ""              # artist or channel name, shown under the title
    # runtime only, never saved
    _stream: Optional[str] = field(default=None, repr=False, compare=False)
    _stream_at: float = field(default=0.0, repr=False, compare=False)
    _headers: dict = field(default_factory=dict, repr=False, compare=False)
    _protocol: str = field(default="", repr=False, compare=False)
    _heatmap: tuple = field(default=(), repr=False, compare=False)   # YouTube "most replayed"
    _chapters: tuple = field(default=(), repr=False, compare=False)  # ((start, title), ...)
    _views: int = field(default=0, repr=False, compare=False)        # view count, 0 = unknown
    _year: str = field(default="", repr=False, compare=False)        # release / upload year

    RUNTIME = ("_stream", "_stream_at", "_headers", "_protocol", "_heatmap", "_chapters",
               "_views", "_year")

    def fmt_duration(self) -> str:
        return fmt_time(self.duration) if self.duration else "LIVE/?"

    @property
    def name(self) -> str:
        """Title for display, without "(Official Video)" and similar tags."""
        return clean_title(self.title)

    @property
    def video_id(self) -> Optional[str]:
        p = urlparse(self.url)
        if "youtu.be" in p.netloc:
            return p.path.lstrip("/") or None
        if "youtube" in p.netloc:
            return parse_qs(p.query).get("v", [None])[0]
        return None

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in self.RUNTIME:
            d.pop(k, None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Track":
        keys = {"title", "url", "duration", "requester_id", "requester_name",
                "thumbnail", "origin", "artist"}
        return cls(**{k: v for k, v in d.items() if k in keys})


def fmt_time(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    seconds = int(max(seconds, 0))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def parse_time(text: str) -> Optional[int]:
    """'90', '1:30', '1:02:03' to seconds."""
    try:
        parts = [int(p) for p in text.strip().split(":")]
    except ValueError:
        return None
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


def _extract(query: str, opts: dict) -> dict:
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(query, download=False)


# Separate thread pools: autocomplete spam must never block playback lookups.
PLAY_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ytdl-play")
SEARCH_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ytdl-search")
_search_inflight = 0


def _dec_inflight():
    global _search_inflight
    _search_inflight = max(_search_inflight - 1, 0)


def search_busy() -> bool:
    return _search_inflight >= 2


async def run_ytdl(query: str, opts: dict, pool: ThreadPoolExecutor = PLAY_POOL) -> dict:
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(pool, functools.partial(_extract, query, opts))
    try:
        return await asyncio.wait_for(fut, timeout=config.YTDL_TIMEOUT)
    except asyncio.TimeoutError:
        raise RuntimeError(f"yt-dlp timeout after {config.YTDL_TIMEOUT}s") from None


def _thumb(entry: dict) -> Optional[str]:
    if entry.get("thumbnail"):
        return entry["thumbnail"]
    thumbs = entry.get("thumbnails") or []
    return thumbs[-1]["url"] if thumbs else None


def _artist(e: dict) -> str:
    """Artist tag, else the channel name without YouTube suffixes."""
    name = e.get("artist") or e.get("creator") or e.get("uploader") or e.get("channel") or ""
    name = name.split(",")[0].strip() if e.get("artist") else name.strip()
    for suffix in (" - Topic", "VEVO", "Official"):
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)].strip()
    return name


def _entry_to_track(e: dict, requester_id: int, requester_name: str) -> Optional[Track]:
    vid = e.get("id")
    ie = (e.get("ie_key") or e.get("extractor_key") or "").lower()
    if vid and ie.startswith("youtube") and len(vid) == 11:
        url = f"https://www.youtube.com/watch?v={vid}"  # clean URL, no shorts/music variants
    else:
        url = e.get("webpage_url") or e.get("url")
    if not url:
        return None
    if not url.startswith("http"):
        url = f"https://www.youtube.com/watch?v={url}"
    thumb = _thumb(e)
    if not thumb and vid and "youtube" in url:
        thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    return Track(
        title=e.get("title") or url,
        url=url,
        duration=int(e["duration"]) if e.get("duration") else None,
        requester_id=requester_id,
        requester_name=requester_name,
        thumbnail=thumb,
        origin=detect_source(url),
        artist=_artist(e),
    )


async def search_tracks(query: str, requester_id: int, requester_name: str) -> list[Track]:
    """URL, playlist URL, Spotify URL or free text to a list of tracks."""
    query = query.strip()
    if spotify.is_spotify(query):
        return await spotify.resolve(query, requester_id, requester_name)
    if not query.startswith(("http://", "https://")):
        query = f"ytsearch1:{query}"
    info = await run_ytdl(query, YTDL_FLAT)
    entries = info.get("entries") if "entries" in info else [info]
    tracks = []
    for e in entries or []:
        if e:
            t = _entry_to_track(e, requester_id, requester_name)
            if t:
                tracks.append(t)
    return tracks


AUTOPLAY_NAME = "📻 Autoplay"


async def related_tracks(track: Track, limit: int = 10) -> list[Track]:
    """Songs like this one, for autoplay: YouTube's own "Mix" for the video, or a search
    by artist and title when there is no YouTube video to start from."""
    vid = track.video_id
    if vid:
        query = f"https://www.youtube.com/watch?v={vid}&list=RD{vid}"
    else:
        query = f"ytsearch{limit}:{track.artist} {clean_title(track.title)}".strip()
    info = await run_ytdl(query, {**YTDL_FLAT, "playlistend": limit + 1})
    out = []
    for e in info.get("entries") or []:
        t = _entry_to_track(e, 0, AUTOPLAY_NAME) if e else None
        if t and t.video_id != vid and t.url != track.url:
            out.append(t)
    return out[:limit]


async def search_choices(query: str, limit: int = 5, autocomplete: bool = False) -> list[Track]:
    """Search results for /search (play pool) or autocomplete (own small pool)."""
    global _search_inflight
    if not autocomplete:
        info = await run_ytdl(f"ytsearch{limit}:{query}", YTDL_FLAT)
    else:
        _search_inflight += 1
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(SEARCH_POOL, functools.partial(
            _extract, f"ytsearch{limit}:{query}", YTDL_FLAT))
        fut.add_done_callback(lambda _f: _dec_inflight())
        info = await asyncio.shield(fut)
    out = []
    for e in info.get("entries") or []:
        if e:
            t = _entry_to_track(e, 0, "")
            if t:
                out.append(t)
    return out


# Versions that are rarely what a Spotify link means, unless the Spotify title says so.
_WRONG_VERSION = ("live", "cover", "karaoke", "remix", "sped up", "slowed", "nightcore", "8d",
                  "instrumental", "reaction", "mashup", "acoustic", "piano", "tutorial",
                  "เวอร์ชั่น", "คาราโอเกะ", "คัฟเวอร์")


def _norm(text: str) -> str:
    return re.sub(r"[^\w]+", " ", (text or "").casefold()).strip()


def match_score(entry: dict, track: Track) -> float:
    """How well a YouTube search result matches a Spotify track (higher is better)."""
    want = _norm(track.title)
    title = _norm(entry.get("title"))
    channel = (entry.get("channel") or entry.get("uploader") or "").strip()
    score = 0.0
    dur = entry.get("duration")
    if track.duration and dur:
        diff = abs(dur - track.duration)
        score += 40 if diff <= 2 else 30 if diff <= 5 else 15 if diff <= 12 else \
            -10 if diff <= 30 else -60
    elif track.duration:
        score -= 5  # unknown length: no proof either way
    if channel.endswith(" - Topic"):
        score += 25  # YouTube Music upload of the studio recording
    if "official audio" in title or "audio" in title.split():
        score += 10
    elif "official" in title:
        score += 4
    artist = _norm((track.artist or "").split(",")[0])
    if artist and (artist in title or artist in _norm(channel)):
        score += 10
    song = _norm(track.title.split(" - ", 1)[1] if " - " in track.title else track.title)
    if song and song in title:
        score += 15
    for word in _WRONG_VERSION:
        if word in title.split() or (" " in word and word in title):
            if word not in want:
                score -= 30
    return score


async def _match_spotify(track: Track) -> Optional[str]:
    """Search 5 YouTube results and keep the one matching the Spotify track best."""
    query = track.url.split(":", 1)[1]
    try:
        info = await run_ytdl(f"ytsearch5:{query}", YTDL_FLAT)
    except Exception as exc:
        log.debug("spotify match search failed: %s", exc)
        return None
    entries = [e for e in info.get("entries") or [] if e and e.get("id")]
    if not entries:
        return None
    best = max(entries, key=lambda e: match_score(e, track))
    log.info("Spotify match for %r: %r (score %.0f)", track.title, best.get("title"),
             match_score(best, track))
    return f"https://www.youtube.com/watch?v={best['id']}"


async def resolve_stream(track: Track) -> str:
    """Return a fresh direct audio URL. Uses the prefetched one when still valid."""
    if track._stream and time.time() - track._stream_at < STREAM_TTL:
        return track._stream
    if track.origin == "spotify" and track.url.startswith("ytsearch"):
        track.url = await _match_spotify(track) or track.url
    info = await run_ytdl(track.url, YTDL_FULL)
    if "entries" in info:
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise RuntimeError("no result")
        info = entries[0]
    # Spotify / search tracks become real YouTube tracks here.
    if info.get("webpage_url"):
        track.url = info["webpage_url"]
    track.title = info.get("title") or track.title
    if info.get("duration"):
        track.duration = int(info["duration"])
    track.thumbnail = track.thumbnail or _thumb(info)
    track.artist = track.artist or _artist(info)
    track._stream = info["url"]
    track._stream_at = time.time()
    track._headers = dict(info.get("http_headers") or {})
    track._protocol = info.get("protocol") or ""
    track._heatmap = tuple(float(h.get("value") or 0) for h in info.get("heatmap") or ())
    track._chapters = tuple((float(c.get("start_time") or 0), str(c.get("title") or ""))
                            for c in info.get("chapters") or () if c.get("title"))
    track._views = int(info.get("view_count") or 0)
    year = info.get("release_year") or str(info.get("upload_date") or "")[:4]
    track._year = str(year) if str(year).isdigit() else ""

    return track._stream


# ---------------------------------------------------------------- Spotify ----

class SpotifyClient:
    """Reads Spotify metadata, then plays the matching song from YouTube."""

    URL_RE = re.compile(
        r"open\.spotify\.com/(?:intl-\w+/)?(track|album|playlist)/([A-Za-z0-9]+)")

    def __init__(self):
        self._token: Optional[str] = None
        self._token_exp = 0.0
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def enabled(self) -> bool:
        return bool(config.SPOTIFY_CLIENT_ID and config.SPOTIFY_CLIENT_SECRET)

    def is_spotify(self, query: str) -> bool:
        return "open.spotify.com" in query or query.startswith("spotify:")

    async def _session_get(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    async def _auth(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        creds = f"{config.SPOTIFY_CLIENT_ID}:{config.SPOTIFY_CLIENT_SECRET}"
        s = await self._session_get()
        async with s.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            headers={"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()},
        ) as r:
            r.raise_for_status()
            data = await r.json()
        self._token = data["access_token"]
        self._token_exp = time.time() + data.get("expires_in", 3600)
        return self._token

    async def _get(self, url: str) -> dict:
        s = await self._session_get()
        headers = {"Authorization": f"Bearer {await self._auth()}"}
        async with s.get(url, headers=headers) as r:
            r.raise_for_status()
            return await r.json()

    async def resolve(self, query: str, requester_id: int, requester_name: str) -> list[Track]:
        if not self.enabled:
            raise RuntimeError("Spotify not configured")
        if query.startswith("spotify:"):
            _, kind, sid = query.split(":")[:3]
        else:
            m = self.URL_RE.search(query)
            if not m:
                return []
            kind, sid = m.groups()

        raw: list[dict] = []
        if kind == "track":
            raw = [await self._get(f"https://api.spotify.com/v1/tracks/{sid}")]
        elif kind == "album":
            album = await self._get(f"https://api.spotify.com/v1/albums/{sid}")
            page = album["tracks"]
            while True:
                for t in page["items"]:
                    t.setdefault("album", {"images": album.get("images", [])})
                    raw.append(t)
                if not page.get("next") or len(raw) >= config.MAX_QUEUE:
                    break
                page = await self._get(page["next"])
        else:
            page = await self._get(
                f"https://api.spotify.com/v1/playlists/{sid}/tracks?limit=100")
            while True:
                raw.extend(i["track"] for i in page["items"] if i.get("track"))
                if not page.get("next") or len(raw) >= config.MAX_QUEUE:
                    break
                page = await self._get(page["next"])

        tracks = []
        for t in raw:
            if not t or not t.get("name"):
                continue
            artists = ", ".join(a["name"] for a in t.get("artists", []))
            images = (t.get("album") or {}).get("images") or []
            tracks.append(Track(
                title=f"{artists} - {t['name']}",
                url=f"ytsearch1:{artists} - {t['name']} audio",
                duration=(t.get("duration_ms") or 0) // 1000 or None,
                requester_id=requester_id,
                requester_name=requester_name,
                thumbnail=images[0]["url"] if images else None,
                origin="spotify",
                artist=artists,
            ))
        return tracks

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


spotify = SpotifyClient()
