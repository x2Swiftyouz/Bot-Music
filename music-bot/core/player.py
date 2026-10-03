"""Per-guild queue and playback engine."""

import asyncio
import ctypes.util
import dataclasses
import datetime
import io
import json
import logging
import os
import math
import random
import time
from collections import deque
from typing import TYPE_CHECKING, Optional

import discord

import config
from core import clock, ratelimit
from core.sources import Track, clean_artist, explain_error, fmt_time, resolve_stream
from core.audio import CountingSource, Prebuffer, Silence, SmoothVolume
from core.stream import HTTPStreamReader

if TYPE_CHECKING:
    from bot import MusicBot

log = logging.getLogger("musicbot.player")

FFMPEG_BEFORE = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 "
    "-reconnect_delay_max 5 -nostdin"
)



def _load_opus() -> bool:
    """libopus lets discord.py encode PCM (live volume). Without it FFmpeg encodes Opus."""
    if discord.opus.is_loaded():
        return True
    candidates = [ctypes.util.find_library("opus"), "libopus.so.0", "libopus.so",
                  "libopus-0.dll", "opus"]
    # PyAV wheels ship libopus: pip install av gives us one on bare hosting.
    try:
        import glob
        import importlib.util
        spec = importlib.util.find_spec("av")
        if spec and spec.origin:
            site = os.path.dirname(os.path.dirname(spec.origin))
            candidates += sorted(glob.glob(os.path.join(site, "av.libs", "libopus*")))
            candidates += sorted(glob.glob(os.path.join(site, "av", ".dylibs", "libopus*")))
    except Exception:
        pass
    for name in candidates:
        if not name:
            continue
        try:
            discord.opus.load_opus(name)
            return True
        except Exception:
            continue
    return False


OPUS_LOADED = _load_opus()

LOOP_MODES = ("off", "track", "queue")


def _safe_pipe_writer(self, source) -> None:
    """discord.py's pipe writer, but closing FFmpeg's stdin can never raise.

    On skip/stop discord.py kills FFmpeg and closes stdin from another thread while
    this thread may be closing it too. Python 3.14 then raises "ValueError:
    PyMemoryView_FromBuffer(): info->buf must not be NULL" from close(), which
    discord.py does not catch. The error is harmless but printed a traceback."""
    while self._process:
        data = source.read(self.BLOCKSIZE)
        if not data:
            try:
                if self._stdin is not None:
                    self._stdin.close()
            except Exception:
                pass  # already closed by the other side
            return
        try:
            if self._stdin is not None:
                self._stdin.write(data)
        except Exception:
            log.debug("pipe write ended for %s", self, exc_info=True)
            try:
                self._process.terminate()
            except Exception:
                pass
            return


class _PCMAudio(discord.FFmpegPCMAudio):
    _pipe_writer = _safe_pipe_writer


class _OpusAudio(discord.FFmpegOpusAudio):
    _pipe_writer = _safe_pipe_writer


