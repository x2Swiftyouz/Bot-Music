"""Notice when Discord rate-limits edits of a message, so the panel can slow down.

discord.py waits out a 429 by itself and only logs a warning. This reads those warnings
(logger "discord.http") and remembers which message was limited and when.
"""

import logging
import re
import time

_PATTERN = re.compile(r"/messages/(\d+) responded with 429")
_hits: dict[int, float] = {}


class _Watch(logging.Handler):
    def emit(self, record: logging.LogRecord):
        try:
            m = _PATTERN.search(record.getMessage())
        except Exception:
            return
        if m:
            _hits[int(m.group(1))] = time.monotonic()
            if len(_hits) > 500:
                _hits.clear()


def install():
    logger = logging.getLogger("discord.http")
    if not any(isinstance(h, _Watch) for h in logger.handlers):
        logger.addHandler(_Watch(level=logging.WARNING))


def limited_since(message_id: int, since: float) -> bool:
    """Was this message rate-limited at or after `since` (time.monotonic())?"""
    at = _hits.get(message_id)
    return at is not None and at >= since
