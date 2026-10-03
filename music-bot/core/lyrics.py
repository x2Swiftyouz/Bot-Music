"""Lyrics from lrclib.net (free, no key). Plain and time-synced (LRC) lyrics."""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from core.sources import Track

log = logging.getLogger("musicbot.lyrics")

API = "https://lrclib.net/api/search"
HEADERS = {"User-Agent": "discord-music-bot/1.0 (+https://lrclib.net)"}

# Words in YouTube titles that are not part of the song name.
_NOISE = re.compile(
    r"\s*[\(\[【][^\)\]】]*(official|mv|m/v|video|audio|lyrics?|visuali[sz]er|live|"
    r"remaster|hd|4k|teaser|เนื้อเพลง|คาราโอเกะ|karaoke)[^\)\]】]*[\)\]】]",
    re.IGNORECASE)
_FEAT = re.compile(r"\s+(feat\.?|ft\.?|featuring)\s+.*$", re.IGNORECASE)
_LRC = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


@dataclass
class Lyrics:
    title: str
    artist: str
    plain: str = ""
    synced: list[tuple[float, str]] = field(default_factory=list)
    instrumental: bool = False

    def line_at(self, seconds: float) -> int:
        """Index of the synced line playing at this time (-1 before the first line)."""
        idx = -1
        for i, (t, _) in enumerate(self.synced):
            if t > seconds:
                break
            idx = i
        return idx


def clean_title(title: str) -> str:
    title = _NOISE.sub("", title)
    title = title.split("|")[0]
    title = _FEAT.sub("", title)
    return re.sub(r"\s{2,}", " ", title).strip(" -–—")


def split_artist(track: Track) -> tuple[str, str]:
    """(artist, song) from the artist field or an 'Artist - Song' title."""
    title = clean_title(track.title)
    artist = (track.artist or "").strip()
    for sep in (" - ", " – ", " — "):
        if sep in title:  # YouTube convention: "Artist - Song"; channel names often differ
            left, right = (x.strip() for x in title.split(sep, 1))
            if artist and right.lower() == artist.lower():
                return right, left
            return left, right
    return artist, title


def parse_lrc(text: str) -> list[tuple[float, str]]:
    out = []
    for line in (text or "").splitlines():
        stamps = _LRC.findall(line)
        words = _LRC.sub("", line).strip()
        for m, s in stamps:
            out.append((int(m) * 60 + float(s), words))
    out.sort(key=lambda x: x[0])
    return out


def _pick(results: list[dict], duration: Optional[int]) -> Optional[dict]:
    """Prefer results with lyrics, a matching length, then synced lyrics."""
    def score(r):
        has = bool(r.get("plainLyrics") or r.get("syncedLyrics") or r.get("instrumental"))
        diff = abs((r.get("duration") or 0) - duration) if duration and r.get("duration") else 30
        return (not has, diff > 8, not r.get("syncedLyrics"), diff)
    good = [r for r in results if r]
    return min(good, key=score) if good else None


def _to_lyrics(r: dict) -> Lyrics:
    synced = parse_lrc(r.get("syncedLyrics") or "")
    plain = r.get("plainLyrics") or "\n".join(t for _, t in synced)
    return Lyrics(title=r.get("trackName") or "", artist=r.get("artistName") or "",
                  plain=plain.strip(), synced=synced, instrumental=bool(r.get("instrumental")))


_cache: dict[str, Optional[Lyrics]] = {}
_session: Optional[aiohttp.ClientSession] = None


async def _search(params: dict) -> list[dict]:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), headers=HEADERS)
    async with _session.get(API, params=params) as r:
        if r.status != 200:
            return []
        data = await r.json()
        return data if isinstance(data, list) else []


def known(track: Track) -> bool:
    """True when find() has a definite answer for this track (found or not found)."""
    return track.url in _cache


async def find(track: Track) -> Optional[Lyrics]:
    """Lyrics for a track. Tries artist + title, then a free text search."""
    key = track.url
    if key in _cache:
        return _cache[key]
    artist, song = split_artist(track)
    attempts = []
    if artist:
        attempts.append({"track_name": song, "artist_name": artist})
    attempts.append({"q": f"{artist} {song}".strip()})
    if artist:
        attempts.append({"q": song})
    found = None
    try:
        for params in attempts:
            best = _pick(await _search(params), track.duration)
            if best and (best.get("plainLyrics") or best.get("syncedLyrics")
                         or best.get("instrumental")):
                found = _to_lyrics(best)
                break
    except Exception as exc:
        log.warning("lyrics lookup failed: %s", exc)
        return None  # network error: do not cache
    if len(_cache) > 200:
        _cache.clear()
    _cache[key] = found
    return found


async def search(query: str) -> Optional[Lyrics]:
    try:
        best = _pick(await _search({"q": query}), None)
    except Exception as exc:
        log.warning("lyrics search failed: %s", exc)
        return None
    return _to_lyrics(best) if best else None


async def close():
    if _session and not _session.closed:
        await _session.close()
