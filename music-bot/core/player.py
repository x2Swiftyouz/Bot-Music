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
from core.sources import Track, fmt_time, resolve_stream
from core.audio import CountingSource, SmoothVolume
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


def _now() -> datetime.datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo(config.TIMEZONE))
    except Exception:  # no tz database (Windows without tzdata)
        return datetime.datetime.now()


def _today() -> datetime.date:
    return _now().date()


def clock_after(seconds: float) -> str:
    """Wall-clock time (TIMEZONE) this many seconds from now, e.g. '21:45'."""
    return (_now() + datetime.timedelta(seconds=max(seconds, 0))).strftime("%H:%M")


TIME_MODES = ("length", "remaining", "clock")

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
        self.request_channel_id = settings.get("request_channel") or 0
        self.request_message_id = settings.get("request_message") or 0
        self.live_lyrics = False      # karaoke line on the panel
        self.lyrics = None            # Lyrics of the current track (live lyrics mode)
        self.lyrics_url: Optional[str] = None  # track the lyrics belong to (set when done)
        self._undo: deque[tuple[str, list[Track]]] = deque(maxlen=5)
        self._preload: Optional[tuple[Track, discord.AudioSource, str]] = None
        self._preload_task: Optional[asyncio.Task] = None
        self._wd_frames = -1
        self._wd_since = 0.0
        self.has_card = False
        self.card_name = ""           # filename of the card attached to the panel
        self.loading = False          # panel shows the loading card while a track resolves
        self._card_key = None         # card state of the last uploaded image
        self._card_at = 0.0
        self._blink = False
        self._panel_sig = None        # last panel edit, to skip edits that change nothing
        self.track_plays = 0          # plays of the current track in this server (hit badge)
        self.requester_birthday = False
        self.session: list[dict] = []  # finished tracks, for the recap card
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
        self._skipped = False

        self._source: Optional[discord.AudioSource] = None
        self._fail_streak = 0
        self._hold = False  # stop auto-advancing after repeated failures
        self._last_status: Optional[str] = "__unset__"
        self._next = asyncio.Event()
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._player_loop())
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
            return self._start_at + frames * 0.02
        now = self._paused_at or time.monotonic()
        return self._start_at + (now - self._started - self._paused_total)

    def humans_in_channel(self) -> list[discord.Member]:
        if not self.vc or not self.vc.channel:
            return []
        return [m for m in self.vc.channel.members if not m.bot]

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
        return int(total)

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
        else:
            self.queue.extend(tracks)
        self._wake.set()
        if tracks:
            asyncio.create_task(self.save_state())
        return len(tracks)

    def user_track_count(self, user_id: int) -> int:
        return sum(1 for t in self.queue if t.requester_id == user_id)

    def contains(self, url: str) -> bool:
        return (self.current and self.current.url == url) or any(t.url == url for t in self.queue)

    # ----------------------------------------------------------- controls
    def _smooth_source(self) -> Optional[SmoothVolume]:
        src = self.vc.source if self.vc else None
        return src if isinstance(src, SmoothVolume) else None

    def _stop_current(self) -> bool:
        """Stop the playing track, with a short fade-out when possible."""
        vc = self.vc
        if not vc or not (vc.is_playing() or vc.is_paused()):
            return False
        src = self._smooth_source()
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
        if (not self.vote_skip_enabled or self.is_admin(member)
                or member.id == self.current.requester_id or len(humans) <= 2):
            self.skip()
            return f"⏭ ข้าม **{self.current.title}**"
        self.skip_votes.add(member.id)
        self.skip_votes &= {m.id for m in humans}
        need = math.ceil(len(humans) / 2)
        if len(self.skip_votes) >= need:
            self.skip()
            return f"⏭ โหวตครบ {need} เสียง ข้ามแล้ว"
        return f"🗳 โหวตข้าม {len(self.skip_votes)}/{need}"

    def toggle_pause(self) -> bool:
        """Return True when now paused."""
        vc = self.vc
        if not vc:
            return False
        src = self._smooth_source()
        fade = config.FADE_MS if src else 0
        if self._paused_at is not None:  # resume
            if src:
                if vc.is_paused():
                    src.fade_to(0, 0)
                src.fade_to(self.volume, fade)
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
        self.push_undo("shuffle")
        items = list(self.queue)
        random.shuffle(items)
        self.queue = deque(items)

    def cycle_loop(self) -> str:
        self.loop_mode = LOOP_MODES[(LOOP_MODES.index(self.loop_mode) + 1) % 3]
        return self.loop_mode

    def set_volume(self, percent: int):
        self.volume = max(0, min(percent, 150)) / 100
        src = self._smooth_source()
        if src:
            if self._paused_at is None:
                src.fade_to(self.volume, config.VOLUME_RAMP_MS)  # smooth ramp
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
        if self.normalize and config.NORMALIZE_FILTER:
            chain.append(config.NORMALIZE_FILTER)
        if start == 0:
            chain.append("afade=t=in:st=0:d=1.5")
        if not OPUS_LOADED:
            chain.append(f"volume={self.volume:.2f}")
        opts = "-vn -loglevel error"
        if chain:
            opts += f' -af "{",".join(chain)}"'

        exe = config.FFMPEG or "ffmpeg"
        src_arg = reader if pipe else track._stream
        try:
            if OPUS_LOADED:
                raw = discord.FFmpegPCMAudio(src_arg, executable=exe, pipe=pipe,
                                             before_options=before or None, options=opts)
                # Seek restarts fade in; track starts use FFmpeg afade instead.
                src = SmoothVolume(raw, volume=self.volume,
                                   start_gain=0.0 if start > 0 else None)
                if start > 0:
                    src.fade_to(self.volume, max(config.FADE_MS, 200))
            else:
                src = CountingSource(discord.FFmpegOpusAudio(
                    src_arg, executable=exe, pipe=pipe, bitrate=128,
                    before_options=before or None, options=opts))
        except Exception:
            if reader:
                reader.close()
            raise
        src._mb_reader = reader  # closed in the after-callback
        return src

    def audio_signature(self) -> str:
        return f"{OPUS_LOADED or self.volume}|{self.normalize}"

    def set_normalize(self, enabled: bool):
        """Filters are baked into FFmpeg: restart the track in place to apply."""
        if enabled == self.normalize:
            return
        self.normalize = enabled
        self._drop_preload()
        if self.current and self.current.duration:
            self.restart_at(self.position)

    @staticmethod
    def close_source(source: discord.AudioSource):
        reader = getattr(source, "_mb_reader", None)
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

        if not self.queue or self._hold:
            self.current = None
            self.loading = False
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

        if config.MAX_DURATION and track.duration and track.duration > config.MAX_DURATION:
            await self.send(f"⛔ **{track.title}** ยาวเกิน {fmt_time(config.MAX_DURATION)} ข้าม")
            self.current = None
            return

        source = self._take_preload(track, start)
        if source is None:
            try:
                source = self.make_source(track, start)
            except Exception as exc:
                await self._play_failed(track, exc)
                return

        def after(err: Optional[Exception], _source=source):
            if err:
                log.error("Playback error: %s", err)
            self.close_source(_source)
            self.bot.loop.call_soon_threadsafe(self._next.set)

        try:
            vc.play(source, after=after)
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
        log.info("[%s] Playing %s (start %.0fs)", self.guild.id, track.title, start)
        self._start_at = start
        self._started = time.monotonic()
        self._paused_at = None
        self._paused_total = 0.0

        self._wd_frames, self._wd_since = -1, time.monotonic()
        await self._load_badges(track, count=start == 0)
        was_loading = self.loading and self.panel_message is not None
        self.loading = False
        if self.live_lyrics:
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

    async def _after_track(self, track: Track):
        if self._restart_at is not None:
            self._pending_start = self._restart_at
            self._restart_at = None
            self.queue.appendleft(track)
            return
        if self._no_bookkeeping:
            self._no_bookkeeping = False
            return
        self._record(track)
        self.history.append(track)
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

    async def _preload_loop(self, track: Track):
        """Near the end of a track, start the next track's FFmpeg so it begins instantly."""
        try:
            while self.current is track and not self.destroyed:
                await asyncio.sleep(1)
                if (self._preload or not self.queue or self.is_paused
                        or self.loop_mode == "track" or not track.duration):
                    continue
                if track.duration - self.position > config.PRELOAD_SECONDS:
                    continue
                nxt = self.queue[0]
                try:
                    await resolve_stream(nxt)
                    src = self.make_source(nxt, 0)
                except Exception as exc:
                    log.debug("preload failed: %s", exc)
                    return
                if self.current is track and self.queue and self.queue[0] is nxt:
                    self._preload = (nxt, src, self.audio_signature())
                else:
                    self.close_source(src)
                    src.cleanup()
                return
        except asyncio.CancelledError:
            pass

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
            try:
                await resolve_stream(nxt)
            except Exception as exc:
                log.debug("Prefetch failed: %s", exc)
                return
            if config.MUSIC_CARD:
                # Pre-render the next card's heavy part (cover, blur, title) and fetch the
                # cover its "up next" row will show, so the next panel appears at once.
                from core.card import fetch_art, warm
                layout = "mini" if self.compact else self.card_layout
                await warm(nxt, self.card_theme, layout)
                if len(self.queue) > 1:
                    await fetch_art(self.queue[1].thumbnail)

    # ------------------------------------------------------ status & panel
    async def _set_status(self, track: Optional[Track]):
        status = f"🎵 {track.title}"[:450] if track else None
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
                name = self.current.title[:120]
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
            end_clock = clock_after(t.duration - self.position)
        nxt = self.queue[0] if self.queue else None
        return CardState(
            position=self.position, volume=int(round(self.volume * 100)), loop=self.loop_mode,
            queue_len=len(self.queue), paused=self.is_paused,
            time_mode=TIME_MODES[self.time_format], end_clock=end_clock,
            next_title=nxt.title if nxt else "", next_thumb=(nxt.thumbnail or "") if nxt else "",
            hot=self.track_plays, birthday=self.requester_birthday, blink=self._blink,
            theme=self.card_theme, layout="mini" if self.compact else self.card_layout,
            mode=mode, reason=reason)

    async def card_file(self, mode: str = "play", reason: str = "") -> Optional[discord.File]:
        """Render the current track's card (None when cards are off or rendering failed).
        The compact panel gets the slim mini card."""
        if not config.MUSIC_CARD or not self.current:
            return None
        from core.card import alt_text, make_card, new_filename
        state = self.card_state(mode, reason)
        if self.compact and mode != "play":
            return None  # loading / error stay text on the compact panel
        data = await make_card(self.current, state)
        if not data:
            return None
        return discord.File(io.BytesIO(data), filename=new_filename(),
                            description=alt_text(self.current, state))

    async def _card_file(self, tick: bool = False, mode: str = "play",
                         reason: str = "") -> Optional[discord.File]:
        """Panel card. On timer ticks the image is re-uploaded only when something besides
        the progress changed, or every CARD_REFRESH seconds. None = keep the current image."""
        if not self.current:
            return None
        key = (self.current.url, dataclasses.replace(
            self.card_state(mode, reason), position=0, blink=False))
        now = time.monotonic()
        if tick and key == self._card_key and now - self._card_at < config.CARD_REFRESH:
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
        text = f"⚠️ เล่นไม่ได้ ข้าม: **{track.title}**\n`{reason}`"
        card = await self.card_file("error", reason)
        loading, self.loading = self.loading, False
        if loading and self.panel_message and not self._is_request_panel():
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
    def _record(self, track: Track, force: bool = False):
        if self.destroyed and not force:
            return  # destroy() already recorded the playing track
        played = self.position
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
        if not config.SESSION_SUMMARY or not plays or sum(p["seconds"] for p in plays) < 30:
            return
        data = None
        if config.MUSIC_CARD:
            from core.card import EXT, make_summary
            data = await make_summary(plays)
        if data:
            await self.send(file=discord.File(io.BytesIO(data), filename=f"recap.{EXT}"))
        else:
            total = int(sum(p["seconds"] for p in plays))
            await self.send(f"📊 สรุปเซสชัน: {len(plays)} เพลง · ฟังรวม {fmt_time(total)}")

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
        if self.current is track:
            self.lyrics, self.lyrics_url = found, track.url
            await self.update_panel()

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
        if self.request_message_id:
            msg = channel.get_partial_message(self.request_message_id)
            try:
                await msg.edit(content=None, embed=embed, view=view,
                               attachments=[card] if card else [])
                return msg
            except discord.NotFound:
                pass
        msg = await channel.send(embed=embed, view=view, **({"file": card} if card else {}))
        self.request_message_id = msg.id
        await self.bot.db.set_setting(self.guild.id, "request_message", msg.id)
        return msg

    async def send_panel(self, loading: bool = False):
        from core.ui import build_now_playing
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
            self.panel_message = await self.send(embed=embed, view=self.make_view(), **kwargs)
        except discord.HTTPException as exc:
            log.debug("send_panel failed: %s", exc)

    async def update_panel(self, tick: bool = False):
        """Refresh the panel. tick=True (timer) skips if an edit is already running."""
        if not self.panel_message or (tick and self._panel_lock.locked()):
            return
        async with self._panel_lock:
            await self._edit_panel(tick)

    async def _edit_panel(self, tick: bool = False):
        if not self.panel_message:
            return
        from core.ui import build_idle_embed, build_now_playing
        try:
            if self.current:
                kwargs = {}
                if self.has_card:
                    card = await self._card_file(tick, mode="loading" if self.loading else "play")
                    if card:
                        kwargs["attachments"] = [card]
                embed, view = build_now_playing(self), self.make_view()
                sig = (json.dumps(embed.to_dict(), sort_keys=True, ensure_ascii=False),
                       repr(view.to_components()))
                if not kwargs and sig == self._panel_sig:
                    return  # nothing visible changed (e.g. paused or live)
                if self.panel_message:
                    await self.panel_message.edit(embed=embed, view=view, **kwargs)
                    self._panel_sig = sig
            else:
                self.has_card = False
                self._panel_sig = None
                if self._is_request_panel():
                    from core.ui import RequestIdleView, build_request_idle_embed
                    await self.panel_message.edit(embed=build_request_idle_embed(),
                                                  view=RequestIdleView(), attachments=[])
                else:
                    await self.panel_message.edit(embed=build_idle_embed(), view=None,
                                                  attachments=[])
                self.panel_message = None
        except discord.NotFound:
            self.panel_message = None
        except discord.HTTPException:
            self._card_key = None  # the upload may be lost: send a fresh card next time

    async def _panel_loop(self):
        while not self.destroyed:
            karaoke = (self.live_lyrics and self.lyrics and self.lyrics.synced
                       and self.current and self.lyrics_url == self.current.url)
            await asyncio.sleep(config.LYRICS_REFRESH if karaoke else config.PANEL_REFRESH)
            if self.current and self.panel_message and not self.is_paused:
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
        for task in (self._panel_task, self._save_task, self._watchdog_task,
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
            src.fade_to(0, config.FADE_MS)
            await asyncio.sleep(config.FADE_MS / 1000 + 0.1)
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
