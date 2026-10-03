"""Discord's clock, as seen from this machine.

Timestamps such as <t:...:R> are counted down by each viewer's Discord app, so they must
be in real time. If the machine running the bot has a clock that is off (common on home
PCs and some hosts), every countdown would be off by the same amount. Each message Discord
sends back carries Discord's own time; the difference to our clock is the offset.
"""

import logging
import statistics
import time
from collections import deque
from typing import Optional

log = logging.getLogger("musicbot.clock")

MAX_SAMPLES = 15
WARN_AFTER = 5.0  # seconds of difference worth telling the owner about
_samples: deque = deque(maxlen=MAX_SAMPLES)
_offset = 0.0
_warned = False


def observe(message) -> None:
    """Learn from a message Discord just created or edited (its time is Discord's)."""
    global _offset, _warned
    stamp = getattr(message, "edited_at", None) or getattr(message, "created_at", None)
    if stamp is None:
        return
    try:
        diff = stamp.timestamp() - time.time()
    except Exception:
        return
    if abs(diff) > 86400:  # an old message, not a fresh reply
        return
    _samples.append(diff)
    _offset = statistics.median(_samples)
    if abs(_offset) >= WARN_AFTER and len(_samples) >= 3 and not _warned:
        _warned = True
        log.warning("This machine's clock is %.0f s %s Discord's. Countdowns are corrected, "
                    "but syncing the system clock (NTP) is recommended.",
                    abs(_offset), "behind" if _offset > 0 else "ahead of")


def offset() -> float:
    return _offset


def now() -> float:
    """Unix time by Discord's clock."""
    return time.time() + _offset


def reset(value: Optional[float] = None) -> None:
    """For tests."""
    global _offset, _warned
    _samples.clear()
    _offset, _warned = value or 0.0, False
