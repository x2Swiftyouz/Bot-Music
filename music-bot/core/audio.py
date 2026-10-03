"""Smooth volume: ramps gain frame by frame instead of jumping (no clicks, no gaps)."""

import array
import sys
import threading
import time
from typing import Callable, Optional

import discord

try:
    import audioop  # stdlib < 3.13, audioop-lts on 3.13+ (installed with discord.py)
except ImportError:  # pragma: no cover
    audioop = None

FRAME_MS = 20
LATE = 0.06  # seconds behind schedule before the sender stops trying to catch up


def keep_pace(source) -> None:
    """Stop discord.py from "catching up" after a late frame.

    Its sender keeps a fixed 20 ms schedule from when the song started. If a frame comes
    late (FFmpeg still starting, a busy moment), it sends the missed frames back to back,
    which plays as a short fast-forward. Moving the schedule to now turns that into a
    tiny pause instead. Called from read(), in the sender thread.
    """
    player = getattr(source, "_mb_player", None)
    if player is None:
        return
    try:
        due = player._start + player.DELAY * player.loops
        now = time.perf_counter()
        if now - due > LATE:
            player._start = now - player.DELAY * player.loops
            source.late_frames = getattr(source, "late_frames", 0) + 1
    except AttributeError:  # another discord.py version: leave its timing alone
        source._mb_player = None


RAMP_STEPS = 8  # a fade changes gain in 8 small steps per 20 ms frame (2.5 ms each)


def _scale_ramp(data: bytes, start: float, end: float) -> bytes:
    """Scale s16le stereo PCM with a gain ramp across the frame. With audioop the frame is
    scaled in short steps (fast, in C); without it, sample by sample in Python."""
    if audioop:
        step = len(data) // RAMP_STEPS // 4 * 4 or len(data)
        parts = []
        for k, i in enumerate(range(0, len(data), step)):
            g = start + (end - start) * (k + 0.5) / RAMP_STEPS
            parts.append(audioop.mul(data[i:i + step], 2, g))
        return b"".join(parts)
    samples = array.array("h")
    samples.frombytes(data)
    if sys.byteorder != "little":
        samples.byteswap()
    frames = len(samples) // 2
    if frames == 0:
        return data
    delta = (end - start) / frames
    g = start
    for i in range(frames):
        for j in (2 * i, 2 * i + 1):
            v = int(samples[j] * g)
            samples[j] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
        g += delta
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def _scale(data: bytes, gain: float) -> bytes:
    if gain == 1.0:
        return data
    if gain <= 0.0:
        return bytes(len(data))
    if audioop:
        return audioop.mul(data, 2, gain)
    return _scale_ramp(data, gain, gain)


class Silence(discord.AudioSource):
    """Placeholder left in a source whose audio was handed to the next song (crossfade)."""

    def read(self) -> bytes:
        return b""

    def is_opus(self) -> bool:
        return False


def _mix(a: bytes, b: bytes) -> bytes:
    """Add two s16le frames (clipped). The shorter one is padded with silence."""
    if len(a) < len(b):
        a += bytes(len(b) - len(a))
    elif len(b) < len(a):
        b += bytes(len(a) - len(b))
    if audioop:
        return audioop.add(a, b, 2)
    x, y = array.array("h"), array.array("h")
    x.frombytes(a)
    y.frombytes(b)
    return array.array("h", (max(-32768, min(32767, p + q)) for p, q in zip(x, y))).tobytes()


class SmoothVolume(discord.AudioSource):
    """Drop-in replacement for PCMVolumeTransformer with fades."""

    def __init__(self, original: discord.AudioSource, volume: float = 1.0,
                 start_gain: Optional[float] = None):
        if original.is_opus():
            raise discord.ClientException("SmoothVolume needs a PCM source")
        self.original = original
        self._lock = threading.Lock()
        self._target = max(0.0, min(volume, 2.0))
        self._gain = self._target if start_gain is None else start_gain
        self._step = 0.0
        self._on_done: Optional[Callable[[], None]] = None
        self.frames = 0  # read counter for the watchdog
        self._tail: Optional[list] = None  # [source, gain, step]: previous song fading out

    def crossfade_from(self, tail: discord.AudioSource, tail_gain: float, ms: int):
        """Start with the previous song's remaining audio mixed in, fading it out over
        ms while this one fades in. Call before playback starts."""
        frames = max(int(ms // FRAME_MS), 1)
        with self._lock:
            self._tail = [tail, tail_gain, tail_gain / frames]
            self._gain = 0.0
        self.fade_to(self._target, ms)

    @property
    def crossfading(self) -> bool:
        return self._tail is not None

    def _end_tail(self):
        tail, self._tail = self._tail, None
        if tail:
            try:
                tail[0].cleanup()
            except Exception:
                pass

    def _mix_tail(self, data: bytes) -> bytes:
        tail = self._tail
        if not tail:
            return data
        source, gain, step = tail
        old = source.read() if gain > 0 else b""
        if not old:
            self._end_tail()
            return data
        tail[1] = max(gain - step, 0.0)
        return _mix(data, _scale(old, (gain + tail[1]) / 2))

    # PCMVolumeTransformer compatible API
    @property
    def volume(self) -> float:
        return self._target

    @volume.setter
    def volume(self, value: float):
        self.fade_to(value, 0)

    def fade_to(self, target: float, ms: int, on_done: Optional[Callable[[], None]] = None):
        """Ramp to target gain over ms. on_done runs (audio thread) when reached."""
        with self._lock:
            self._target = max(0.0, min(target, 2.0))
            frames = int(ms // FRAME_MS)
            if frames <= 0:
                self._gain = self._target
                self._step = 0.0
            else:
                self._step = (self._target - self._gain) / frames
            self._on_done = on_done

    def is_opus(self) -> bool:
        return False

    def cleanup(self):
        self._end_tail()
        self.original.cleanup()

    def read(self) -> bytes:
        data = self.original.read()
        if not data:
            return data
        self.frames += 1
        callback = None
        with self._lock:
            start = self._gain
            if self._step:
                end = start + self._step
                if (self._step > 0 and end >= self._target) or (self._step < 0 and end <= self._target):
                    end = self._target
                    self._step = 0.0
                self._gain = end
            else:
                end = start
            if not self._step and self._on_done:
                callback, self._on_done = self._on_done, None
        out = _scale(data, end) if start == end else _scale_ramp(data, start, end)
        if self._tail:
            out = self._mix_tail(out)
        keep_pace(self)
        if callback:
            try:
                callback()
            except Exception:
                pass
        return out


class CountingSource(discord.AudioSource):
    """Pass-through for Opus sources that counts frames (watchdog)."""

    def __init__(self, original: discord.AudioSource):
        self.original = original
        self.frames = 0

    def read(self) -> bytes:
        data = self.original.read()
        if data:
            self.frames += 1
            keep_pace(self)
        return data

    def is_opus(self) -> bool:
        return self.original.is_opus()

    def cleanup(self):
        self.original.cleanup()