def _now() -> datetime.datetime:
    """Now in TIMEZONE, by Discord's clock (see core.clock)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.fromtimestamp(clock.now(), ZoneInfo(config.TIMEZONE))
    except Exception:  # no tz database (Windows without tzdata)
        return datetime.datetime.fromtimestamp(clock.now())


def is_night(hour: Optional[int] = None) -> bool:
    """NIGHT_MODE between NIGHT_HOURS ("22-6" = 22:00 to 05:59, in TIMEZONE)."""
    if not config.NIGHT_MODE:
        return False
    try:
        start, end = (int(x) % 24 for x in config.NIGHT_HOURS.split("-"))
    except ValueError:
        return False
    h = _now().hour if hour is None else hour
    return start <= h < end if start < end else h >= start or h < end


def _today() -> datetime.date:
    return _now().date()


def clock_after(seconds: float) -> str:
    """Wall-clock time (TIMEZONE) this many seconds from now, e.g. '21:45'."""
    return (_now() + datetime.timedelta(seconds=max(seconds, 0))).strftime("%H:%M")


TIME_MODES = ("length", "remaining", "clock")
NO_PINGS = discord.AllowedMentions.none()
EDIT_GAP_START = 5.0   # seconds between timer edits after the first 429
EDIT_GAP_MAX = 20.0
EDIT_GAP_DECAY = 120   # seconds without a 429 before edits speed up by 1 s
STICKY_MIN_GAP = 20
STOP_FADE_MS = 900  # ⏹ and leaving fade out at least this long
SEEK_CROSSFADE_MS = 400  # a seek overlaps the song with itself: short, or it sounds doubled
QUEUE_RECAP_MIN = 2  # songs in a queue run before "queue ended" shows a recap card
# 🎛️ sound effects: key -> (name, emoji, FFmpeg filters, playback speed). Speed changes
# come from resampling (asetrate), so the pitch moves with it, like the real thing.
EFFECTS = {
    "off": ("ปกติ", "🎵", "", 1.0),
    "bass": ("Bass boost", "🔈", "bass=g=8:f=110:w=0.7,volume=-3dB", 1.0),
    "nightcore": ("Nightcore", "⚡", "aresample=48000,asetrate=60000,aresample=48000", 1.25),
    "slowed": ("Slowed + Reverb", "🌧️",
               "aresample=48000,asetrate=40800,aresample=48000,"
               "aecho=0.8:0.85:60|120:0.3|0.2", 0.85),
    "8d": ("8D", "🎧", "apulsator=hz=0.09", 1.0),
}

BITRATE_MIN, BITRATE_MAX = 64, 384  # kbps, sent to Discord

ENDING_SECONDS = 10  # the card shows the next song this long before the end
RESUME_MIN_SECONDS = 20 * 60  # clips this long continue where they were left next time
RESUME_FROM = 60              # ...when at least this far in
RESUME_END = 90               # ...and not within this much of the end (that counts as done)
QUEUE_LOW_SECONDS = 60  # nothing queued, no autoplay: the card warns this long before the end
HOT_PART = 0.85  # heatmap level (of the peak) that counts as the hit part (= card.HOT_LEVEL)


def _media_urls(components) -> list[str]:
    """Every media URL inside message components (galleries, thumbnails, files)."""
    out = []
    for c in components:
        media = getattr(c, "media", None)
        url = getattr(media, "url", None) or getattr(media, "proxy_url", None)
        if isinstance(url, str):
            out.append(url)
        for attr in ("children", "items", "components"):
            out += _media_urls(getattr(c, attr, None) or ())
        accessory = getattr(c, "accessory", None)
        if accessory is not None:
            out += _media_urls([accessory])
    return out

# Ears hear -10 dB as "half as loud", so the volume percent is turned into gain with
# gain = (percent/100) ** 1.66: 50% sounds half as loud as 100%, 25% a quarter.
# (Plain gain = percent/100 makes 75% sound almost like 100% and 50% barely softer.)
LOUDNESS_EXP = 1.66


def loudness_gain(volume: float) -> float:
    """Volume as people hear it (1.0 = full) -> audio gain."""
    return max(volume, 0.0) ** LOUDNESS_EXP

SHUFFLE_LOOKAHEAD = 8  # how far ahead smart shuffle looks for a song that does not clash


def _artist_key(t: Track) -> str:
    return clean_artist(t.artist or "").casefold().removesuffix(" - topic").strip()


def smart_shuffle(items: list, after: Optional[Track] = None) -> list:
    """Shuffle so one artist's songs spread out evenly (each artist's songs get evenly
    spaced slots with a random offset, Spotify-style), then walk the result and swap in a
    song from a little further on when the next one would follow the same artist or the
    same requester. Songs without an artist never clash on artist."""
    if len(items) < 2:
        return list(items)
    groups: dict = {}
    for t in items:
        key = _artist_key(t) or f"#{id(t)}"
        groups.setdefault(key, []).append(t)
    slots = []
    for songs in groups.values():
        random.shuffle(songs)
        n, start = len(songs), random.random()
        for i, t in enumerate(songs):
            jitter = random.uniform(-0.15, 0.15) if n > 1 else 0.0
            slots.append(((i + start + jitter) / n, random.random(), t))
    rest = [t for *_, t in sorted(slots, key=lambda x: x[:2])]

    def clash(a, b) -> int:
        if a is None:
            return 0
        same_artist = bool(_artist_key(a)) and _artist_key(a) == _artist_key(b)
        same_person = bool(a.requester_id) and a.requester_id == b.requester_id
        return 2 * same_artist + same_person

    out, prev = [], after
    while rest:
        window = rest[:SHUFFLE_LOOKAHEAD]
        best = min(range(len(window)), key=lambda i: (clash(prev, window[i]), i))
        prev = rest.pop(best)
        out.append(prev)
    return out


class GuildPlayer:
    def __init__(self, bot: "MusicBot", guild: discord.Guild, settings: dict):
        self.bot = bot
        self.guild = guild
        self.queue: deque[Track] = deque()
        self.history: deque[Track] = deque(maxlen=config.HISTORY_SIZE)
        self.current: Optional[Track] = None
        self.text_channel: Optional[discord.abc.Messageable] = None
        self.panel_message: Optional[discord.Message] = None

        self.volume = settings["volume"] / 100
        self.loop_mode = settings["loop_mode"] if settings["loop_mode"] in LOOP_MODES else "off"
        self.stay_247 = bool(settings["stay_247"])
        self.announce = bool(settings["announce"])
        self.vote_skip_enabled = bool(settings["vote_skip"])

        self.compact = bool(settings.get("compact"))
        # 0 = track length, 1 = time left, 2 = clock time it ends (column kept for old DBs)
        self.time_format = int(settings.get("time_remaining") or 0) % 3
        self.card_theme = settings.get("card_theme") or "blur"
        self.card_layout = settings.get("card_layout") or "wide"
        self.normalize = bool(settings.get("normalize"))
        self.effect = "off"  # 🎛️ EFFECTS key, for this session only
        self.auto_clean = bool(settings.get("auto_clean"))
        self.autoplay = bool(settings.get("autoplay"))  # 📻 similar songs when the queue ends
        self.fair_queue = bool(settings.get("fair_queue"))  # ⚖️ requesters take turns
        self.since_panel = 0     # chat messages posted below the panel (sticky panel)
        self._panel_sent_at = 0.0
        self._last_played: Optional[Track] = None
        self.request_channel_id = settings.get("request_channel") or 0
        self.request_message_id = settings.get("request_message") or 0
        self._header_card = False  # the request header shows a card (unknown at start: no)
        self.live_lyrics = False      # karaoke line on the panel
        self.lyrics = None            # Lyrics of the current track (live lyrics mode)
        self.lyrics_url: Optional[str] = None  # track the lyrics belong to (set when done)
        self._undo: deque[tuple[str, list[Track]]] = deque(maxlen=5)
        self._preload: Optional[tuple[Track, discord.AudioSource, str]] = None
        self._preload_task: Optional[asyncio.Task] = None
        self._prefetched: Optional[Track] = None  # queue head already looked up
        self._wd_frames = -1
        self._wd_since = 0.0
        self.has_card = False
        self.card_name = ""           # filename of the card attached to the panel
        self.loading = False          # panel shows the loading card while a track resolves
        self._card_key = None         # card state of the last uploaded image
        self._card_at = 0.0
        self._blink = False
        self._card_check = True  # see _card_present
        self.away_token = None   # empty room: see everyone_left / someone_back
        self.away_paused = False
        self.away_until = 0.0
        self._tail = None        # (pcm source, gain, http reader, ms): crossfade hand-over
        self._last_edit = 0.0    # timer edits pace themselves (see _pace)
        self._edit_gap = 0.0
        self._gap_since = 0.0
        self._panel_sig = None        # last panel edit, to skip edits that change nothing
        self.track_plays = 0          # plays of the current track in this server (hit badge)
        self.requester_birthday = False
        self.session: list[dict] = []  # finished tracks, for the recap card
        self.prev_volume: Optional[int] = None  # volume before the last change
        self._resolve_ema: Optional[float] = None  # typical lookup time (preload_lead)
        self._run_mark = 0       # session index where the current queue run started
        self._recap_all = False  # the last queue-end recap covered the whole session
        self._end_recap: list[dict] = []  # plays shown on the next "queue ended" panel
        self._panel_lock = asyncio.Lock()
        self.skip_votes: set[int] = set()
        self.destroyed = False

        # position tracking
        self._start_at = 0.0
        self._started = 0.0
        self._paused_at: Optional[float] = None
        self._paused_total = 0.0

        # control flags read after a track ends
        self._pending_start = 0.0
        self._restart_at: Optional[float] = None
        self._no_bookkeeping = False
        self._seeking = False    # the next start is a seek / replay, not a fresh play
        self._skipped = False

        self._source: Optional[discord.AudioSource] = None
        self._fail_streak = 0
        self._hold = False  # stop auto-advancing after repeated failures
        self._last_status: Optional[str] = "__unset__"
        self._next = asyncio.Event()
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._player_loop())
        self._soon: Optional[asyncio.Task] = None  # pending panel_soon() refresh
        self._panel_task = asyncio.create_task(self._panel_loop())
        self._save_task = asyncio.create_task(self._save_loop())
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    # ------------------------------------------------------------ helpers
    @property
    def vc(self) -> Optional[discord.VoiceClient]:
        return self.guild.voice_client  # type: ignore[return-value]

    @property
    def time_remaining(self) -> bool:
        return self.time_format == 1

    @property
    def is_paused(self) -> bool:
        return self._paused_at is not None

    @property
    def position(self) -> float:
        if not self.current or not self._started:
            return 0.0
        frames = getattr(self._source, "frames", None)
        if frames is not None:  # real audio progress: 20 ms per frame sent
            return self._start_at + frames * 0.02 * self.speed
        now = self._paused_at or time.monotonic()
        return self._start_at + (now - self._started - self._paused_total) * self.speed

    @property
    def speed(self) -> float:
        """Song seconds per real second (Nightcore plays faster, Slowed slower)."""
        return EFFECTS.get(self.effect, EFFECTS["off"])[3]

    def humans_in_channel(self) -> list[discord.Member]:
        if not self.vc or not self.vc.channel:
            return []
        return [m for m in getattr(self.vc.channel, "members", ()) if not m.bot]

    def is_admin(self, member: discord.Member) -> bool:
        """Server managers bypass vote skip and per-user limits."""
        return member.guild_permissions.manage_guild

    def eta(self, index: int) -> Optional[int]:
        """Seconds until queue[index] starts. None if unknown (live/unknown durations)."""
        total = 0.0
        if self.current:
            if not self.current.duration:
                return None
            total += max(self.current.duration - self.position, 0)
        for t in list(self.queue)[:index]:
            if not t.duration:
                return None
            total += t.duration
        return int(total / self.speed)

    def queue_seconds(self) -> int:
        """Length of the waiting songs (0 when one has no known length, e.g. a live)."""
        total = 0
        for t in self.queue:
            if not t.duration:
                return 0
            total += t.duration
        return int(total / self.speed)

    def total_remaining(self) -> Optional[int]:
        return self.eta(len(self.queue))

    def in_request_channel(self) -> bool:
        return bool(self.request_channel_id and self.text_channel
                    and getattr(self.text_channel, "id", None) == self.request_channel_id)

    async def send(self, content: str = None, **kwargs) -> Optional[discord.Message]:
        if not self.text_channel:
            return None
        if self.in_request_channel():
            kwargs.setdefault("delete_after", 30)  # keep the request channel clean
        elif self.auto_clean:
            kwargs.setdefault("delete_after", config.AUTO_CLEAN_SECONDS)
        try:
            return await self.text_channel.send(content, **kwargs)
        except discord.HTTPException:
            return None

    # -------------------------------------------------------------- queue
    def add(self, tracks: list[Track], front: bool = False) -> int:
        room = max(config.MAX_QUEUE - len(self.queue), 0)
        tracks = tracks[:room]
        if front:
            self.queue.extendleft(reversed(tracks))
        elif self.fair_queue:
            for t in tracks:
                self._insert_fair(t)
        else:
            self.queue.extend(tracks)
        self._wake.set()
        if tracks:
            asyncio.create_task(self.save_state())
            self.panel_soon()  # "next" on the card, the 📜 count
        return len(tracks)

    PANEL_SOON = 1.0  # seconds: songs added together share one panel edit

    def panel_soon(self):
        """Refresh the panel shortly (the queue changed). Not a timer tick: it is never
        skipped, so the new "next" song shows at once instead of on a later tick."""
        if self._soon and not self._soon.done():
            return

        async def run():
            await asyncio.sleep(self.PANEL_SOON)
            if not self.destroyed and self.current:
                await self.update_panel()
        self._soon = asyncio.create_task(run())

    def _insert_fair(self, track: Track):
        """Fair queue: a requester's n-th waiting song goes after everyone's n-th song, so
        people take turns instead of one long batch playing first."""
        seen: dict[int, int] = {}
        rounds = []
        for q in self.queue:
            r = seen.get(q.requester_id, 0)
            rounds.append(r)
            seen[q.requester_id] = r + 1
        mine = seen.get(track.requester_id, 0)
        pos = next((i for i, r in enumerate(rounds) if r > mine), len(self.queue))
        self.queue.insert(pos, track)

    def position_of(self, track: Track) -> int:
        """0-based place of this exact track object in the queue (-1 = not there)."""
        for i, q in enumerate(self.queue):
            if q is track:
                return i
        return -1

    def user_track_count(self, user_id: int) -> int:
        return sum(1 for t in self.queue if t.requester_id == user_id)

    def contains(self, url: str) -> bool:
        return (self.current and self.current.url == url) or any(t.url == url for t in self.queue)

    # ----------------------------------------------------------- controls
    def _smooth_source(self) -> Optional[SmoothVolume]:
        src = self.vc.source if self.vc else None
        return src if isinstance(src, SmoothVolume) else None

    def _stop_current(self) -> bool:
        """Stop the playing track. When something plays next (skip, previous, jump, seek),
        the two overlap: this one fades out while the next fades in (SKIP_CROSSFADE_MS).
        Otherwise a short fade-out."""
        vc = self.vc
        if not vc or not (vc.is_playing() or vc.is_paused()):
            return False
        src = self._smooth_source()
        if (src and vc.is_playing() and config.SKIP_CROSSFADE_MS > 0 and not self._tail
                and (self.queue or self._restart_at is not None)):
            # a seek overlaps the same song with itself: keep that one short
            ms = (min(config.SKIP_CROSSFADE_MS, SEEK_CROSSFADE_MS)
                  if self._restart_at is not None else config.SKIP_CROSSFADE_MS)
            self._hand_over(ms)
            return True
        if src and vc.is_playing() and config.FADE_MS > 0:
            loop = self.bot.loop

            def done():
                loop.call_soon_threadsafe(
                    lambda: self.vc and self.vc.source is src and self.vc.stop())
            src.fade_to(0, config.FADE_MS, on_done=done)
        else:
            vc.stop()
        return True

    def skip(self):
        if self.vc and (self.vc.is_playing() or self.vc.is_paused()):
            self._skipped = True
            self._stop_current()

    def vote_skip(self, member: discord.Member) -> str:
        if not self.current:
            return "ไม่มีเพลงเล่นอยู่"
        humans = self.humans_in_channel()
        if self.can_skip_now(member):
            self.skip()
            return f"⏭ ข้าม **{self.current.name}**"
        self.skip_votes.add(member.id)
        self.skip_votes &= {m.id for m in humans}
        need = self.skip_need()
        if len(self.skip_votes) >= need:
            self.skip()
            return f"⏭ โหวตครบ {need} เสียง ข้ามแล้ว"
        return f"🗳 โหวตข้าม {len(self.skip_votes)}/{need}"

    def can_skip_now(self, member: discord.Member) -> bool:
        """Skip without a vote: admins, the song's requester, or only two people here."""
        return (not self.vote_skip_enabled or self.is_admin(member)
                or bool(self.current and member.id == self.current.requester_id)
                or len(self.humans_in_channel()) <= 2)

    def skip_need(self) -> int:
        """Votes needed to skip: half the people in the voice channel."""
        return max(math.ceil(len(self.humans_in_channel()) / 2), 1)

    def toggle_pause(self) -> bool:
        """Return True when now paused."""
        self.away_paused = False  # a manual pause / resume overrides the automatic one
        vc = self.vc
        if not vc:
            return False
        src = self._smooth_source()
        fade = config.FADE_MS if src else 0
        if self._paused_at is not None:  # resume
            if src:
                if vc.is_paused():
                    src.fade_to(0, 0)
                src.fade_to(self.gain, fade)
            if vc.is_paused():
                vc.resume()
            self._paused_total += time.monotonic() - self._paused_at
            self._paused_at = None
            return False
        if vc.is_playing():  # pause
            self._paused_at = time.monotonic()
            if src and fade:
                loop = self.bot.loop

                def done():
                    loop.call_soon_threadsafe(
                        lambda: self._paused_at is not None and self.vc
                        and self.vc.source is src and self.vc.is_playing() and self.vc.pause())
                src.fade_to(0, fade, on_done=done)
            else:
                vc.pause()
            return True
        return False

    def restart_at(self, seconds: float) -> bool:
        """Seek: replay the current track from a position."""
        if not self.current or not self.vc:
            return False
        self._restart_at = max(seconds, 0.0)
        if self._paused_at is not None:  # seeking resumes playback
            self._paused_total += time.monotonic() - self._paused_at
            self._paused_at = None
        self._stop_current()
        return True

    def previous(self) -> bool:
        if not self.history:
            return False
        prev = self.history.pop()
        if self.current:
            self.queue.appendleft(self.current)
        self.queue.appendleft(prev)
        if self.vc and (self.vc.is_playing() or self.vc.is_paused()):
            self._no_bookkeeping = True
            self._stop_current()
        else:
            self._wake.set()
        return True

    def jump(self, index: int) -> Optional[Track]:
        if not 1 <= index <= len(self.queue):
            return None
        self.push_undo("jump")
        for _ in range(index - 1):
            t = self.queue.popleft()
            if self.loop_mode == "queue":
                self.queue.append(t)
        target = self.queue[0]
        self.skip()
        return target

    def push_undo(self, label: str):
        self._undo.append((label, list(self.queue)))

    def undo(self) -> Optional[str]:
        if not self._undo:
            return None
        label, items = self._undo.pop()
        self.queue = deque(items)
        self._drop_preload()
        self._wake.set()
        return label

    def remove_at(self, index: int) -> Track:
        self.push_undo("remove")
        t = self.queue[index - 1]
        del self.queue[index - 1]
        return t

    def move_track(self, a: int, b: int) -> Track:
        self.push_undo("move")
        t = self.queue[a - 1]
        del self.queue[a - 1]
        self.queue.insert(min(b, len(self.queue) + 1) - 1, t)
        return t

    def clear_queue(self) -> int:
        self.push_undo("clear")
        n = len(self.queue)
        self.queue.clear()
        return n

    def remove_tracks(self, tracks: list[Track]) -> int:
        ids = {id(t) for t in tracks}
        if not any(id(t) in ids for t in self.queue):
            return 0
        self.push_undo("remove")
        before = len(self.queue)
        self.queue = deque(t for t in self.queue if id(t) not in ids)
        return before - len(self.queue)

    def move_to_front(self, tracks: list[Track]) -> int:
        """Selected tracks become the next ones, in their queue order."""
        ids = {id(t) for t in tracks}
        picked = [t for t in self.queue if id(t) in ids]
        if not picked:
            return 0
        self.push_undo("move")
        self.queue = deque(picked + [t for t in self.queue if id(t) not in ids])
        return len(picked)

    def move_by(self, tracks: list[Track], step: int) -> int:
        """Move the selected tracks one place up (step -1) or down (+1). Selected tracks
        next to each other move together. Returns how many moved."""
        ids = {id(t) for t in tracks}
        q = list(self.queue)
        if not any(id(t) in ids for t in q):
            return 0
        order = range(len(q)) if step < 0 else range(len(q) - 1, -1, -1)
        moves = []
        for i in order:
            j = i + step
            if id(q[i]) in ids and 0 <= j < len(q) and id(q[j]) not in ids:
                moves.append((i, j))
                q[i], q[j] = q[j], q[i]
        if moves:
            self.push_undo("move")
            self.queue = deque(q)
        return len(moves)

    def dedupe(self) -> int:
        """Remove repeated songs (and the one playing now) from the queue."""
        seen = {self.current.url} if self.current else set()
        keep = []
        for t in self.queue:
            if t.url not in seen:
                seen.add(t.url)
                keep.append(t)
        removed = len(self.queue) - len(keep)
        if removed:
            self.push_undo("dedupe")
            self.queue = deque(keep)
        return removed

    def shuffle(self):
        """🔀 Smart shuffle: random, but songs by the same artist are spread through the
        queue, and neither the same artist nor the same requester's songs sit side by side
        (nor right after the song playing now) when the queue allows it."""
        self.push_undo("shuffle")
        self.queue = deque(smart_shuffle(list(self.queue), self.current))

    def cycle_loop(self) -> str:
        self.loop_mode = LOOP_MODES[(LOOP_MODES.index(self.loop_mode) + 1) % 3]
        return self.loop_mode

    def toggle_mute(self) -> bool:
        """🔇: silence without losing the volume; again brings it back. True = now muted."""
        if self.volume > 0:
            self._unmute_to = int(round(self.volume * 100))
            self.set_volume(0)
            return True
        self.set_volume(getattr(self, "_unmute_to", 0) or config.DEFAULT_VOLUME)
        return False

    async def _autoplay_next(self) -> bool:
        """Queue one song like the last one (not one played recently). True when queued."""
        seed = self._last_played
        if not seed:
            return False
        if await self._queue_next_episode(seed):
            return True
        from core.sources import related_tracks
        try:
            found = await related_tracks(seed)
        except Exception as exc:
            log.info("[%s] autoplay lookup failed: %s", self.guild.id, exc)
            return False
        recent = {t.url for t in self.history} | {seed.url}
        recent |= {r.get("url") for r in self.session[-50:]}
        for t in found:
            if t.url not in recent:
                me = getattr(getattr(self.guild, "me", None), "id", 0) or 0
                t.requester_id, t.requester_name = me, "📻 Autoplay"
                self.queue.append(t)
                return True
        return False

    async def _queue_next_episode(self, seed: Track) -> bool:
        """An episode of a show ('หลอนตามสั่ง EP.562'): autoplay looks for the next one of
        the same show before anything else."""
        from core.card import episode_matches, next_episode
        from core.sources import search_choices
        ep = next_episode(seed)
        if not ep:
            return False
        base, n = ep
        query = f"{base} EP.{n} {seed.artist or ''}".strip()
        try:
            found = await search_choices(query, 8)
        except Exception as exc:
            log.info("[%s] next episode lookup failed: %s", self.guild.id, exc)
            return False
        for t in found:
            if episode_matches(t, base, n) and t.url != seed.url:
                me = getattr(getattr(self.guild, "me", None), "id", 0) or 0
                t.requester_id, t.requester_name = me, "📻 Autoplay"
                self.queue.append(t)
                log.info("[%s] Autoplay: next episode %s", self.guild.id, t.title)
                return True
        return False

    def upcoming_episode(self) -> str:
        """What autoplay will play after this episode, for the card ('หลอนตามสั่ง EP.563')."""
        if not self.autoplay or self.queue or self.loop_mode != "off":
            return ""
        from core.card import next_episode
        ep = next_episode(self.current)
        return f"{ep[0]} EP.{ep[1]}" if ep else ""

    def toggle_autoplay(self) -> bool:
        self.autoplay = not self.autoplay
        if self.autoplay:
            self._wake.set()  # idle with an empty queue: start right away
        return self.autoplay

    # ------------------------------------------------------ away (empty room)
    def everyone_left(self) -> object:
        """Pause a playing song when the room empties (see cogs.music). Returns a token:
        the leave timer only acts if no newer leave or return happened meanwhile."""
        self.away_token = token = object()
        if config.AWAY_TIMEOUT and self.current and not self.is_paused and not self.loading:
            self.toggle_pause()
            self.away_paused = True
            self.away_until = clock.now() + config.AWAY_TIMEOUT
            asyncio.create_task(self.update_panel())
        return token

    async def someone_back(self, member):
        """Someone joined the bot's room: cancel the leave timer, resume if we paused."""
        self.away_token = None
        self.away_until = 0.0
        if self.away_paused:
            self.away_paused = False
            if self.is_paused:
                self.toggle_pause()
                from core.ui import who_label
                self.note(f"▶️ {who_label(member)} กลับมา เล่นต่อ")
        await self.update_panel()

    def arm_stop(self, seconds: float):
        """First ⏹ press while others listen: a second press within `seconds` stops."""
        self._stop_until = time.monotonic() + seconds

    def stop_armed(self) -> bool:
        return time.monotonic() < getattr(self, "_stop_until", 0)

    # ------------------------------------------------------------- notes
    NOTE_SECONDS = 12

    def note(self, text: str):
        """Who did what, shown briefly on the panel's status line."""
        self._note, self._note_until = text, time.monotonic() + self.NOTE_SECONDS

    def active_note(self) -> str:
        until = getattr(self, "_note_until", 0)
        if not until:
            return ""
        if time.monotonic() >= until:
            self._note_until = 0  # gone: the next refresh drops it
            return ""
        return self._note

    def _note_expired(self) -> bool:
        until = getattr(self, "_note_until", 0)
        return bool(until) and time.monotonic() >= until

    @property
    def gain(self) -> float:
        """Audio gain for the current volume (see loudness_gain)."""
        return loudness_gain(self.volume)

    def set_volume(self, percent: int):
        before = int(round(self.volume * 100))
        if before != max(0, min(percent, 150)):
            self.prev_volume = before  # "↩ back to …" in the volume menu
        self.volume = max(0, min(percent, 150)) / 100
        src = self._smooth_source()
        if src:
            if self._paused_at is None:
                src.fade_to(self.gain, config.VOLUME_RAMP_MS)  # smooth ramp
        elif self.current:
            self._drop_preload()
            self.restart_at(self.position)  # FFmpeg applies volume, restart in place

    # ------------------------------------------------------------- source
    def make_source(self, track: Track, start: float = 0.0) -> discord.AudioSource:
        """Build the FFmpeg source for a resolved track (track._stream must be set)."""
        is_http = track._stream.startswith("http") and "m3u8" not in track._protocol
        pipe = config.STREAM_MODE == "pipe" and is_http
        reader = HTTPStreamReader(track._stream, track._headers) if pipe else None

        if pipe:
            before = f"-ss {start:.2f}" if start > 0 else ""
        else:
            before = FFMPEG_BEFORE + (f" -ss {start:.2f}" if start > 0 else "")
        chain = []
        effect = EFFECTS.get(self.effect, EFFECTS["off"])[2]
        if effect:
            chain.append(effect)
        if self.normalize and config.NORMALIZE_FILTER:
            chain.append(config.NORMALIZE_FILTER)
        if start == 0:
            chain.append("afade=t=in:st=0:d=1.5")
        if not OPUS_LOADED:
            chain.append(f"volume={self.gain:.3f}")
        opts = "-vn -loglevel error"
        if chain:
            opts += f' -af "{",".join(chain)}"'

        exe = config.FFMPEG or "ffmpeg"
        src_arg = reader if pipe else track._stream
        try:
            if OPUS_LOADED:
                raw = _PCMAudio(src_arg, executable=exe, pipe=pipe,
                                before_options=before or None, options=opts)
                # Seek restarts fade in; track starts use FFmpeg afade instead.
                # read ahead in a thread: starts at once, rides out network hiccups
                src = SmoothVolume(Prebuffer(raw), volume=self.gain,
                                   start_gain=0.0 if start > 0 else None)
                if start > 0:
                    src.fade_to(self.gain, max(config.FADE_MS, 200))
            else:
                src = CountingSource(_OpusAudio(
                    src_arg, executable=exe, pipe=pipe, bitrate=self.voice_bitrate(),
                    before_options=before or None, options=opts))
        except Exception:
            if reader:
                reader.close()
            raise
        src._mb_reader = reader  # closed in the after-callback
        return src

    def audio_signature(self) -> str:
        return f"{OPUS_LOADED or self.gain}|{self.normalize}|{self.effect}"

    def set_normalize(self, enabled: bool):
        """Filters are baked into FFmpeg: restart the track in place to apply."""
        if enabled == self.normalize:
            return
        self.normalize = enabled
        self._drop_preload()
        if self.current and self.current.duration:
            self.restart_at(self.position)

    def voice_bitrate(self) -> int:
        """Opus bitrate (kbps) to send at: the voice channel's own setting, which a
        server's boost level allows up to 384. More than the channel takes is wasted,
        less throws away quality the listeners could have. VOICE_BITRATE pins it."""
        if config.VOICE_BITRATE:
            return max(16, min(config.VOICE_BITRATE, 512))
        channel = self.vc.channel if self.vc else None
        kbps = (getattr(channel, "bitrate", 0) or 0) // 1000
        return max(BITRATE_MIN, min(kbps or 128, BITRATE_MAX))

    def set_effect(self, key: str) -> bool:
        """🎛️ Switch the sound effect. Filters are baked into FFmpeg, so the song restarts
        in place (with the seek crossfade). False for an unknown key."""
        if key not in EFFECTS:
            return False
        if key == self.effect:
            return True
        pos = self.position
        self.effect = key
        self._drop_preload()
        if self.current and self.current.duration:
            self.restart_at(pos)
        return True

    @staticmethod
    def close_source(source: discord.AudioSource):
        for attr in ("_mb_reader", "_mb_tail_reader"):
            reader = getattr(source, attr, None)
            if reader:
                reader.close()

    # --------------------------------------------------------------- loop
    async def _player_loop(self):
        await self.bot.wait_until_ready()
        try:
            while not self.destroyed:
                await self._play_next()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Player loop crashed in guild %s", self.guild.id)
            await self.send("⚠️ ระบบเล่นเพลงมีปัญหา เริ่มใหม่อัตโนมัติ")
            # Restart the loop instead of dying.
            self._task = asyncio.create_task(self._player_loop())

    async def _play_next(self):
        self._next.clear()

        if not self.queue and not self._hold and self.autoplay and await self._autoplay_next():
            pass  # a similar song was queued: play it below
        if not self.queue or self._hold:
            if self._tail:  # the next song was removed during the hand-over
                self._drop_tail(self._tail)
                self._tail = None
            self.current = None
            self.loading = False
            start = min(self._run_mark, len(self.session))
            if len(self.session) > start:  # this run of the queue, for the idle panel
                self._end_recap = self.session[start:]
                self._recap_all = start == 0
                self._run_mark = len(self.session)
            await self._set_status(None)
            await self.update_panel()
            if self.stay_247:  # 24/7 never leaves, so recap when the queue runs out
                await self.send_summary()
            self._wake.clear()
            timeout = None if self.stay_247 else config.IDLE_TIMEOUT
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
                self._hold = False
            except asyncio.TimeoutError:
                await self.send("💤 ไม่มีเพลงในคิวนานเกินไป ออกจากห้องแล้ว")
                await self.destroy(clear_saved=not self._hold)
            return

        vc = await self._wait_voice()
        if not vc:
            await self.destroy(clear_saved=False)
            return

        track = self.queue.popleft()
        start, self._pending_start = self._pending_start, 0.0
        fresh, self._seeking = not self._seeking, False
        self.current = track
        self.skip_votes.clear()
        self._skipped = False

        log.info("[%s] Resolving %s", self.guild.id, track.url)
        t0 = time.monotonic()
        resolving = asyncio.ensure_future(resolve_stream(track))
        try:
            # Slow lookup: show a loading card instead of an old panel or nothing.
            await asyncio.wait_for(asyncio.shield(resolving), 1.0)
        except asyncio.TimeoutError:
            if start == 0 and (self.announce or self.in_request_channel()):
                await self.send_panel(loading=True)
        except Exception:
            pass  # reported below
        try:
            await resolving
        except Exception as exc:
            log.warning("Resolve failed %s: %s", track.url, exc)
            reason = str(exc).splitlines()[0][:180]
            await self._show_error(track, reason)
            self.current = None
            self._fail_streak += 1
            if self._fail_streak >= 5:
                await self.send("⛔ เล่นไม่ได้ติดกัน 5 เพลง หยุดระบบไว้ก่อน คิวยังอยู่ ใช้ `/play` เพื่อลองใหม่")
                self._fail_streak = 0
                self._hold = True
            return
        log.info("[%s] Resolved in %.1fs", self.guild.id, time.monotonic() - t0)
        self._note_resolve(time.monotonic() - t0)

        if config.MAX_DURATION and track.duration and track.duration > config.MAX_DURATION:
            await self.send(f"⛔ **{track.name}** ยาวเกิน {fmt_time(config.MAX_DURATION)} ข้าม")
            self.current = None
            return

        if fresh and start == 0:
            start = await self._resume_point(track)
        source = self._take_preload(track, start)
        if source is None:
            # a waiting crossfade tail stays: the new source reads ahead (Prebuffer), so
            # the old song keeps fading out while this one's FFmpeg starts
            try:
                source = self.make_source(track, start)
            except Exception as exc:
                await self._play_failed(track, exc)
                return
        self._attach_tail(source)

        def after(err: Optional[Exception], _source=source):
            if err:
                log.error("Playback error: %s", err)
            self.close_source(_source)
            self.bot.loop.call_soon_threadsafe(self._next.set)

        try:
            # music, at the voice channel's own quality (boosted servers go up to 384 kbps)
            vc.play(source, after=after, bitrate=self.voice_bitrate(), signal_type="music")
            source._mb_player = getattr(vc, "_player", None)  # see audio.keep_pace
        except Exception as exc:
            self.close_source(source)
            try:
                source.cleanup()
            except Exception:
                pass
            await self._play_failed(track, exc)
            return
        self._fail_streak = 0

        self._source = source
        log.info("[%s] Playing %s (start %.0fs, %d kbps)", self.guild.id, track.title, start,
                 self.voice_bitrate())
        self._start_at = start
        self._started = time.monotonic()
        self._paused_at = None
        self._paused_total = 0.0

        self._wd_frames, self._wd_since = -1, time.monotonic()
        await self._load_badges(track, count=start == 0)
        was_loading = self.loading and self.panel_message is not None
        self.loading = False
        if self.live_lyrics or config.LYRICS_PREFETCH:  # also sets the 🎤 / 🎙 buttons
            asyncio.create_task(self._fetch_lyrics(track))
        if start == 0 and not was_loading:
            if self.announce or self.in_request_channel():
                await self.send_panel()
        else:
            await self.update_panel()  # also turns the loading card into the real one
        await self._set_status(track)
        await self._update_presence()
        asyncio.create_task(self._prefetch())
        if self._preload_task is None or self._preload_task.done():
            self._preload_task = asyncio.create_task(self._preload_loop(track))

        await self._next.wait()
        await self._after_track(track)

    async def _play_failed(self, track: Track, exc: Exception):
        """Setup errors (FFmpeg/Opus/voice) are usually not the track's fault: keep it, hold."""
        log.error("Cannot start playback: %r", exc)
        self.queue.appendleft(track)
        self.current = None
        self._fail_streak += 1
        if self._fail_streak >= 3:
            self._fail_streak = 0
            self._hold = True
            await self.send(f"⛔ ระบบเสียงมีปัญหา หยุดไว้ก่อน คิวยังอยู่\n`{type(exc).__name__}: {exc}`"[:1900])
        else:
            await asyncio.sleep(2)

    def _log_audio(self, track: Track):
        """Late audio frames this song had: the bot was too busy to send sound on time."""
        src = self._source
        late = getattr(src, "late_frames", 0)
        if late:
            log.info("[%s] Audio: %d late frame(s) in %s, worst %.0f ms", self.guild.id, late,
                     track.title[:60], getattr(src, "late_worst", 0.0) * 1000)

    async def _after_track(self, track: Track):
        self._log_audio(track)
        if self._restart_at is not None:
            self._pending_start = self._restart_at
            self._restart_at = None
            self._seeking = True  # /replay must start from 0, not where it was left
            self.queue.appendleft(track)
            return
        if self._no_bookkeeping:
            self._no_bookkeeping = False
            return
        self._record(track)
        self.history.append(track)
        self._last_played = track  # autoplay looks for songs like this one
        if self.loop_mode == "track" and not self._skipped:
            self.queue.appendleft(track)
        elif self.loop_mode == "queue":
            self.queue.append(track)
        self.current = None

    async def _wait_voice(self) -> Optional[discord.VoiceClient]:
        for _ in range(15):
            vc = self.vc
            if vc and vc.is_connected():
                return vc
            await asyncio.sleep(1)
        return None

    # ------------------------------------------------- gapless preloading
    def _drop_preload(self):
        if self._preload:
            _, src, _ = self._preload
            self._preload = None
            self.close_source(src)
            try:
                src.cleanup()
            except Exception:
                pass

    def _take_preload(self, track: Track, start: float) -> Optional[discord.AudioSource]:
        """Use the FFmpeg process started early for this track, if still valid."""
        pre, self._preload = self._preload, None
        if not pre:
            return None
        ptrack, src, sig = pre
        if ptrack is track and start == 0 and sig == self.audio_signature():
            log.info("[%s] Gapless: using preloaded source", self.guild.id)
            return src
        self._preload = pre
        self._drop_preload()
        return None

    PRELOAD_MAX = 60  # seconds: never start the next song's FFmpeg earlier than this

    def _note_resolve(self, seconds: float):
        """Remember how long looking up a song takes here (average of recent ones)."""
        if seconds < 0.05:
            return  # cached: says nothing about the network
        ema = self._resolve_ema
        self._resolve_ema = seconds if ema is None else ema * 0.7 + seconds * 0.3

    def preload_lead(self) -> float:
        """How long before the end the next song is prepared. PRELOAD_SECONDS normally; more
        when lookups here are slow, so the hand-over (and its crossfade) is never late."""
        lead = float(config.PRELOAD_SECONDS)
        if self._resolve_ema:
            fade = config.CROSSFADE_SECONDS if self._can_crossfade() else 0
            lead = max(lead, 3 * self._resolve_ema + fade + 3)
        return min(lead, self.PRELOAD_MAX)

    async def _preload_loop(self, track: Track):
        """Near the end of a track, start the next track's FFmpeg so it begins instantly."""
        try:
            while self.current is track and not self.destroyed:
                await asyncio.sleep(1)
                # A song added (or moved to the top) after this track started has not been
                # looked up yet: do it now, not when this track ends (that left a 3s silence).
                if self.queue and self.queue[0] is not self._prefetched:
                    asyncio.create_task(self._prefetch())
                if (self._preload or not self.queue or self.is_paused
                        or self.loop_mode == "track" or not track.duration):
                    continue
                if (track.duration - self.position) / self.speed > self.preload_lead():
                    continue
                nxt = self.queue[0]
                try:
                    t0 = time.monotonic()
                    await resolve_stream(nxt)
                    src = self.make_source(nxt, 0)
                    self._note_resolve(time.monotonic() - t0)
                except Exception as exc:
                    log.debug("preload failed: %s", exc)
                    return
                if self.current is track and self.queue and self.queue[0] is nxt:
                    self._preload = (nxt, src, self.audio_signature())
                else:
                    self.close_source(src)
                    src.cleanup()
                    return
                if self._can_crossfade():
                    await self._crossfade_when_due(track)
                return
        except asyncio.CancelledError:
            pass

    # -------------------------------------------------------- crossfade
    def _can_crossfade(self) -> bool:
        """Crossfade mixes PCM, so it needs libopus (PCM path) and a SmoothVolume."""
        return bool(config.CROSSFADE_SECONDS and OPUS_LOADED)

    async def _crossfade_when_due(self, track: Track):
        """Wait until CROSSFADE_SECONDS before the end, then hand over (see _hand_over)."""
        while self.current is track and not self.destroyed and self._preload:
            left = (track.duration or 0) - self.position
            if left <= config.CROSSFADE_SECONDS:
                if (not self.is_paused and self.loop_mode != "track"
                        and self.queue and self._preload[0] is self.queue[0]):
                    self._hand_over()
                return
            await asyncio.sleep(min(max(left - config.CROSSFADE_SECONDS, 0.05), 1.0))

    def _hand_over(self, ms: Optional[int] = None):
        """End this song early, but keep its audio: the next song's source plays the rest
        of it, fading out over ms, while the next song fades in."""
        vc, src = self.vc, self._smooth_source()
        if not vc or not src or not vc.is_playing():
            return
        ms = config.CROSSFADE_SECONDS * 1000 if ms is None else ms
        reader = getattr(src, "_mb_reader", None)
        # the level it is playing at right now (it may be fading in after a seek)
        self._tail = (src.original, src.current_gain, reader, ms)
        src.original = Silence()   # stopping the old source must not kill its FFmpeg...
        src._mb_reader = None      # ...or close the stream feeding it
        log.info("[%s] Crossfade: %.1fs", self.guild.id, ms / 1000)
        vc.stop()

    def _attach_tail(self, source) -> None:
        """Give the waiting crossfade tail to the next song's source, or drop it."""
        tail, self._tail = self._tail, None
        if not tail:
            return
        pcm, gain, reader, ms = tail
        if isinstance(source, SmoothVolume) and not source.crossfading:
            source.crossfade_from(pcm, gain, ms)
            source._mb_tail_reader = reader
            return
        self._drop_tail(tail)

    @staticmethod
    def _drop_tail(tail):
        pcm, _, reader, _ = tail
        try:
            pcm.cleanup()
        except Exception:
            pass
        if reader:
            reader.close()

    # --------------------------------------------------------- watchdog
    async def _watchdog_loop(self):
        """Audio stuck (no frames read) for 15s: restart the track at its position."""
        while not self.destroyed:
            await asyncio.sleep(5)
            try:
                vc = self.vc
                if not self.current or not vc or self.is_paused or not vc.is_playing():
                    self._wd_frames, self._wd_since = -1, time.monotonic()
                    continue
                frames = getattr(vc.source, "frames", None)
                if frames is None:
                    continue
                now = time.monotonic()
                if frames != self._wd_frames:
                    self._wd_frames, self._wd_since = frames, now
                    continue
                if now - self._wd_since >= config.WATCHDOG_SECONDS:
                    log.warning("[%s] Watchdog: audio stuck, restarting at %.0fs",
                                self.guild.id, self.position)
                    await self.send("🔧 เสียงค้าง กำลังต่อใหม่จากจุดเดิม")
                    self._wd_frames, self._wd_since = -1, now
                    self._drop_preload()
                    # No fade here: a stuck source never finishes a fade.
                    self._restart_at = max(self.position, 0.0)
                    channel = vc.channel
                    connected = vc.is_connected()
                    vc.stop()
                    if not connected:
                        try:
                            await vc.disconnect(force=True)
                            await channel.connect(timeout=20, reconnect=True, self_deaf=True)
                        except Exception as exc:
                            log.error("Watchdog reconnect failed: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("watchdog error")

    async def _prefetch(self):
        if self.queue:
            nxt = self.queue[0]
            if nxt is self._prefetched:
                return  # already done or running (a failed lookup is not retried here)
            self._prefetched = nxt
            try:
                t0 = time.monotonic()
                await resolve_stream(nxt)
                self._note_resolve(time.monotonic() - t0)
            except Exception as exc:
                log.debug("Prefetch failed: %s", exc)
                return
            if config.MUSIC_CARD:
                # Pre-render the next card's heavy part (cover, blur, title) and fetch the
                # cover its "up next" row will show, so the next panel appears at once.
                from core.card import fetch_art, warm
                layout = "mini" if self.compact else self.card_layout
                await warm(nxt, self.card_theme, layout, self.avatar_url(nxt), is_night())
                if len(self.queue) > 1:
                    await fetch_art(self.queue[1].thumbnail)

    # ------------------------------------------------------ status & panel
    async def _set_status(self, track: Optional[Track]):
        status = f"🎵 {track.name}"[:450] if track else None
        if status == self._last_status or not self.vc or not self.vc.channel:
            return
        self._last_status = status
        try:
            await self.vc.channel.edit(status=status)
        except (discord.HTTPException, TypeError, AttributeError):
            pass

    async def _update_presence(self):
        try:
            if len(self.bot.players) == 1 and self.current:
                name = self.current.name[:120]
            else:
                name = f"/play | {len(self.bot.players)} servers"
            await self.bot.change_presence(
                activity=discord.Activity(type=discord.ActivityType.listening, name=name))
        except Exception:
            pass

    # ------------------------------------------------------------- cards
    def card_state(self, mode: str = "play", reason: str = ""):
        from core.card import CardState
        t = self.current
        end_clock = ""
        if self.time_format == 2 and t and t.duration:
            end_clock = clock_after((t.duration - self.position) / self.speed)
        nxt = self.queue[0] if self.queue else None
        return CardState(
            position=self.position, volume=int(round(self.volume * 100)), loop=self.loop_mode,
            queue_len=len(self.queue), paused=self.is_paused,
            time_mode=TIME_MODES[self.time_format], end_clock=end_clock,
            next_title=nxt.name if nxt else "", next_thumb=(nxt.thumbnail or "") if nxt else "",
            more_thumbs=tuple(q.thumbnail or "" for q in list(self.queue)[1:3]),
            queue_secs=self.queue_seconds(),
            hot_part=self.in_hot_part(), ending=self.in_ending(),
            hot=self.track_plays, birthday=self.requester_birthday, blink=self._blink,
            theme=self.card_theme, layout="mini" if self.compact else self.card_layout,
            mode=mode, reason=reason, avatar=self.avatar_url(t),
            animate=self._animate(mode), views=t._views if t else 0, year=t._year if t else "",
            night=is_night(), server_icon=self.server_icon_url(),
            listeners=self.listener_avatars(), listener_count=len(self.humans_in_channel()),
            queue_low=self.queue_low(), autoplay_next=self.upcoming_episode(),
            autoplay=bool(self.autoplay and not self.queue),
            effect=EFFECTS[self.effect][0] if self.effect != "off" else "",
            fx=self.effect if self.effect != "off" else "")

    def queue_low(self) -> bool:
        """The music is about to stop: under a minute left, nothing queued, nothing to
        repeat and autoplay off. The card's up-next row asks for a song."""
        t = self.current
        return bool(t and t.duration and not self.queue and self.loop_mode == "off"
                    and not self.autoplay
                    and t.duration - self.position <= QUEUE_LOW_SECONDS)

    def in_ending(self) -> bool:
        """Last ENDING_SECONDS of a song with another one queued: the card shows it."""
        t = self.current
        return bool(t and t.duration and self.queue and self.loop_mode != "track"
                    and t.duration - self.position <= ENDING_SECONDS)

    def in_hot_part(self) -> bool:
        """Is the song at its most replayed part (YouTube heatmap, top 15%)?"""
        t = self.current
        heat = tuple(t._heatmap or ()) if t else ()
        if not heat or not t.duration or max(heat) <= 0:
            return False
        i = min(int(self.position / t.duration * len(heat)), len(heat) - 1)
        return heat[i] >= HOT_PART * max(heat)

    def server_icon_url(self) -> str:
        """Small server icon for the card corner ("" = none or CARD_SERVER_ICON off)."""
        icon = getattr(self.guild, "icon", None)
        if not config.CARD_SERVER_ICON or icon is None:
            return ""
        try:
            return str(icon.with_size(64).url)
        except Exception:
            return ""

    def _animate(self, mode: str) -> bool:
        """Moving equalizer: always, never, or (default) only on a song's first card, so
        Discord's "GIF" label does not stay on the panel."""
        if mode != "play" or self.is_paused or config.CARD_ANIMATION == "off":
            return False
        if config.CARD_ANIMATION == "always":
            return True
        return self.position < config.CARD_ANIMATION_SECONDS

    LISTENER_FACES = 5

    def listener_avatars(self) -> tuple:
        """Avatar URLs of the first few people in the voice channel (for the card)."""
        out = []
        for m in self.humans_in_channel()[:self.LISTENER_FACES]:
            try:
                out.append(str(m.display_avatar.with_size(64).url))
            except Exception:
                pass
        return tuple(out)

    def avatar_url(self, track: Optional[Track]) -> str:
        """Small avatar of whoever requested the track ("" when unknown)."""
        if not track or not track.requester_id:
            return ""
        get = getattr(self.guild, "get_member", None)
        member = get(track.requester_id) if get else None
        if member is None:
            return ""
        try:
            return str(member.display_avatar.with_size(64).url)
        except Exception:
            return ""

    async def card_file(self, mode: str = "play", reason: str = "") -> Optional[discord.File]:
        """Render the current track's card (None when cards are off or rendering failed).
        The compact panel gets the slim mini card."""
        track = self.current  # the song can end while the card is drawn: keep this one
        if not config.MUSIC_CARD or not track:
            return None
        from core.card import alt_text, make_card, new_filename
        state = self.card_state(mode, reason)
        if self.compact and mode != "play":
            return None  # loading / error stay text on the compact panel
        data = await make_card(track, state)
        if not data or self.current is not track:
            return None  # finished meanwhile: the next panel draws its own card
        return discord.File(io.BytesIO(data), filename=new_filename(),
                            description=alt_text(track, state))

    async def _card_file(self, tick: bool = False, mode: str = "play",
                         reason: str = "") -> Optional[discord.File]:
        """Panel card. On timer ticks the image is re-uploaded only when something besides
        the progress changed, or every CARD_REFRESH seconds. None = keep the current image."""
        if not self.current:
            return None
        key = (self.current.url, dataclasses.replace(
            self.card_state(mode, reason), position=0, blink=False))
        now = time.monotonic()
        # 1 s slack: the timer ticks every PANEL_REFRESH (10 s), and a tick landing a hair
        # early must not push the next card to the tick after
        if tick and key == self._card_key and now - self._card_at < config.CARD_REFRESH - 1:
            return None
        if not self.current.duration:
            self._blink = not self._blink  # LIVE dot blinks between uploads
        card = await self.card_file(mode, reason)
        if card:
            self._card_key, self._card_at = key, now
            self.card_name = card.filename
        return card

    async def _load_badges(self, track: Track, count: bool):
        """Hit counter and requester birthday for the card badges."""
        self.track_plays, self.requester_birthday = 0, False
        try:
            db = self.bot.db
            if count:
                self.track_plays = await db.bump_play(self.guild.id, track.url)
            else:
                self.track_plays = await db.get_plays(self.guild.id, track.url)
            bday = await db.get_birthday(track.requester_id) if track.requester_id else None
            if bday:
                today = _today()
                self.requester_birthday = bday == (today.month, today.day)
        except Exception as exc:
            log.debug("badge lookup failed: %s", exc)

    async def _show_error(self, track: Track, reason: str):
        """Error card for a track that cannot play. Replaces the loading card if shown."""
        cause, fix = explain_error(reason)
        text = f"⚠️ เล่นไม่ได้ ข้าม: **{track.name}**\n{cause}"
        if fix:
            text += f"\n-# วิธีแก้: {fix}"
        card = await self.card_file("error", f"{cause}\n{fix}" if fix else cause)
        loading, self.loading = self.loading, False
        if loading and self.panel_message and not self._is_request_panel():
            async with self._panel_lock:
                try:
                    await self.panel_message.edit(content=text, embed=None, view=None,
                                                  attachments=[card] if card else [])
                except discord.HTTPException:
                    pass
                self.panel_message, self.has_card = None, False
        elif card:
            await self.send(text, file=card)
        else:
            await self.send(text)

    # ----------------------------------------------------- session recap
    async def _resume_point(self, track: Track) -> float:
        """A long clip left halfway last time starts there, with a note saying so."""
        if not track.duration or track.duration < RESUME_MIN_SECONDS:
            return 0.0
        try:
            at = await self.bot.db.get_resume(self.guild.id, track.url)
        except Exception as exc:
            log.debug("resume lookup failed: %s", exc)
            return 0.0
        if not RESUME_FROM <= at <= track.duration - RESUME_END:
            return 0.0
        log.info("[%s] Resuming %s at %.0fs", self.guild.id, track.title, at)
        self.note(f"▶ เล่นต่อจากเดิม {fmt_time(at)} · พิมพ์ /replay เพื่อเริ่มใหม่")
        return at

    def _remember_place(self, track: Track, played: float):
        """Long clips: keep where it stopped (skipped, stopped, the bot left), forget it once
        it was heard to the end."""
        if not track.duration or track.duration < RESUME_MIN_SECONDS:
            return
        db = getattr(self.bot, "db", None)
        if db is None:
            return
        if played >= track.duration - RESUME_END:
            job = db.clear_resume(self.guild.id, track.url)
        elif played >= RESUME_FROM:
            job = db.set_resume(self.guild.id, track.url, played)
        else:
            return

        async def run():
            try:
                await job
            except Exception as exc:
                log.debug("resume save failed: %s", exc)
        asyncio.ensure_future(run())

    def _record(self, track: Track, force: bool = False):
        if self.destroyed and not force:
            return  # destroy() already recorded the playing track
        played = self.position
        self._remember_place(track, played)
        if track.duration:
            played = min(played, track.duration)
        self.session.append({
            "title": track.title, "url": track.url, "thumbnail": track.thumbnail,
            "origin": track.origin, "requester_id": track.requester_id,
            "requester_name": track.requester_name, "seconds": max(played, 0.0)})
        if len(self.session) > 5000:
            del self.session[:1000]

    async def send_summary(self):
        plays, self.session = self.session, []
        mark, self._run_mark = self._run_mark, 0
        if self._recap_all and len(plays) <= mark:
            return  # the "queue ended" panel already showed this whole session
        if not config.SESSION_SUMMARY or not plays or sum(p["seconds"] for p in plays) < 30:
            return
        data = None
        if config.MUSIC_CARD:
            from core.card import EXT, make_summary
            data = await make_summary(plays)
        if data:
            await self.send(file=discord.File(io.BytesIO(data), filename=f"recap.{EXT}"),
                            **self._recap_keep())
        else:
            total = int(sum(p["seconds"] for p in plays))
            await self.send(f"📊 สรุปเซสชัน: {len(plays)} เพลง · ฟังรวม {fmt_time(total)}",
                            **self._recap_keep())

    def _recap_keep(self) -> dict:
        """The recap stays longer than other bot messages (10 min) when auto-clean is on."""
        return {"delete_after": 600} if self.auto_clean and not self.in_request_channel() else {}

    # ------------------------------------------------------------- panel
    def make_view(self) -> discord.ui.View:
        from core.ui import CompactPanelView, PanelView
        return CompactPanelView(self) if self.compact else PanelView(self)

    # ------------------------------------------------------ live lyrics
    def toggle_live_lyrics(self) -> bool:
        self.live_lyrics = not self.live_lyrics
        if self.live_lyrics and self.current and self.lyrics_url != self.current.url:
            asyncio.create_task(self._fetch_lyrics(self.current))
        return self.live_lyrics

    async def _fetch_lyrics(self, track: Track):
        from core import lyrics
        if self.lyrics_url == track.url:
            return
        found = await lyrics.find(track)
        if found is None and not lyrics.known(track):
            return  # network trouble: leave the buttons as they are, try again later
        if self.current is track:
            self.lyrics, self.lyrics_url = found, track.url
            await self.update_panel()

    def lyrics_state(self) -> str:
        """For the buttons: unknown | none | plain | synced (current track)."""
        if not self.current or self.lyrics_url != self.current.url:
            return "unknown"
        lyr = self.lyrics
        if lyr is None or (not lyr.plain and not lyr.synced and not lyr.instrumental):
            return "none"
        return "synced" if lyr.synced else "plain"

    # ------------------------------------------------------------- panel
    def _is_request_panel(self) -> bool:
        return bool(self.panel_message and self.request_message_id
                    and self.panel_message.id == self.request_message_id)

    async def _request_panel(self, embed, view, card) -> Optional[discord.Message]:
        """Request channel: the pinned header message is the panel, edited in place."""
        channel = self.text_channel
        if self.panel_message and not self._is_request_panel():
            try:
                await self.panel_message.delete()  # panel left in another channel
            except discord.HTTPException:
                pass
        if self.request_message_id and card is not None and not self._header_card:
            # The header has no picture now (idle). Discord's app often does not show a
            # picture that an edit adds to such a message until the channel is opened
            # again, so the first card goes into a fresh header instead.
            try:
                await channel.get_partial_message(self.request_message_id).delete()
            except discord.HTTPException:
                pass
            self.request_message_id = 0
        if self.request_message_id:
            msg = channel.get_partial_message(self.request_message_id)
            try:
                await msg.edit(content=None, embed=embed, view=view, allowed_mentions=NO_PINGS,
                               attachments=[card] if card else [])
                self._header_card = card is not None
                return msg
            except discord.NotFound:
                pass
        msg = await channel.send(embed=embed, view=view, allowed_mentions=NO_PINGS,
                                 **({"file": card} if card else {}))
        clock.observe(msg)
        self._header_card = card is not None
        self.request_message_id = msg.id
        await self.bot.db.set_setting(self.guild.id, "request_message", msg.id)
        return msg

    async def send_panel(self, loading: bool = False):
        # same lock as edits: a timer edit must not run between rendering this card and
        # sending it, or it would point the old panel at a file it does not have
        async with self._panel_lock:
            await self._send_panel(loading)

    def chat_message(self, message: discord.Message):
        """A message was posted in a channel. After STICKY_PANEL of them below the panel,
        post the panel again at the bottom so it never scrolls out of sight."""
        panel = self.panel_message
        if (not config.STICKY_PANEL or not panel or not self.current or self.loading
                or self._is_request_panel()):
            return
        channel = getattr(panel, "channel", None)
        if channel is None or message.channel.id != channel.id or message.id == panel.id:
            return
        self.since_panel += 1
        if (self.since_panel >= config.STICKY_PANEL and not self._panel_lock.locked()
                and time.monotonic() - self._panel_sent_at > STICKY_MIN_GAP):
            self.since_panel = 0
            asyncio.create_task(self.send_panel())

    async def _send_panel(self, loading: bool):
        from core.ui import build_now_playing
        self.since_panel = 0
        self._panel_sent_at = time.monotonic()
        self.loading = loading
        card = await self._card_file(mode="loading" if loading else "play")
        self.has_card = card is not None
        embed = build_now_playing(self)
        if self.in_request_channel():
            self._panel_sig = None
            try:
                self.panel_message = await self._request_panel(embed, self.make_view(), card)
            except discord.HTTPException as exc:
                log.debug("request panel failed: %s", exc)
            return
        try:
            if self.panel_message:
                try:
                    await self.panel_message.delete()
                except discord.HTTPException:
                    pass
            kwargs = {"file": card} if card else {}
            self._panel_sig = None
            # the panel itself is never auto-deleted while it is live
            self.panel_message = await self.send(embed=embed, view=self.make_view(),
                                                 delete_after=None, allowed_mentions=NO_PINGS,
                                                 **kwargs)
            clock.observe(self.panel_message)
        except discord.HTTPException as exc:
            log.debug("send_panel failed: %s", exc)
            self._card_key = None
            return
        if self.panel_message is not None and not self._card_present(self.panel_message):
            log.info("[%s] panel sent without its card, uploading it again", self.guild.id)
            self._card_key = None
            await self._edit_panel()

    async def update_panel(self, tick: bool = False):
        """Refresh the panel. tick=True (timer) skips if an edit is already running, or
        when Discord recently asked us to slow down (see _edit_gap)."""
        if not self.panel_message or (tick and self._panel_lock.locked()):
            return
        if tick and time.monotonic() - self._last_edit < self._edit_gap:
            return
        async with self._panel_lock:
            await self._edit_panel(tick)

    async def _edit_panel(self, tick: bool = False):
        if not self.panel_message:
            return
        from core.ui import build_idle_embed, build_now_playing
        try:
            if self.current:
                for attempt in range(2):
                    kwargs = {}
                    before = (self.card_name, self._card_key, self._card_at)
                    if self.has_card:
                        card = await self._card_file(tick and not attempt,
                                                     mode="loading" if self.loading else "play")
                        if card:
                            kwargs["attachments"] = [card]
                    embed, view = build_now_playing(self), self.make_view()
                    sig = (json.dumps(embed.to_dict(), sort_keys=True, ensure_ascii=False),
                           repr(view.to_components()))
                    if not kwargs and sig == self._panel_sig:
                        return  # nothing visible changed (e.g. paused or live)
                    if not self.panel_message:
                        return
                    try:
                        started = time.monotonic()
                        msg = await self.panel_message.edit(embed=embed, view=view,
                                                            allowed_mentions=NO_PINGS, **kwargs)
                        self._pace(started)
                    except Exception:
                        # the new card never arrived: keep pointing at the one that did
                        self.card_name, self._card_key, self._card_at = before
                        self._card_key = None
                        raise
                    clock.observe(msg)
                    self._panel_sig = sig
                    if self._card_present(msg):
                        break
                    if attempt:  # a fresh upload did not help: the check itself is wrong
                        self._card_check = False
                        log.warning("[%s] cannot verify the panel card, check turned off",
                                    self.guild.id)
                        break
                    log.info("[%s] panel card missing after edit, uploading it again",
                             self.guild.id)
                    self._card_key = None  # attempt 2 re-renders and re-uploads it
            else:
                self.has_card = False
                self._panel_sig = None
                if self._is_request_panel():
                    from core.ui import RequestIdleView, build_request_idle_embed
                    await self.panel_message.edit(embed=build_request_idle_embed(),
                                                  view=RequestIdleView(), attachments=[])
                    self._header_card = False
                else:
                    from core.ui import IdleView
                    recap, self._end_recap = self._end_recap, []
                    from core.ui import panel_color
                    embed = build_idle_embed(with_buttons=True, recap=recap,
                                             color=panel_color(self))
                    files = []
                    if len(recap) >= QUEUE_RECAP_MIN and config.MUSIC_CARD:
                        from core.card import make_summary, new_filename
                        data = await make_summary(recap, label="จบคิวแล้ว · QUEUE RECAP")
                        if data:
                            name = new_filename("queue-end")
                            embed.set_image(url=f"attachment://{name}")
                            files = [discord.File(io.BytesIO(data), filename=name,
                                                  description=f"สรุปคิว {len(recap)} เพลง")]
                    await self.panel_message.edit(embed=embed, view=IdleView(self),
                                                  attachments=files, allowed_mentions=NO_PINGS)
                    if self.auto_clean:  # "queue ended" goes away by itself
                        await self.panel_message.delete(delay=config.AUTO_CLEAN_SECONDS)
                self.panel_message = None
        except discord.NotFound:
            self.panel_message = None
        except discord.HTTPException as exc:
            log.debug("panel edit failed: %s", exc)
        except Exception:  # network trouble must not stop the panel timer
            log.warning("panel edit failed", exc_info=True)

    def _pace(self, started: float):
        """After an edit: if Discord rate-limited it, space timer edits further apart
        (up to EDIT_GAP_MAX); after a quiet while, speed back up step by step."""
        now = time.monotonic()
        self._last_edit = now
        mid = getattr(self.panel_message, "id", None)
        if mid is not None and ratelimit.limited_since(mid, started):
            self._edit_gap = min(max(self._edit_gap * 1.5, EDIT_GAP_START), EDIT_GAP_MAX)
            self._gap_since = now
            log.info("[%s] Discord is rate limiting the panel, edits now every %.0fs",
                     self.guild.id, self._edit_gap)
        elif self._edit_gap and now - self._gap_since > EDIT_GAP_DECAY:
            self._edit_gap = max(self._edit_gap - 1, 0.0)
            self._gap_since = now

    def _card_present(self, msg) -> bool:
        """Is the card the panel shows really on the message Discord sent back? A missing
        one shows as "image could not be loaded". Classic messages list it in attachments,
        Groove-style (Components V2) ones carry it as a media URL inside the components.
        When the message gives no image information at all, assume it is fine."""
        if not self.has_card or msg is None or not self._card_check:
            return True
        seen = [getattr(a, "filename", "") for a in getattr(msg, "attachments", None) or ()]
        seen += _media_urls(getattr(msg, "components", None) or ())
        if not seen:
            return True  # nothing to check against
        return any(self.card_name and self.card_name in s for s in seen)

    async def _panel_loop(self):
        while not self.destroyed:
            karaoke = (self.live_lyrics and self.lyrics and self.lyrics.synced
                       and self.current and self.lyrics_url == self.current.url)
            wait = config.LYRICS_REFRESH if karaoke else config.PANEL_REFRESH
            t = self.current
            if t and t.duration and not self.is_paused:
                # wake up right when the "up next" card (or the "almost over" one) is due
                lead = ENDING_SECONDS if self.queue else QUEUE_LOW_SECONDS
                until_end = (t.duration - self.position - lead) / self.speed
                if 0 < until_end < wait:
                    wait = until_end + 0.3
            await asyncio.sleep(wait)
            if self.current and self.panel_message and (not self.is_paused
                                                         or self._note_expired()):
                await self.update_panel(tick=True)

    # -------------------------------------------------------- persistence
    def snapshot(self) -> list[dict]:
        tracks = ([self.current] if self.current else []) + list(self.queue)
        return [t.to_dict() for t in tracks]

    async def save_state(self):
        if self.destroyed or not self.vc or not self.vc.channel:
            return
        data = self.snapshot()
        if not data:
            await self.bot.db.clear_queue(self.guild.id)
            return
        text_id = getattr(self.text_channel, "id", None)
        await self.bot.db.save_queue(self.guild.id, self.vc.channel.id, text_id, data,
                                     self.position if self.current else 0.0)

    async def _save_loop(self):
        while not self.destroyed:
            await asyncio.sleep(20)
            try:
                await self.save_state()
            except Exception:
                log.exception("save_state failed")

    # ------------------------------------------------------------ teardown
    async def destroy(self, clear_saved: bool = True):
        if self.destroyed:
            return
        self.destroyed = True
        me = asyncio.current_task()
        self._drop_preload()
        if self._tail:
            self._drop_tail(self._tail)
            self._tail = None
        for task in (self._panel_task, self._soon, self._save_task, self._watchdog_task,
                     self._preload_task):
            if task and task is not me and not task.done():
                task.cancel()
        self.queue.clear()
        if self.current:
            self._record(self.current, force=True)
        self.current = None
        await self._set_status(None)
        await self.update_panel()
        if not self.bot.shutting_down:
            await self.send_summary()
        src = self._smooth_source()
        if src and self.vc.is_playing() and config.FADE_MS > 0:
            fade = max(config.FADE_MS, STOP_FADE_MS)  # ⏹ / leaving: a gentle ending
            src.fade_to(0, fade)
            await asyncio.sleep(fade / 1000 + 0.1)
        if self.vc:
            try:
                await self.vc.disconnect(force=True)
            except Exception:
                pass
        self.bot.players.pop(self.guild.id, None)
        if clear_saved and not self.bot.shutting_down:
            await self.bot.db.clear_queue(self.guild.id)
        await self._update_presence()
        if self._task is not me and not self._task.done():
            self._task.cancel()
