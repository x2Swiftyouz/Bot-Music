"""
HTTP reader that feeds audio bytes to FFmpeg through stdin (pipe mode).

Why: some FFmpeg builds (for example the static one from imageio-ffmpeg) crash
on HTTPS/DNS. Python does the networking here, FFmpeg only decodes.
It downloads in Range chunks (avoids YouTube throttling) and reconnects
from the last byte on errors.
"""

import io
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

log = logging.getLogger("musicbot.stream")

CHUNK = 4 * 1024 * 1024
READ_SIZE = 64 * 1024
MAX_RETRIES = 6


class HTTPStreamReader(io.RawIOBase):
    def __init__(self, url: str, headers: Optional[dict] = None):
        super().__init__()
        self.url = url
        self.headers = {k: v for k, v in (headers or {}).items() if k.lower() != "range"}
        self.headers.setdefault("User-Agent", "Mozilla/5.0")
        self.pos = 0
        self.total: Optional[int] = None
        self._resp = None
        self._chunk_end = -1
        self._closed_flag = threading.Event()

    def readable(self) -> bool:
        return True

    def _open(self):
        end = self.pos + CHUNK - 1
        if self.total is not None:
            end = min(end, self.total - 1)
        req = urllib.request.Request(
            self.url, headers={**self.headers, "Range": f"bytes={self.pos}-{end}"})
        resp = urllib.request.urlopen(req, timeout=20)
        if resp.status == 206:
            m = re.search(r"/(\d+)", resp.headers.get("Content-Range", ""))
            if m:
                self.total = int(m.group(1))
            self._chunk_end = end
        else:  # server ignored Range: whole body in one response
            length = resp.headers.get("Content-Length")
            if length:
                self.total = int(length)
            self._chunk_end = (self.total or 1 << 62) - 1
            skip = self.pos
            while skip > 0:
                data = resp.read(min(skip, READ_SIZE))
                if not data:
                    break
                skip -= len(data)
        self._resp = resp

    def _drop(self):
        if self._resp is not None:
            try:
                self._resp.close()
            except Exception:
                pass
        self._resp = None

    def read(self, size: int = -1) -> bytes:
        """Never raises. Returns b'' at end or on fatal error."""
        size = READ_SIZE if size is None or size <= 0 else size
        retries = 0
        while not self._closed_flag.is_set():
            if self.total is not None and self.pos >= self.total:
                return b""
            try:
                if self._resp is None:
                    self._open()
                data = self._resp.read(size)
            except urllib.error.HTTPError as exc:
                self._drop()
                if exc.code == 416:
                    return b""
                if exc.code in (401, 403, 404, 410):
                    log.warning("Stream HTTP %s, stop", exc.code)
                    return b""
                data = None
            except Exception as exc:
                self._drop()
                log.debug("Stream read error at %d: %s", self.pos, exc)
                data = None

            if data:
                self.pos += len(data)
                return data
            if data == b"":  # end of this range chunk
                self._drop()
                if self.total is None or self.pos >= self.total:
                    return b""
                continue
            retries += 1
            if retries > MAX_RETRIES:
                log.warning("Stream gave up after %d retries at byte %d", MAX_RETRIES, self.pos)
                return b""
            time.sleep(min(2 ** retries * 0.25, 5))
        return b""

    def close(self):
        self._closed_flag.set()
        self._drop()
        super().close()
