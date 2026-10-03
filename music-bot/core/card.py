"""Image cards (Pillow): now playing (wide, square, mini), loading, error, share,
queue and session recap. Thai + Latin text with the bundled Kanit font."""

import asyncio
import colorsys
import dataclasses
import functools
import io
import itertools
import logging
import math
import multiprocessing
import os
import pickle
import random
import re
import time
import unicodedata
import zlib
from collections import Counter, OrderedDict
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from typing import Optional

import aiohttp
from PIL import (Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps,
                 ImageStat, features)

import config
from core.sources import (_BRACKETS, SOURCE_COLORS, Track, clean_artist, detect_source, fmt_time,
                          split_feat)

log = logging.getLogger("musicbot.card")

FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "fonts")
LAYOUT = ImageFont.Layout.RAQM if features.check("raqm") else ImageFont.Layout.BASIC
EXT = "webp" if features.check("webp") else "jpg"

THEMES = ("blur", "solid", "minimal", "polaroid", "cassette", "neon")
LAYOUTS = ("wide", "square")  # "mini" is used by the compact panel
HOT_THRESHOLD = config.HOT_THRESHOLD

SOURCE_NAMES = {"youtube": "YouTube", "soundcloud": "SoundCloud", "spotify": "Spotify",
                "bandcamp": "Bandcamp", "twitch": "Twitch"}
DEFAULT_ACCENT = (88, 101, 242)
WHITE = (255, 255, 255)
RED = (237, 66, 69)
ORANGE = (255, 152, 0)

# Thai: vowels written before their consonant, and vowels that must stay after it.
LEAD_VOWELS = "เแโใไ"
FOLLOW_VOWELS = "ะาำๅ"

_seq = itertools.count(1)


def new_filename(prefix: str = "nowplaying") -> str:
    """A fresh name per upload, so no client shows a cached older image."""
    return f"{prefix}-{next(_seq)}.{EXT}"


_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    key = (weight, size)
    if key not in _font_cache:
        path = os.path.join(FONT_DIR, f"Kanit-{weight}.ttf")
        try:
            _font_cache[key] = ImageFont.truetype(path, size, layout_engine=LAYOUT)
        except OSError:
            _font_cache[key] = ImageFont.load_default(size)
    return _font_cache[key]


# ------------------------------------------------------------ other scripts
# Kanit has Thai and Latin only. Other letters fall back, one by one, to Noto fonts: Lao,
# Khmer and Myanmar ship in assets/fonts. Chinese, Japanese and Korean fonts are big (4-9 MB
# each), so they are downloaded once into data/fonts the first time a title needs them.
FALLBACK_FONTS = ("NotoSansLao", "NotoSansKhmer", "NotoSansMyanmar")
CJK_DIR = os.path.join(os.path.dirname(config.DB_PATH) or ".", "fonts")
CJK_URL = ("https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/SubsetOTF/"
           "{region}/NotoSans{region}-{weight}.otf")
CJK_REGIONS = ("JP", "KR", "SC")
_JOINERS = "\u200c\u200d\ufe0e\ufe0f"


def _cjk_path(region: str, weight: str) -> str:
    # Medium text uses Regular: one weight less to download
    return os.path.join(CJK_DIR, f"NotoSans{region}-{'Bold' if weight == 'Bold' else 'Regular'}.otf")


def cjk_regions(text: str) -> list[str]:
    """CJK fonts this text needs, best first: kana = JP, hangul = KR, other Han = SC."""
    kana = hangul = han = punct = False
    for ch in text:
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF:
            kana = True
        elif 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
            hangul = True
        elif 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            han = True
        elif 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF:
            punct = True  # 【】「」 and full-width letters
    out = []
    if kana or (punct and not han and not hangul):
        out.append("JP")
    if hangul:
        out.append("KR")
    if han and not kana:
        out.append("SC")
    return out


def _simple(text: str) -> bool:
    """Only ASCII, Latin-1 and Thai: Kanit has all of it."""
    return all(c < "\u0250" or "\u0e00" <= c <= "\u0e7f" for c in text)


_probes: dict[str, tuple] = {}
_has: dict[tuple[str, str], bool] = {}


def _covers(path: str, ch: str) -> bool:
    """Does this font have a glyph for ch? (A missing one draws the same box as U+FFFF.)"""
    key = (path, ch)
    found = _has.get(key)
    if found is None:
        if path not in _probes:
            probe = ImageFont.truetype(path, 20, layout_engine=LAYOUT)
            box = probe.getmask("\uffff")
            _probes[path] = (probe, (box.size, bytes(box)))
        probe, box = _probes[path]
        mask = probe.getmask(ch)
        found = _has[key] = (mask.size, bytes(mask)) != box
    return found


_fallback_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _fallback_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    key = (path, size)
    if key not in _fallback_cache:
        _fallback_cache[key] = ImageFont.truetype(path, size, layout_engine=LAYOUT)
    return _fallback_cache[key]


def _chain(weight: str, text: str) -> list[str]:
    paths = [os.path.join(FONT_DIR, f"{name}-{weight}.ttf") for name in FALLBACK_FONTS]
    prefer = cjk_regions(text)
    paths += [_cjk_path(r, weight) for r in prefer + [r for r in CJK_REGIONS if r not in prefer]]
    return [p for p in paths if os.path.exists(p)]


def _runs(text: str, fnt) -> Optional[list[tuple[str, ImageFont.FreeTypeFont]]]:
    """Split text into (part, font) runs when Kanit lacks some of its letters; None when
    Kanit draws all of it. Symbols no font has (emoji) are left out rather than drawn
    as boxes; letters no font has yet stay (their font may still be downloading)."""
    if not text or _simple(text):
        return None
    path = getattr(fnt, "path", None)
    name = os.path.basename(path or "")
    if not name.startswith("Kanit-"):
        return None
    if all(_covers(path, c) for c in set(text) if c not in _JOINERS):
        return None
    chain = [path] + _chain(name[6:-4], text)
    runs: list[list] = []
    dropped = False
    for ch in text:
        cat = unicodedata.category(ch)
        cur = runs[-1][1] if runs else None
        if dropped and ch in _JOINERS:
            continue  # the ️ / ZWJ of an emoji that was left out
        dropped = False
        if cur and (cat in ("Mn", "Mc", "Me") or ch in _JOINERS):
            runs[-1][0] += ch  # marks stay with their letter
            continue
        if cur and cat[0] in "ZPSN" and _covers(cur, ch):
            runs[-1][0] += ch  # spaces and punctuation do not switch fonts
            continue
        use = next((p for p in chain if _covers(p, ch)), None)
        if use is None:
            if cat[0] == "S" or ch in _JOINERS:
                dropped = True
                continue  # emoji and symbols nobody has: leave out
            use = path
        if cur == use:
            runs[-1][0] += ch
        else:
            runs.append([ch, use])
    return [(t, fnt if p == path else _fallback_font(p, fnt.size))
            for t, p in ((t.rstrip() if i == len(runs) - 1 else t, p)
                         for i, (t, p) in enumerate(runs)) if t]


def _clusters(text: str) -> list[tuple[str, str]]:
    """'ດີ' -> [('ດ', 'ີ')]: each letter with the marks that sit on it."""
    out: list[list[str]] = []
    for ch in text:
        if out and unicodedata.category(ch) in ("Mn", "Me"):
            out[-1][1] += ch
        else:
            out.append([ch, ""])
    return [(b, m) for b, m in out]


def _places_marks(f) -> bool:
    """Without libraqm (Pillow's basic layout) a fallback font's vowel and tone marks are
    not put over their letter (Lao "ດີ" draws as "ດ ີ"): the bot places them itself.
    Kanit's Thai marks are made to overlap, so Kanit does not need this."""
    return (getattr(f, "layout_engine", None) == ImageFont.Layout.BASIC
            and not os.path.basename(getattr(f, "path", "") or "").startswith("Kanit-"))


class _TextDraw(ImageDraw.ImageDraw):
    """ImageDraw that draws letters Kanit lacks with a fallback font (see _runs)."""

    def _run_width(self, part: str, f) -> float:
        if not _places_marks(f):
            return super().textlength(part, f)
        return sum(super(_TextDraw, self).textlength(base, f) for base, _ in _clusters(part))

    def _draw_run(self, x: float, y: float, part: str, f, fill, *args, **kwargs):
        if not _places_marks(f):
            return super().text((x, y), part, fill, f, "ls", *args, **kwargs)
        for base, marks in _clusters(part):
            bw = super().textlength(base, f)
            super().text((x, y), base, fill, f, "ls", *args, **kwargs)
            bl, _, br, _ = f.getbbox(base, anchor="ls")
            centre = x + (bl + br) / 2 if br > bl else x + bw / 2
            for mark in marks:  # the mark's ink centred over the letter's ink
                ml, _, mr, _ = f.getbbox(mark, anchor="ls")
                super().text((centre - (ml + mr) / 2, y), mark, fill, f, "ls", *args, **kwargs)
            x += bw

    def textlength(self, text, font=None, *args, **kwargs):
        runs = _runs(text, font) if isinstance(text, str) else None
        if runs is None:
            return super().textlength(text, font, *args, **kwargs)
        return sum(self._run_width(t, f) for t, f in runs)

    def text(self, xy, text, fill=None, font=None, anchor=None, *args, **kwargs):
        runs = _runs(text, font) if isinstance(text, str) else None
        if runs is None:
            return super().text(xy, text, fill, font, anchor, *args, **kwargs)
        anchor = anchor or "la"
        widths = [self._run_width(t, f) for t, f in runs]
        total = sum(widths)
        x = xy[0] - {"m": total / 2, "r": total}.get(anchor[0], 0)
        ascent, descent = font.getmetrics()  # all runs share Kanit's baseline
        y = xy[1] + {"m": (ascent - descent) / 2, "s": 0, "d": -descent}.get(anchor[1], ascent)
        for (part, f), w in zip(runs, widths):
            self._draw_run(x, y, part, f, fill, *args, **kwargs)
            x += w


def _Draw(im: Image.Image, mode=None) -> _TextDraw:
    return _TextDraw(im, mode)


def _font_files() -> tuple:
    """Downloaded fonts so far: cached card parts are redrawn once a font arrives."""
    try:
        return tuple(sorted(os.listdir(CJK_DIR)))
    except OSError:
        return ()


_downloads: dict[str, "asyncio.Future"] = {}
FONT_WAIT = 15  # seconds a card waits for a font download (it goes on in the background)


async def _download_font(url: str, path: str):
    os.makedirs(CJK_DIR, exist_ok=True)
    tmp = path + ".part"
    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as r:
            r.raise_for_status()
            with open(tmp, "wb") as fh:
                async for chunk in r.content.iter_chunked(1 << 16):
                    fh.write(chunk)
    os.replace(tmp, path)  # the card process never sees half a file
    log.info("Downloaded font %s (%d KB)", os.path.basename(path), os.path.getsize(path) // 1024)


async def ensure_fonts(*texts: str):
    """Download the CJK fonts these texts need (once). Waits up to FONT_WAIT seconds."""
    if not config.CARD_FONT_DOWNLOAD:
        return
    jobs = []
    for region in cjk_regions(" ".join(t for t in texts if t)):
        for weight in ("Regular", "Bold"):
            path = _cjk_path(region, weight)
            if os.path.exists(path):
                continue
            job = _downloads.get(path)
            if job is None:
                url = CJK_URL.format(region=region, weight=weight)
                job = _downloads[path] = asyncio.ensure_future(_download_font(url, path))

                def done(f, path=path):
                    if f.cancelled() or f.exception():
                        log.warning("font download failed (%s): %s", os.path.basename(path),
                                    "cancelled" if f.cancelled() else f.exception())
                        _downloads.pop(path, None)  # try again next time
                job.add_done_callback(done)
            jobs.append(job)
    if jobs:
        await asyncio.wait([asyncio.shield(j) for j in jobs], timeout=FONT_WAIT)


# ------------------------------------------------------------------ layout
@dataclass(frozen=True)
class Geo:
    w: int
    h: int
    art: int
    art_x: int
    art_y: int
    x0: int          # text column
    max_w: int
    center: bool
    label_y: int
    title_y: int
    title_size: int
    chips_y: int
    next_y: int
    wave_y: int
    wave_h: int
    time_y: int
    s: float = 1.0       # size of the small text, chips and icons
    title_size2: int = 0  # wide: a title that needs two lines uses this size instead


# 3:1 like Groove's MusicCard (780x260), drawn larger so it stays sharp. Discord shows it
# at about 45%, so the small text is drawn 1.4x bigger than on the other layouts.
WIDE = Geo(w=1200, h=400, art=328, art_x=36, art_y=36, x0=404, max_w=756, center=False,
           label_y=26, title_y=64, title_size=44, chips_y=204, next_y=254,
           wave_y=310, wave_h=28, time_y=346, s=1.4, title_size2=34)
# Square flows below the text and is cropped to its content (see _flow).
SQUARE = Geo(w=640, h=820, art=300, art_x=170, art_y=36, x0=40, max_w=560, center=True,
             label_y=352, title_y=384, title_size=32, chips_y=0, next_y=0,
             wave_y=0, wave_h=40, time_y=0)
MINI = Geo(w=600, h=120, art=96, art_x=12, art_y=12, x0=124, max_w=460, center=False,
           label_y=0, title_y=12, title_size=22, chips_y=0, next_y=0,
           wave_y=72, wave_h=22, time_y=98)
GEOS = {"wide": WIDE, "square": SQUARE, "mini": MINI}


@dataclass(frozen=True)
class CardState:
    """Everything that changes the dynamic part of a card."""
    position: float = 0
    volume: int = 50
    loop: str = "off"
    queue_len: int = 0
    paused: bool = False
    time_mode: str = "length"   # length | remaining | clock
    end_clock: str = ""         # "21:45" when time_mode is clock
    next_title: str = ""
    next_thumb: str = ""
    more_thumbs: tuple = ()     # covers of the 2nd and 3rd songs in the queue
    hot_part: bool = False      # playing the most replayed part of the song right now
    ending: bool = False        # last seconds of the song: show what comes next, big
    hot: int = 0
    birthday: bool = False
    blink: bool = True
    theme: str = "blur"
    layout: str = "wide"
    mode: str = "play"          # play | share | loading | error
    reason: str = ""
    avatar: str = ""            # requester's avatar URL (drawn before "ขอโดย")
    animate: bool = False       # play mode: moving equalizer (animated WebP)
    night: bool = False         # late hours: a darker card
    server_icon: str = ""       # the server's icon URL, shown small in the top-right corner
    views: int = 0              # view count chip (0 = none)
    year: str = ""              # release year chip
    queue_secs: int = 0         # length of the songs waiting (0 = unknown, e.g. a live one)
    listeners: tuple = ()       # avatar URLs of people in the voice channel (a few)
    listener_count: int = 0     # everyone in the voice channel ("+N" for the rest)
    effect: str = ""            # 🎛️ sound effect name ("Nightcore"), "" = none
    fx: str = ""                # its key (bass | nightcore | slowed | 8d): the card's look
    queue_low: bool = False     # last minute, nothing queued, no autoplay: ask for a song
    autoplay_next: str = ""     # autoplay will play this next ('หลอนตามสั่ง EP.563')
    autoplay: bool = False      # autoplay is on: the empty 'up next' row says it will pick


# -------------------------------------------------------------------- text
def _safe_cut(text: str, cut: int) -> int:
    """Move a cut point left so Thai marks and vowels stay with their consonant."""
    while 1 < cut < len(text) and (unicodedata.category(text[cut]) == "Mn"
                                   or text[cut] in FOLLOW_VOWELS
                                   or text[cut - 1] in LEAD_VOWELS):
        cut -= 1
    return cut


def _fit(draw: ImageDraw.ImageDraw, text: str, fnt, max_w: float) -> str:
    if draw.textlength(text, font=fnt) <= max_w:
        return text
    cut = len(text)
    while cut > 1 and draw.textlength(text[:cut] + "…", font=fnt) > max_w:
        cut -= 1
    return text[:_safe_cut(text, cut)].rstrip() + "…"


def _wrap(draw: ImageDraw.ImageDraw, text: str, fnt, max_w: float, lines: int) -> list[str]:
    """Greedy wrap by characters (Thai has no spaces). Prefers spaces, never splits marks."""
    out = []
    rest = text.strip()
    while rest and len(out) < lines - 1:
        if draw.textlength(rest, font=fnt) <= max_w:
            break
        cut = 0
        for i in range(1, len(rest) + 1):
            if draw.textlength(rest[:i], font=fnt) > max_w:
                break
            cut = i
        cut = _safe_cut(rest, cut)
        space = rest.rfind(" ", 0, cut)
        if space > cut * 0.6:
            cut = space
        out.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        out.append(_fit(draw, rest, fnt, max_w))
    return out


TITLE_BIG = 56        # wide card: a short title is drawn this big
TITLE_BIG_FILL = 0.7  # ...when it takes at most this much of the line (a long one stays smaller)
TITLE_ROOM = 58  # px under a wide card's title: the artist line and a gap before the chips
TITLE_MIN = 24


def _fit_title(draw, text: str, size: int, max_w: float, lines: int = 2, size2: int = 0):
    """Largest of size, size-4, size-8 that fits without '…'. Returns (font, lines).
    size2: a title that does not fit on one line at `size` uses two lines from size2 down."""
    if size2:
        # a short title gets bigger letters instead of empty space (wide card)
        for s in (TITLE_BIG, TITLE_BIG - 6, size):
            fnt = font("Bold", s)
            if draw.textlength(text, font=fnt) <= max_w * (TITLE_BIG_FILL if s > size else 1):
                return fnt, [text]
        size = size2
    for s in (size, size - 4, size - 8):
        fnt = font("Bold", s)
        wrapped = _wrap(draw, text, fnt, max_w, lines)
        if not wrapped[-1].endswith("…"):
            return fnt, wrapped
    return fnt, wrapped


_NAME_STOP = {"official", "music", "video", "audio", "lyric", "lyrics", "remix", "cover",
              "live", "the", "and", "feat", "version", "mv", "ost", "channel", "records",
              "entertainment", "topic"}


def _name_tokens(text: str) -> set[str]:
    """Words of a name for matching: case, tone marks and punctuation ignored."""
    text = "".join(c for c in unicodedata.normalize("NFC", text.casefold())
                   if unicodedata.category(c) != "Mn")
    words = re.split(r"[\s\-–—|/\\,.&+()\[\]{}:;!?'\"“”‘’「」【】]+", text)
    return {w for w in words if len(w) >= 3 and w not in _NAME_STOP}


def _same_artist(part: str, artist: str) -> bool:
    """'จิมมี้ สิทธิพล ຈິມມີ້ ສິດທິພົນ' names 'JIMMY SITTHIPHON - จิมมี่ สิทธิพล'."""
    if not part or not artist:
        return False
    if artist.casefold() in part.casefold():
        return True
    return bool(_name_tokens(part) & _name_tokens(artist))


def _is_lao(word: str) -> bool:
    return any("\u0e80" <= c <= "\u0eff" for c in word)


def _one_script(text: str) -> str:
    """'ໃສວ່າຊັງເຂົາ ไสว่าซังเขา' (the same name in Lao and Thai): keep the Thai."""
    if not any("\u0e00" <= c <= "\u0e7f" for c in text) or not _is_lao(text):
        return text
    kept = " ".join(w for w in text.split() if not _is_lao(w))
    return re.sub(r"^[\s\-–—|/]+|[\s\-–—|/]+$", "", kept) or text


def _artist_name(artist: str) -> str:
    """'JIMMY SITTHIPHON - จิมมี่ สิทธิพล' (one name, two scripts): keep the Thai one."""
    for sep in (" - ", " – ", " | "):
        parts = [x.strip() for x in artist.split(sep)]
        if len(parts) == 2 and all(parts):
            thai = [x for x in parts if any("\u0e00" <= c <= "\u0e7f" for c in x)]
            latin = [x for x in parts if x.isascii()]
            if len(thai) == 1 and len(latin) == 1:
                return thai[0]
    return _one_script(artist)


# Bracketed words that name a version of the song: shown as a chip, not in the title
_VERSION = re.compile(
    r"\b(?:session|live|acoustic|unplugged|remix|cover|ver\.?|version|sped\s*up|slowed|"
    r"nightcore|instrumental|piano|demo|studio|concert|orchestra(?:l)?|edit|mix|rework|"
    r"reprise|remaster(?:ed)?|band|karaoke|english|japanese|korean|chinese)\b|"
    r"แสดงสด|อะคูสติก|คอนเสิร์ต|เวอร์ชัน|เวอร์ชั่น|รีมิกซ์|คัฟเวอร์", re.I)
SHOW_CHARS = 32    # a talk clip's segment name longer than this stays in the title
LABEL_CHARS = 22    # a longer label name is cut on its chip
VERSION_CHARS = 24  # longer bracketed text stays out of the chip


@dataclass
class SongParts:
    song: str
    artist: str
    feat: str = ""
    version: str = ""  # "Rock Session", "Live", "Acoustic Ver."
    label: str = ""    # the uploading channel when it is not the artist (a record label)
    series: str = ""   # the show an episode belongs to: 'หลอนตามสั่ง EP.562'


# "EP.562", "Ep 12", "ตอนที่ 5", "ตอน 5", "#123", "Episode 4"
_EPISODE = re.compile(r"(?i)\b(?:ep|episode)\.?\s*\d+|ตอนที่\s*\d+|\bตอน\s*\d+|(?<!\w)#\d+\b")


def episode_number(text: str) -> Optional[int]:
    m = _EPISODE.search(text or "")
    return int(re.search(r"\d+", m.group(0)).group(0)) if m else None


def next_episode(track: Optional[Track]) -> Optional[tuple[str, int]]:
    """('หลอนตามสั่ง', 563) for an episode 562 of a show, else None."""
    if track is None:
        return None
    m = _EPISODE.search(track.title or "")
    if not m:
        return None
    n = int(re.search(r"\d+", m.group(0)).group(0))
    series = song_parts(track).series
    if series:
        base = _EPISODE.sub("", series)
    else:  # no ' | ' parts: the words before the episode number name the show
        base = track.title[:m.start()]
        base = base.rsplit("|", 1)[-1]
    base = re.sub(r"\s{2,}", " ", base).strip(" -–—|:#.,")
    if not base:
        base = clean_artist(track.artist or "")
    return (base[:40], n + 1) if base else None


def episode_matches(track: Track, base: str, n: int) -> bool:
    """Is this search result episode n of the show called base?"""
    return (episode_number(track.title) == n
            and bool(_name_tokens(base) & _name_tokens(track.title or "")
                     or base.casefold() in (track.title or "").casefold()))


TALK_MIN_SECONDS = 15 * 60   # a talk clip is at least this long
LONG_CHIP_SECONDS = 20 * 60  # the card shows "⏱ 27 นาที" from this length
_TALK = re.compile(r"(?i)podcast|พอดแคสต์|เล่าเรื่อง|เรื่องเล่า|เรื่องผี|เรื่องหลอน|สัมภาษณ์|"
                   r"interview|\btalk\b|รายการ|ไลฟ์คุย|คุยกัน|นิทาน|audiobook|หนังสือเสียง")
_MUSIC = re.compile(r"(?i)official\s*(?:mv|music|audio|video)|\bmv\b|lyrics?|เนื้อเพลง|"
                    r"full\s*album|playlist|\bmix\b|รวมเพลง|เพลงฮิต|karaoke|คาราโอเกะ")


def is_talk(track: Optional[Track]) -> bool:
    """A long clip of people talking (a story, a podcast episode), not a song: by its length
    and its title (an episode number or talk words, and no music words)."""
    if track is None or not track.duration or track.duration < TALK_MIN_SECONDS:
        return False
    title = track.title or ""
    if _MUSIC.search(title):
        return False
    return bool(_TALK.search(title) or _HORROR.search(title)
                or episode_number(title) is not None)


def _pipe_parts(title: str, artist: str) -> tuple[str, str]:
    """'Story | หลอนตามสั่ง EP.562 | nuenglc' -> ('Story', 'หลอนตามสั่ง EP.562'): the
    channel's own name is dropped (it is on the artist line) and the show with its episode
    number goes to a chip. Titles without ' | ' come back as they are."""
    parts = [x.strip() for x in re.split(r"\s+[|｜]\s+", title) if x.strip()]
    if len(parts) < 2:
        return title, ""
    keep, series = [], ""
    for part in parts:
        if artist and _same_artist(part, artist) and len(part) <= len(artist) + 12:
            continue  # "| nuenglc"
        if not series and episode_number(part) is not None and len(part) <= VERSION_CHARS:
            series = part
            continue
        keep.append(part)
    if not keep:  # every part was the show or the channel: keep the first as the title
        return parts[0], ("" if series == parts[0] else series)
    return " | ".join(keep), series


def _pull_versions(title: str) -> tuple[str, str]:
    """'Song [Rock Session] (Live)' -> ('Song', 'Rock Session'): version brackets out of the
    title, the first one kept for the chip."""
    found = []

    def take(m):
        inner = m.group(1).strip()
        if inner and _VERSION.search(inner) and not split_feat(f"x ({inner})")[1]:
            found.append(inner)
            return " "
        return m.group(0)
    out = re.sub(r"\s{2,}", " ", _BRACKETS.sub(take, title)).strip(" -–—|")
    version = next((v for v in found if len(v) <= VERSION_CHARS), "")
    return (out or title), version


def _has_thai(text: str) -> bool:
    return any("\u0e00" <= c <= "\u0e7f" or "\u0e80" <= c <= "\u0eff" for c in text)


def song_parts(track: Track) -> SongParts:
    """Song, artists, featured artists, version and label for the card and the links.
    'HK & GH - Lost or Love FT. A & B' with channel 'HK' -> ('Lost or Love', 'HK & GH',
    'A & B'). 'Song - Artist' works too. Uploaded by a label ('หลงกล - LHAM (Rock Quest
    Project) [Rock Session]' from 'RS Music Thailand'): song 'หลงกล', artist 'LHAM',
    version 'Rock Session', label 'RS Music Thailand'."""
    title, feat = split_feat(track.name)
    title, version = _pull_versions(title)
    artist = clean_artist((track.artist or "").strip())
    title, series = _pipe_parts(title, artist)
    parts = _song_parts(title, artist, feat, version, talk=is_talk(track))
    parts.series = series or parts.series
    return parts


def _song_parts(title: str, artist: str, feat: str, version: str,
                talk: bool = False) -> SongParts:
    # "แต่งงานกันนะ-Rapper Tery": a bare "-" counts too, but only when one side names
    # the artist (so "Spider-Man" stays whole)
    for sep in (" - ", " – ", " — ", "-", "–"):
        left, found, right = title.partition(sep)
        left, right = left.strip(), right.strip()
        if not (found and left and right and artist):
            continue
        if left.lower().startswith(artist.lower()) or _same_artist(left, artist):
            left, left_feat = split_feat(left)
            return SongParts(_one_script(right), _artist_name(left), feat or left_feat, version)
        if _same_artist(right, artist):  # 'Song - Artist': the channel names the artist
            right, right_feat = split_feat(right)
            return SongParts(_one_script(left), _artist_name(artist), feat or right_feat,
                             version)
    # The channel names neither side: a label uploaded it. Thai titles go 'Song - Artist',
    # others 'Artist - Song'. Only a spaced dash, once, with short sides.
    for sep in (" - ", " – ", " — "):
        parts = [x.strip() for x in title.split(sep)]
        if len(parts) != 2 or not all(parts) or max(len(x) for x in parts) > 60:
            continue
        if _VERSION.search(parts[1]) and len(parts[1]) <= VERSION_CHARS:
            # 'Song - Remastered 2011', 'Song - Live at Wembley': a version, not an artist
            return SongParts(_one_script(parts[0]), _artist_name(artist), feat,
                             version or parts[1])
        if talk:
            # a story or a show, not a song: 'กฎ 5 ข้อ... - Rules of horror' from the
            # channel 'Shock Tonight' is the episode, then its segment. The channel
            # stays on the artist line and the segment goes to the show chip.
            show = parts[1] if len(parts[1]) <= SHOW_CHARS else ""
            return SongParts(_one_script(parts[0]), _artist_name(artist), feat, version,
                             series=show)
        song, who = parts if _has_thai(title) else parts[::-1]
        extra = [m.group(1).strip() for m in _BRACKETS.finditer(who)]
        who = re.sub(r"\s{2,}", " ", _BRACKETS.sub(" ", who)).strip(" -–—|") or who
        if not version:  # '(Rock Quest Project)' after the artist: the chip if short
            version = next((x for x in extra if x and len(x) <= VERSION_CHARS), "")
        who, who_feat = split_feat(who)
        label = artist if artist and not _same_artist(who, artist) else ""
        return SongParts(_one_script(song), _artist_name(who), feat or who_feat, version,
                         label)
    return SongParts(_one_script(title), _artist_name(artist), feat, version)


def _title_parts(track: Track) -> tuple[str, str, str]:
    """(song, artists, featured), see song_parts."""
    p = song_parts(track)
    return p.song, p.artist, p.feat


def display_title(track: Track) -> str:
    """The song name alone: the artist line shows who made it (see display_artist)."""
    return _title_parts(track)[0]


def display_artist(track: Track) -> str:
    """'HK & GH · ft. Forus & FREEFA'."""
    _, artist, feat = _title_parts(track)
    return " · ".join(x for x in (artist, f"ft. {feat}" if feat else "") if x)


def alt_text(track: Track, st: CardState) -> str:
    """Screen-reader description of a card (Discord attachment alt text)."""
    who = f" โดย {track.artist}" if track.artist else ""
    if st.mode == "error":
        return f"เล่นไม่ได้: {track.name}{who}. {st.reason.replace(chr(10), '. วิธีแก้: ')}"[:1000]
    if st.mode == "loading":
        return f"กำลังโหลด {track.name}{who}"[:1000]
    if not track.duration:
        timing = "ถ่ายทอดสด"
    elif st.mode == "share":
        timing = f"ความยาว {fmt_time(track.duration)}"
    else:
        timing = f"{fmt_time(st.position)} จาก {fmt_time(track.duration)}"
    state = "หยุดชั่วคราว" if st.paused else ("กำลังฟัง" if st.mode == "share" else "กำลังเล่น")
    text = f"{state} {track.name}{who}, {timing}, ขอโดย {track.requester_name or '-'}"
    if st.next_title and st.mode == "play":
        text += f". ถัดไป: {st.next_title}"
    return text[:1000]


# ------------------------------------------------------------------- color
def _mix(a, b, t: float) -> tuple[int, int, int]:
    return tuple(int(x + (y - x) * t) for x, y in zip(a, b))


def _lum(c) -> float:
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(v) for v in c[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b) -> float:
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _readable(fg, bg, ratio: float = 4.5) -> tuple[int, int, int]:
    """Lighten (or darken on a light background) until the text is readable."""
    target = WHITE if _lum(bg) < 0.4 else (0, 0, 0)
    for i in range(11):
        c = _mix(fg, target, i / 10)
        if _contrast(c, bg) >= ratio:
            return c
    return target


def _vivid(c) -> tuple[int, int, int]:
    h, s, v = colorsys.rgb_to_hsv(*(x / 255 for x in c))
    if s < 0.12:
        return tuple(int(x) for x in _mix(c, (200, 200, 210), 0.5))
    s, v = max(s, 0.45), max(v, 0.6)
    return tuple(int(x * 255) for x in colorsys.hsv_to_rgb(h, s, v))


def _palette(img: Optional[Image.Image]) -> tuple[tuple, tuple]:
    """Two distinct accent colours from the cover (dominant, then a contrasting hue)."""
    if img is None:
        return DEFAULT_ACCENT, (235, 69, 158)
    q = img.convert("RGB").resize((48, 48)).quantize(colors=8)
    pal = q.getpalette()
    cands = []
    for count, idx in q.getcolors():
        c = tuple(pal[idx * 3:idx * 3 + 3])
        h, s, v = colorsys.rgb_to_hsv(*(x / 255 for x in c))
        cands.append((count * (0.2 + s) * (0.3 + v), c, h, s))
    cands.sort(reverse=True)
    first, h1 = cands[0][1], cands[0][2]
    second = None
    for _, c, h, s in cands[1:]:
        if s > 0.2 and min(abs(h - h1), 1 - abs(h - h1)) > 0.08:
            second = c
            break
    if second is None:
        h, s, v = colorsys.rgb_to_hsv(*(x / 255 for x in _vivid(first)))
        second = tuple(int(x * 255) for x in colorsys.hsv_to_rgb((h + 0.12) % 1, s, v))
    return _vivid(first), _vivid(second)


def _gradient(size, c1, c2, a1: int, a2: int) -> Image.Image:
    w, h = size
    row = Image.new("RGBA", (w, 1))
    row.putdata([(*_mix(c1, c2, x / max(w - 1, 1)), int(a1 + (a2 - a1) * x / max(w - 1, 1)))
                 for x in range(w)])
    return row.resize((w, h))


# ------------------------------------------------------------------- shapes
def _rounded(img: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", img.size, 0)
    _Draw(mask).rounded_rectangle((0, 0, *img.size), radius=radius, fill=255)
    img = img.convert("RGBA")
    img.putalpha(mask)
    return img


def _note(d: ImageDraw.ImageDraw, x: float, y: float, size: float, color):
    """Draw a music note (the font has no ♪ glyph)."""
    r = size * 0.22
    stem_x = x + r * 2
    d.ellipse((x, y + size - 2 * r, x + 2 * r, y + size), fill=color)
    d.rectangle((stem_x - max(size * 0.07, 1.5), y, stem_x, y + size - r), fill=color)
    d.polygon([(stem_x, y), (stem_x + size * 0.35, y + size * 0.2),
               (stem_x + size * 0.35, y + size * 0.38), (stem_x, y + size * 0.2)], fill=color)


ICON_W = 16


def _icon(d: ImageDraw.ImageDraw, kind: str, x: float, cy: float, color, muted=False,
          k: float = 1.0):
    """Small chip icons drawn with shapes (Kanit has no emoji). k scales them."""
    def P(dx, dy):
        return x + dx * k, cy + dy * k

    def box(x0, y0, x1, y1):
        return (*P(x0, y0), *P(x1, y1))

    w2 = max(round(2 * k), 2)
    if kind == "vol":
        d.rectangle(box(0, -3, 4, 3), fill=color)
        d.polygon([P(4, -3), P(9, -7), P(9, 7), P(4, 3)], fill=color)
        if muted:
            d.line(box(11, -4, 16, 4), fill=color, width=w2)
            d.line(box(11, 4, 16, -4), fill=color, width=w2)
        else:
            d.arc(box(5, -6, 15, 6), -55, 55, fill=color, width=w2)
    elif kind == "loop":
        d.arc(box(0, -7, 15, 7), 20, 320, fill=color, width=w2)
        d.polygon([P(10, -8), P(17, -8), P(14, -2)], fill=color)
    elif kind == "queue":
        for i in range(3):
            d.rounded_rectangle(box(0, -6 + i * 5, 15 - i * 3, -4 + i * 5), 1, fill=color)
    elif kind == "hot":
        d.ellipse(box(2, -2, 14, 8), fill=color)
        d.polygon([P(2, 3), P(9, -9), P(14, 3)], fill=color)
    elif kind == "play":
        d.polygon([P(1, -7), P(1, 7), P(14, 0)], fill=color)
    elif kind == "cal":
        d.rounded_rectangle(box(0, -6, 15, 8), 2, outline=color, width=w2)
        d.line(box(0, -2, 15, -2), fill=color, width=w2)
        d.line(box(4, -9, 4, -5), fill=color, width=w2)
        d.line(box(11, -9, 11, -5), fill=color, width=w2)
    elif kind == "live":  # a small stage spotlight dot with rings
        d.ellipse(box(4, -4, 12, 4), fill=color)
        d.arc(box(0, -8, 16, 8), 120, 240, fill=color, width=w2)
        d.arc(box(0, -8, 16, 8), -60, 60, fill=color, width=w2)
    elif kind == "chapter":  # a bookmark
        d.polygon([P(2, -8), P(13, -8), P(13, 8), P(7.5, 3.5), P(2, 8)], fill=color)
    elif kind.startswith("flag:"):
        _flag(d, kind[5:], box(0, -6, 17, 6), k)
    elif kind == "tv":  # a screen on a stand: an episode of a show
        d.rounded_rectangle(box(0, -7, 16, 4), 2, outline=color, width=w2)
        d.line(box(5, 7, 11, 7), fill=color, width=w2)
    elif kind == "mic":  # a talk clip
        d.rounded_rectangle(box(5, -8, 11, 2), 3, fill=color)
        d.arc(box(2, -4, 14, 5), 0, 180, fill=color, width=w2)
        d.line(box(8, 5, 8, 8), fill=color, width=w2)
    elif kind == "clock":
        d.ellipse(box(0, -7, 14, 7), outline=color, width=w2)
        d.line(box(7, -4, 7, 0), fill=color, width=w2)
        d.line(box(7, 0, 10, 2), fill=color, width=w2)
    elif kind == "tag":  # a price-tag shape: which version of the song
        d.polygon([P(0, -6), P(10, -6), P(16, 0), P(10, 6), P(0, 6)], fill=color)
        d.ellipse(box(2.5, -1.5, 5.5, 1.5), fill=(40, 40, 50))  # the hole
    elif kind == "disc":  # a record: the label that put the song out
        d.ellipse(box(0, -7, 14, 7), outline=color, width=w2)
        d.ellipse(box(5, -2, 9, 2), fill=color)
    elif kind == "fx":  # mixer sliders
        for i, knob in enumerate((-3, 3, -1)):
            d.line(box(2 + i * 6, -7, 2 + i * 6, 7), fill=color, width=w2)
            d.ellipse(box(i * 6, knob - 2, 4 + i * 6, knob + 2), fill=color)
    elif kind == "cake":
        d.rounded_rectangle(box(0, 0, 16, 7), 2, fill=color)
        d.rectangle(box(7, -6, 9, 0), fill=color)
        d.ellipse(box(6, -10, 10, -6), fill=color)


# Song language from the letters in its title: (code, name). Thai songs often carry Lao or
# Korean words, so another script wins when it has a few letters of its own.
LANGUAGES = {"la": "เพลงลาว", "kr": "เพลงเกาหลี", "jp": "เพลงญี่ปุ่น", "cn": "เพลงจีน",
             "kh": "เพลงกัมพูชา", "mm": "เพลงพม่า", "vn": "เพลงเวียดนาม", "th": "เพลงไทย"}
_VIET = set("ăâđêôơưĂÂĐÊÔƠƯ")


def song_language(text: str) -> Optional[str]:
    counts = Counter()
    for ch in text:
        o = ord(ch)
        if 0x0E80 <= o <= 0x0EFF:
            counts["la"] += 1
        elif 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF:
            counts["kr"] += 1
        elif 0x3040 <= o <= 0x30FF:
            counts["jp"] += 1
        elif 0x4E00 <= o <= 0x9FFF:
            counts["cn"] += 1
        elif 0x1780 <= o <= 0x17FF:
            counts["kh"] += 1
        elif 0x1000 <= o <= 0x109F:
            counts["mm"] += 1
        elif 0x0E00 <= o <= 0x0E7F:
            counts["th"] += 1
        elif ch in _VIET or 0x1EA0 <= o <= 0x1EFF:
            counts["vn"] += 1
    if counts["jp"] and counts["cn"]:  # kanji with kana: Japanese
        counts["jp"] += counts.pop("cn")
    other = [(n, c) for c, n in counts.items() if c != "th" and n >= 2]
    if other:
        return max(other)[1]
    return "th" if counts["th"] else None


def _star(d, cx, cy, r, color):
    pts = []
    for i in range(10):
        a = -math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * 0.42
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    d.polygon(pts, fill=color)


def _flag(d, code: str, box, k: float):
    """A tiny flag drawn with shapes (the card font has no flag emoji)."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0

    def bands(*colors, weights=None):
        weights = weights or [1] * len(colors)
        y, unit = y0, h / sum(weights)
        for c, wt in zip(colors, weights):
            d.rectangle((x0, y, x1, y + unit * wt), fill=c)
            y += unit * wt
    if code == "th":
        bands((165, 25, 49), WHITE, (45, 42, 74), WHITE, (165, 25, 49), weights=[1, 1, 2, 1, 1])
    elif code == "la":
        bands((206, 17, 38), (0, 40, 104), (206, 17, 38), weights=[1, 2, 1])
        r = h * 0.2
        d.ellipse((x0 + w / 2 - r, y0 + h / 2 - r, x0 + w / 2 + r, y0 + h / 2 + r), fill=WHITE)
    elif code == "kh":
        bands((3, 46, 161), (224, 0, 37), (3, 46, 161), weights=[1, 2, 1])
        d.rectangle((x0 + w * 0.36, y0 + h * 0.36, x0 + w * 0.64, y0 + h * 0.66), fill=WHITE)
    elif code == "mm":
        bands((254, 203, 0), (52, 178, 51), (234, 40, 57))
        _star(d, x0 + w / 2, y0 + h * 0.55, h * 0.45, WHITE)
    elif code == "jp":
        d.rectangle(box, fill=WHITE)
        r = h * 0.3
        d.ellipse((x0 + w / 2 - r, y0 + h / 2 - r, x0 + w / 2 + r, y0 + h / 2 + r),
                  fill=(188, 0, 45))
    elif code == "kr":
        d.rectangle(box, fill=WHITE)
        r = h * 0.3
        c = (x0 + w / 2 - r, y0 + h / 2 - r, x0 + w / 2 + r, y0 + h / 2 + r)
        d.pieslice(c, 180, 360, fill=(205, 46, 58))
        d.pieslice(c, 0, 180, fill=(0, 71, 160))
    elif code == "cn":
        d.rectangle(box, fill=(238, 28, 37))
        _star(d, x0 + w * 0.25, y0 + h * 0.32, h * 0.22, (255, 255, 0))
    elif code == "vn":
        d.rectangle(box, fill=(218, 37, 29))
        _star(d, x0 + w / 2, y0 + h / 2 + 0.3 * k, h * 0.34, (255, 255, 0))


# --------------------------------------------------------------------- art
def _trim_bars(img: Image.Image) -> Image.Image:
    """Remove letterbox bars (YouTube hqdefault has black bars above and below)."""
    box = img.convert("L").point(lambda v: 255 if v > 22 else 0).getbbox()
    if not box:
        return img
    w, h = img.size
    l, t, r, b = box
    bars_tb = t > h * 0.04 and abs(t - (h - b)) < h * 0.04
    bars_lr = l > w * 0.04 and abs(l - (w - r)) < w * 0.04
    if not (bars_tb or bars_lr):
        return img
    box = (l if bars_lr else 0, t if bars_tb else 0, r if bars_lr else w, b if bars_tb else h)
    if (box[2] - box[0]) < w * 0.4 or (box[3] - box[1]) < h * 0.4:
        return img
    return img.crop(box)


def _open_art(art_bytes: Optional[bytes]) -> Optional[Image.Image]:
    if not art_bytes:
        return None
    try:
        return _trim_bars(Image.open(io.BytesIO(art_bytes)).convert("RGB"))
    except Exception:
        return None


def _background(g: Geo, theme: str, art: Optional[Image.Image], a1, a2) -> Image.Image:
    if theme in ("blur", "polaroid") and art:
        bg = ImageOps.fit(art, (g.w, g.h)).filter(ImageFilter.GaussianBlur(28))
        bg = Image.blend(bg, Image.new("RGB", (g.w, g.h), (12, 12, 18)), 0.62)
    elif theme == "minimal":
        bg = Image.new("RGB", (g.w, g.h), (20, 20, 26))
    elif theme == "neon":
        bg = Image.new("RGB", (g.w, g.h), (8, 8, 14))
    else:
        bg = Image.new("RGB", (g.w, g.h), _mix((14, 14, 20), a1, 0.18))
    canvas = bg.convert("RGBA")
    if theme == "neon":
        canvas.alpha_composite(_gradient((g.w, g.h), a1, a2, 26, 10))
    elif theme != "minimal":  # two-tone accent glow
        canvas.alpha_composite(_gradient((g.w, g.h), a1, a2, 70, 22))
    return canvas


FULL_COVER_MIN = 120   # px: smaller covers (thumbnails) are simply cropped
FULL_COVER_RATIO = 1.2  # wider (or taller) than this vs the box: show the whole picture


def _cover(art: Optional[Image.Image], size, accent) -> Image.Image:
    """The cover in a w x h box. A video thumbnail (16:9) in a square box is shown whole,
    with a blurred copy of itself filling the space above and below, instead of cropping
    off its sides (and the text on it, like "Official Video")."""
    w, h = (size, size) if isinstance(size, int) else size
    if art:
        ratio = (art.width / art.height) / (w / h)
        if (config.CARD_FULL_COVER and min(w, h) >= FULL_COVER_MIN
                and not 1 / FULL_COVER_RATIO <= ratio <= FULL_COVER_RATIO):
            fill = ImageOps.fit(art, (w // 4, h // 4)).filter(ImageFilter.GaussianBlur(4))
            fill = ImageEnhance.Brightness(fill.resize((w, h), Image.BILINEAR)).enhance(0.6)
            whole = ImageOps.contain(art, (w, h), Image.LANCZOS)
            fill.paste(whole, ((w - whole.width) // 2, (h - whole.height) // 2))
            return fill
        return ImageOps.fit(art, (w, h))
    cover = Image.new("RGB", (w, h), accent)
    s = min(w, h)
    _note(_Draw(cover), (w - s * 0.42) / 2, (h - s * 0.42) / 2, s * 0.42, WHITE)
    return cover


def _shadow_of(img: Image.Image) -> Image.Image:
    """The soft drop shadow of an RGBA image, 20 px larger on each side (paste at -20)."""
    w, h = img.size
    shadow = Image.new("RGBA", (w + 40, h + 40), (0, 0, 0, 0))
    alpha = img.getchannel("A").point(lambda a: 150 if a else 0)
    shadow.paste((0, 0, 0, 255), (20, 24), alpha)
    return shadow.filter(ImageFilter.GaussianBlur(10))


def _shadowed(canvas: Image.Image, img: Image.Image, x: int, y: int, radius: int = 24):
    """Paste an RGBA image with a soft drop shadow."""
    canvas.alpha_composite(_shadow_of(img), (x - 20, y - 20))
    canvas.alpha_composite(img, (x, y))


def _paste_cover(canvas: Image.Image, cover: Image.Image, x: int, y: int, radius: int = 24):
    _shadowed(canvas, _rounded(cover, radius), x, y, radius)


def _source_badge(img: Image.Image, x: float, y: float, track: Track):
    key = track.origin if track.origin in SOURCE_COLORS else detect_source(track.url)
    name = SOURCE_NAMES.get(key)
    if not name:
        return
    d = _Draw(img)
    fnt = font("Medium", 15)
    tw = d.textlength(name, font=fnt)
    color = SOURCE_COLORS[key]
    rgb = ((color >> 16) & 255, (color >> 8) & 255, color & 255)
    d.rounded_rectangle((x, y, x + tw + 34, y + 26), 13, fill=rgb)
    if key == "youtube":
        d.polygon([(x + 10, y + 7), (x + 10, y + 19), (x + 20, y + 13)], fill=WHITE)
    else:
        d.ellipse((x + 10, y + 8, x + 20, y + 18), fill=WHITE)
    d.text((x + 26, y + 13), name, font=fnt, fill=WHITE, anchor="lm")


# ------------------------------------------------------------ art blocks
VINYL_PEEK = 30   # px of the record showing beside the cover
VINYL_SIZE = 0.94  # of the cover


def _vinyl(size: int, label, angle: float) -> Image.Image:
    """A black record with grooves, a label in the cover's colour, and a light sheen at
    angle (degrees). Turning the sheen frame by frame makes it spin."""
    disc = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = _Draw(disc)
    r = size / 2
    d.ellipse((0, 0, size - 1, size - 1), fill=(20, 20, 24, 255))
    for i, rr in enumerate(range(int(r * 0.38), int(r * 0.95), 3)):
        c = 44 + (14 if i % 4 == 0 else 0)  # grooves light enough to see on a dark card
        d.ellipse((r - rr, r - rr, r + rr, r + rr), outline=(c, c, c + 5, 255), width=1)
    # a lit rim: the record's edge shows on any background
    d.ellipse((1, 1, size - 2, size - 2), outline=(150, 150, 160, 255), width=2)
    sheen = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sd = _Draw(sheen)
    for a in (angle, angle + 180):
        sd.pieslice((0, 0, size - 1, size - 1), a - 16, a + 16, fill=(255, 255, 255, 72))
    sheen = sheen.filter(ImageFilter.GaussianBlur(size * 0.03))
    sheen.putalpha(ImageChops.multiply(sheen.getchannel("A"), disc.getchannel("A")))
    disc.alpha_composite(sheen)
    lr = r * 0.33
    d = _Draw(disc)
    d.ellipse((r - lr, r - lr, r + lr, r + lr), fill=(*label, 255))
    d.ellipse((r - 4, r - 4, r + 4, r + 4), fill=(14, 14, 17, 255))
    return disc


@dataclass
class Vinyl:
    """The part of the record beside the cover, one picture per animation frame."""
    x: int
    y: int
    strips: list


def _art_plain(canvas, g: Geo, art, a1, track) -> Optional[Vinyl]:
    cover = _cover(art, g.art, a1)
    vinyl = None
    if config.CARD_VINYL and g is not MINI:
        size = round(g.art * VINYL_SIZE)
        x0 = g.art_x + g.art + VINYL_PEEK - size
        y0 = round(g.art_y + (g.art - size) / 2)
        label = _mix(a1, WHITE, 0.08)
        discs = [_vinyl(size, label, i * 180 / EQ_FRAMES) for i in range(EQ_FRAMES)]
        canvas.alpha_composite(discs[0], (x0, y0))
        # frames redraw only the strip beside the cover, with the cover's shadow on it
        sx = g.art_x + g.art
        shadow = _shadow_of(_rounded(cover, 24))
        strips = []
        for disc in discs:
            strip = disc.crop((sx - x0, 0, size, size))
            sh = shadow.crop((sx - (g.art_x - 20), y0 - (g.art_y - 20),
                              sx - (g.art_x - 20) + strip.width, y0 - (g.art_y - 20) + size))
            sh.putalpha(ImageChops.multiply(sh.getchannel("A"), strip.getchannel("A")))
            strip.alpha_composite(sh)
            strips.append(strip)
        vinyl = Vinyl(sx, y0, strips)
    _paste_cover(canvas, cover, g.art_x, g.art_y)
    _source_badge(canvas, g.art_x + 12, g.art_y + g.art - 38, track)
    return vinyl


def _tint_strip(strip: Image.Image, rgba) -> Image.Image:
    """A colour wash over the record strip only (night, hit part), not around it."""
    wash = Image.new("RGBA", strip.size, rgba)
    wash.putalpha(ImageChops.multiply(wash.getchannel("A"), strip.getchannel("A")))
    out = strip.copy()
    out.alpha_composite(wash)
    return out


def _art_neon(canvas, g: Geo, art, a1, track):
    box = (g.art_x - 2, g.art_y - 2, g.art_x + g.art + 2, g.art_y + g.art + 2)
    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    _Draw(glow).rounded_rectangle(box, 26, outline=(*a1, 255), width=8)
    canvas.alpha_composite(glow.filter(ImageFilter.GaussianBlur(14)))
    canvas.alpha_composite(_rounded(_cover(art, g.art, a1), 24), (g.art_x, g.art_y))
    _Draw(canvas).rounded_rectangle(box, 26, outline=_mix(a1, WHITE, 0.35), width=3)
    _source_badge(canvas, g.art_x + 12, g.art_y + g.art - 38, track)


def _art_polaroid(canvas, g: Geo, art, a1, track):
    s = g.art
    pad, bottom = max(s // 22, 10), max(s // 6, 34)
    frame = Image.new("RGBA", (s, s), (246, 243, 236, 255))
    photo = _cover(art, (s - 2 * pad, s - pad - bottom), a1)
    frame.paste(photo, (pad, pad))
    d = _Draw(frame)
    caption = track.artist or display_title(track)
    fnt = font("Regular", max(s // 18, 13))
    d.text((s / 2, s - bottom / 2), _fit(d, caption, fnt, s - 2 * pad), font=fnt,
           fill=(70, 66, 60), anchor="mm")
    _source_badge(frame, pad + 10, s - bottom - 36, track)
    tilted = frame.rotate(-3, resample=Image.BICUBIC, expand=True)
    ox = g.art_x - (tilted.width - s) // 2
    oy = g.art_y - (tilted.height - s) // 2
    _shadowed(canvas, tilted, ox, oy)


def _art_cassette(canvas, g: Geo, art, a1, track):
    w = g.art
    h = int(w * 0.66)
    body = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = _Draw(body)
    shell = _mix(a1, (18, 18, 24), 0.6)
    d.rounded_rectangle((0, 0, w, h), 18, fill=(*shell, 255))
    lb = (14, 14, w - 14, int(h * 0.70))
    body.alpha_composite(_rounded(_cover(art, (lb[2] - lb[0], lb[3] - lb[1]), a1), 10), lb[:2])
    win = (int(w * 0.24), int(h * 0.36), int(w * 0.76), int(h * 0.62))
    d.rounded_rectangle(win, 14, fill=(20, 20, 26, 225))
    r = (win[3] - win[1]) * 0.36
    cy = (win[1] + win[3]) / 2
    for cx in (w * 0.36, w * 0.64):
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(235, 235, 240, 255))
        d.ellipse((cx - r * 0.45, cy - r * 0.45, cx + r * 0.45, cy + r * 0.45), fill=(30, 30, 36, 255))
        for k in range(6):
            a = math.pi * k / 3
            d.line((cx, cy, cx + math.cos(a) * r * 0.42, cy + math.sin(a) * r * 0.42),
                   fill=(235, 235, 240, 255), width=2)
    d.polygon([(w * 0.18, h), (w * 0.24, h * 0.80), (w * 0.76, h * 0.80), (w * 0.82, h)],
              fill=(*_mix(shell, WHITE, 0.12), 255))
    for sx, sy in ((10, 10), (w - 10, 10), (10, h - 10), (w - 10, h - 10)):
        d.ellipse((sx - 4, sy - 4, sx + 4, sy + 4), fill=(*_mix(shell, WHITE, 0.3), 255))
    _source_badge(body, lb[0] + 10, lb[1] + 10, track)
    _shadowed(canvas, body, g.art_x, g.art_y + (g.art - h) // 2)


ART_BLOCKS = {"neon": _art_neon, "polaroid": _art_polaroid, "cassette": _art_cassette}


# ----------------------------------------------------------------- drawing
def _row_x(g: Geo, width: float) -> float:
    return (g.w - width) / 2 if g.center else g.x0


def _text(d: ImageDraw.ImageDraw, g: Geo, y: float, text: str, fnt, fill):
    if g.center:
        d.text((g.w / 2, y), text, font=fnt, fill=fill, anchor="ma")
    else:
        d.text((g.x0, y), text, font=fnt, fill=fill)


def _glow_text(canvas: Image.Image, g: Geo, y: float, text: str, fnt, color):
    """Neon: a blurred coloured copy behind the text."""
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    _text(_Draw(layer), g, y, text, fnt, (*color, 255))
    canvas.alpha_composite(layer.filter(ImageFilter.GaussianBlur(7)))


@dataclass
class Base:
    canvas: Image.Image
    accent: tuple
    accent2: tuple
    bg: tuple
    text_bottom: int
    vinyl: Optional[Vinyl] = None


_base_cache: "OrderedDict[tuple, Base]" = OrderedDict()
BASE_CACHE_SIZE = 32  # several servers playing at once must not evict each other


_accents: "OrderedDict[str, int]" = OrderedDict()
SLOW_RENDER = 0.8  # seconds: log cards slower than this (helps find stutter causes)


def accent_of(track: Track) -> Optional[int]:
    """Main colour of the track's cover (0xRRGGBB), once a card for it was drawn."""
    return _accents.get(track.url)


def _avatar(avatar: Optional[bytes], size: int) -> Optional[Image.Image]:
    img = _open_art(avatar)
    if img is None:
        return None
    img = ImageOps.fit(img.convert("RGBA"), (size * 3, size * 3), Image.LANCZOS)
    mask = Image.new("L", img.size, 0)
    _Draw(mask).ellipse((0, 0, *img.size), fill=255)
    img.putalpha(mask)
    return img.resize((size, size), Image.LANCZOS)


def _who(canvas: Image.Image, d: ImageDraw.ImageDraw, x: float, y: float, text: str, fnt,
         fill, avatar: Optional[Image.Image], max_w: float) -> float:
    """'ขอโดย name' with the requester's round avatar in front. Returns the width used."""
    used = 0
    if avatar is not None:
        size = avatar.width
        cy = y + fnt.size * 0.62  # middle of the Thai x-height in Kanit
        canvas.alpha_composite(avatar, (int(x), int(cy - size / 2)))
        used = size + 8
    text = _fit(d, text, fnt, max_w - used)
    d.text((x + used, y), text, font=fnt, fill=fill)
    return used + d.textlength(text, font=fnt)


def _render_base(track: Track, art_bytes: Optional[bytes], g: Geo, theme: str,
                 avatar_bytes: Optional[bytes] = None, night: bool = False) -> Base:
    """Static part (background, cover, title, artist, requester). Cached per track."""
    key = (track.url, track.title, track.artist, track.requester_name,
           len(art_bytes or b""), len(avatar_bytes or b""), g, theme, night, _font_files())
    if key in _base_cache:
        _base_cache.move_to_end(key)
        return _base_cache[key]
    art = _open_art(art_bytes)
    a1, a2 = _palette(art)
    if art is not None:
        _accents[track.url] = (a1[0] << 16) | (a1[1] << 8) | a1[2]
        _accents.move_to_end(track.url)
        while len(_accents) > 200:
            _accents.popitem(last=False)
    canvas = _background(g, theme, art, a1, a2)
    vinyl = None
    if g is MINI:
        _paste_cover(canvas, _cover(art, g.art, a1), g.art_x, g.art_y, radius=14)
    else:
        vinyl = ART_BLOCKS.get(theme, _art_plain)(canvas, g, art, a1, track)

    if night:  # darker background and cover; the text drawn next stays bright
        canvas.alpha_composite(Image.new("RGBA", canvas.size, NIGHT_SHADE))
        if vinyl:
            vinyl.strips = [_tint_strip(st, NIGHT_SHADE) for st in vinyl.strips]

    # Text colours checked against the real background behind the text column.
    region = (0, g.label_y, g.w, g.h) if g.center else (g.x0, 0, g.w, g.h)
    bg = tuple(int(v) for v in ImageStat.Stat(canvas.crop(region).convert("RGB")).mean)
    accent = _readable(a1, bg)
    accent2 = _readable(a2, bg)

    d = _Draw(canvas)
    if g is MINI:
        fnt = font("Bold", g.title_size)
        d.text((g.x0, g.title_y), _fit(d, display_title(track), fnt, g.max_w), font=fnt, fill=WHITE)
        sub = " · ".join(x for x in (display_artist(track), f"ขอโดย {track.requester_name or '-'}")
                         if x)
        small = font("Regular", 15)
        d.text((g.x0, g.title_y + 32), _fit(d, sub, small, g.max_w), font=small,
               fill=_readable((200, 200, 212), bg))
        base = Base(canvas, accent, accent2, bg, g.h)
    else:
        title_font, lines = _fit_title(d, display_title(track), g.title_size, g.max_w,
                                       size2=g.title_size2)
        lh = int(title_font.size * 1.3)
        size2 = g.title_size2
        # wide: a two-line title must leave the artist line clear of the chips below it
        while (size2 and len(lines) > 1 and size2 > TITLE_MIN
               and g.title_y + len(lines) * lh + TITLE_ROOM > g.chips_y):
            size2 -= 2
            title_font, lines = _fit_title(d, display_title(track), g.title_size, g.max_w,
                                           size2=size2)
            lh = int(title_font.size * 1.3)
        y = g.title_y
        for line in lines:
            if theme == "neon":
                _glow_text(canvas, g, y, line, title_font, a1)
                d = _Draw(canvas)
            _text(d, g, y, line, title_font, WHITE)
            y += lh
        who = f"ขอโดย {track.requester_name or '-'}"
        grey = _readable((185, 185, 198), bg)
        if g.title_size2:  # wide: artist and requester share one line
            y += 4
            x = g.x0
            if display_artist(track):
                fnt = font("Medium", 30)
                artist = _fit(d, display_artist(track), fnt, g.max_w * 0.6)
                d.text((x, y), artist, font=fnt, fill=_readable((225, 225, 235), bg))
                x += d.textlength(artist, font=fnt)
                dot = "  ·  "
                d.text((x, y + 4), dot, font=font("Regular", 25), fill=grey)
                x += d.textlength(dot, font=font("Regular", 25))
            fnt = font("Regular", 25)
            _who(canvas, d, x, y + 4, who, fnt, grey, _avatar(avatar_bytes, 30),
                 g.x0 + g.max_w - x)
            base = Base(canvas, accent, accent2, bg, y + 40)
        else:
            if display_artist(track):
                fnt = font("Medium", 23)
                _text(d, g, y + 2, _fit(d, display_artist(track), fnt, g.max_w), fnt,
                      _readable((225, 225, 235), bg))
                y += 32
            fnt = font("Regular", 19)
            av = _avatar(avatar_bytes, 24)
            width = (av.width + 8 if av else 0) + d.textlength(who, font=fnt)
            x = _row_x(g, min(width, g.max_w))
            _who(canvas, d, x, y + 4, who, fnt, grey, av, g.max_w)
            d = _Draw(canvas)
            base = Base(canvas, accent, accent2, bg, y + 32)

    base.vinyl = vinyl
    _base_cache[key] = base
    while len(_base_cache) > BASE_CACHE_SIZE:
        _base_cache.popitem(last=False)
    return base


def _flow(g: Geo, text_bottom: int) -> Geo:
    """Square: put chips, next, waveform and times right under the text."""
    if not g.center:
        return g
    chips = max(text_bottom + 18, g.title_y + 120)
    wave = chips + 94  # room for the chapter dots and the replay peak above the bars
    return dataclasses.replace(g, chips_y=chips, next_y=chips + 44, wave_y=wave,
                               time_y=wave + g.wave_h + 6, h=wave + g.wave_h + 6 + 46)


# -------------------------------------------------------------- waveform
def _resample(values: tuple[float, ...], n: int) -> list[float]:
    m = len(values)
    return [values[min(int((i + 0.5) / n * m), m - 1)] for i in range(n)]


@functools.lru_cache(maxsize=128)
def _wave_heights(seed: str, n: int, heat: tuple[float, ...] = ()) -> tuple[float, ...]:
    """Bar heights. With YouTube 'most replayed' data the shape is real; otherwise a stable
    pseudo-random shape (the same track always draws the same)."""
    if heat:
        vals = _resample(heat, n)
        top = max(vals) or 1
        return tuple(0.14 + 0.86 * (v / top) ** 0.8 for v in vals)
    rnd = random.Random(zlib.crc32(seed.encode()))
    raw = [rnd.random() for _ in range(n)]
    out = []
    for i in range(n):
        v = (raw[max(i - 1, 0)] + 2 * raw[i] + raw[min(i + 1, n - 1)]) / 4
        env = 0.55 + 0.45 * math.sin(math.pi * (i + 0.5) / n)
        out.append(max(0.14, min(1.0, v * env * 1.5)))
    return tuple(out)


WAVE_BAR, WAVE_GAP = 4, 3


def _wave_box(g: Geo) -> tuple[float, float]:
    n = g.max_w // (WAVE_BAR + WAVE_GAP)
    total = n * (WAVE_BAR + WAVE_GAP) - WAVE_GAP
    return _row_x(g, total), total


def heat_peak(heat: tuple[float, ...]) -> Optional[float]:
    """Fraction (0..1) of the track people replay most, or None without data."""
    if not heat or max(heat) <= 0:
        return None
    i = max(range(len(heat)), key=heat.__getitem__)
    return (i + 0.5) / len(heat)


WAVE_GLOW_ALPHA = 210  # the soft glow under played bars (non-neon themes)
HOT_LEVEL = 0.85  # heatmap level (of the peak) that counts as the hit part
HOT_BAR = (255, 140, 40)


def hot_bars(heat: tuple[float, ...], n: int) -> list[bool]:
    """Which of n waveform bars fall in the hit part (heat at HOT_LEVEL of the peak)."""
    if not heat or max(heat) <= 0:
        return [False] * n
    top = max(heat)
    return [heat[min(int((i + 0.5) / n * len(heat)), len(heat) - 1)] >= HOT_LEVEL * top
            for i in range(n)]


def _draw_wave(canvas: Image.Image, g: Geo, track: Track, ratio: Optional[float], c1, c2,
               bg, glow: bool = False):
    """ratio None = everything dim (live / loading), 1.0 = everything lit (share card).
    The hit part is orange, so people see it coming."""
    n = g.max_w // (WAVE_BAR + WAVE_GAP)
    x0, total = _wave_box(g)
    cy = g.wave_y + g.wave_h / 2
    heat = tuple(track._heatmap or ())
    heights = _wave_heights(track.video_id or track.url, n, heat)
    hot = hot_bars(heat, n) if track.duration and g is not MINI else [False] * n
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ld = _Draw(layer)
    lit = []
    for i, hgt in enumerate(heights):
        x = x0 + i * (WAVE_BAR + WAVE_GAP)
        half = max(hgt * g.wave_h / 2, 2)
        box = (x, cy - half, x + WAVE_BAR, cy + half)
        if ratio is not None and (i + 0.5) / n <= ratio:
            lit.append((box, HOT_BAR if hot[i] else _mix(c1, c2, i / max(n - 1, 1))))
        else:
            ld.rounded_rectangle(box, 2, fill=(*HOT_BAR, 120) if hot[i] else (255, 255, 255, 70))
    canvas.alpha_composite(layer)
    if lit and (glow or (config.CARD_WAVE_GLOW and g is not MINI)):
        # light from the played bars (neon: strong). Blurred in a strip, not the whole card.
        pad = 16
        box0 = (int(x0 - pad), int(g.wave_y - pad), int(x0 + total + pad),
                int(g.wave_y + g.wave_h + pad))
        gl = Image.new("RGBA", (box0[2] - box0[0], box0[3] - box0[1]), (0, 0, 0, 0))
        gd = _Draw(gl)
        strength = 255 if glow else WAVE_GLOW_ALPHA
        for box, color in lit:
            gd.rounded_rectangle((box[0] - box0[0], box[1] - box0[1] - 2, box[2] - box0[0] + 1,
                                  box[3] - box0[1] + 2), 2, fill=(*color, strength))
        canvas.alpha_composite(gl.filter(ImageFilter.GaussianBlur(6 if glow else 5)), box0[:2])
    d = _Draw(canvas)
    for box, color in lit:
        d.rounded_rectangle(box, 2, fill=color)

    dur = track.duration
    if dur:
        # chapter boundaries: a gap in the bars and a dot above (no dot on the slim mini card)
        for start, _ in track._chapters or ():
            if 0 < start < dur:
                cx = x0 + total * start / dur
                d.rectangle((cx - 2, g.wave_y - 2, cx + 2, g.wave_y + g.wave_h + 2), fill=bg)
                if g is not MINI:
                    d.ellipse((cx - 2.5, g.wave_y - 8, cx + 2.5, g.wave_y - 3),
                              fill=(220, 220, 230))
        peak = heat_peak(heat)
        if peak is not None and g is not MINI:  # most replayed part
            px = x0 + total * peak
            _icon(d, "hot", px - 8, g.wave_y - 9, ORANGE)
    if ratio is not None and 0 < ratio < 1:
        _playhead(canvas, x0 + total * ratio, cy, c1, 4 if g is MINI else 7)


def _playhead(canvas: Image.Image, x: float, cy: float, color, r: float):
    """Where the song is: a white dot with a glow in the cover's colour."""
    pad = int(r * 3)
    glow = Image.new("RGBA", (pad * 2, pad * 2), (0, 0, 0, 0))
    _Draw(glow).ellipse((pad - r * 1.9, pad - r * 1.9, pad + r * 1.9, pad + r * 1.9),
                        fill=(*_vivid(color), 200))
    canvas.alpha_composite(glow.filter(ImageFilter.GaussianBlur(r * 0.7)),
                           (int(x - pad), int(cy - pad)))
    d = _Draw(canvas)
    d.ellipse((x - r, cy - r, x + r, cy + r), fill=WHITE)


def current_chapter(track: Track, position: float) -> str:
    title = ""
    for start, name in track._chapters or ():
        if start <= position:
            title = name
    return title


# ------------------------------------------------------------- other parts
def _draw_chips(canvas: Image.Image, g: Geo, chips: list[tuple]):
    """chips: (icon, text, rgba fill, text colour, muted)."""
    k = g.s
    fnt = font("Medium", round(17 * k))
    d = _Draw(canvas)
    gap, icon_w, pad = 10 * k, (ICON_W + 8) * k, 26 * k
    sizes = []
    for icon, text, *_ in chips:
        sizes.append((icon_w if icon else 0) + d.textlength(text, font=fnt) + pad)
    while chips and sum(sizes) + gap * (len(sizes) - 1) > g.max_w:
        chips, sizes = chips[:-1], sizes[:-1]
    if not chips:
        return
    x = _row_x(g, sum(sizes) + gap * (len(sizes) - 1))
    y0, y1 = g.chips_y, g.chips_y + 30 * k
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ld = _Draw(layer)
    spots = []
    for (icon, text, fill, color, muted), w in zip(chips, sizes):
        ld.rounded_rectangle((x, y0, x + w, y1), 15 * k, fill=fill)
        spots.append((x + 13 * k, icon, text, color, muted))
        x += w + gap
    canvas.alpha_composite(layer)
    d = _Draw(canvas)
    cy = (y0 + y1) / 2
    for tx, icon, text, color, muted in spots:
        if icon:
            _icon(d, icon, tx, cy, color, muted, k)
            tx += icon_w
        d.text((tx, cy), text, font=fnt, fill=color, anchor="lm")


def _chip_colors(accent) -> tuple[tuple, tuple]:
    """Chip fill tinted with the cover colour, dark enough for white text."""
    base = _mix(accent, (14, 14, 20), 0.55)
    return (*base, 215), _readable((245, 245, 250), base)


CHAPTER_CHARS = 22


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _volume_chip(volume: int, soft=(255, 255, 255, 38), text=(240, 240, 245)) -> tuple:
    if volume >= 130:
        fill, color = (*RED, 215), WHITE
    elif volume > 100:
        fill, color = (*ORANGE, 205), WHITE
    else:
        fill, color = soft, text
    return ("vol", f"{volume}%", fill, color, volume == 0)


def queue_time(seconds: int) -> str:
    """'8 นาที', '1 ชม. 5 น.' for the queue chip (whole minutes, so it rarely changes)."""
    m = max(round(seconds / 60), 1)
    return f"{m} นาที" if m < 60 else f"{m // 60} ชม. {m % 60} น."


def short_count(n: int) -> str:
    """1234 -> 1.2K, 3400000 -> 3.4M, 1200000000 -> 1.2B."""
    for size, unit in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= size:
            v = n / size
            text = f"{v:.1f}".rstrip("0").rstrip(".") if v < 10 else f"{v:.0f}"
            return text + unit
    return str(n)


def short_label(name: str, limit: int = LABEL_CHARS) -> str:
    """A channel name for a small chip: words like 'Thailand' / 'Official' out, one script
    when it mixes English and Thai ('Shock Tonight คำคืนแห่งความหลอน' -> 'Shock Tonight'),
    and cut between words, never inside one."""
    out = re.sub(r"(?i)\s+(?:thailand|official|channel|thai)\b", "", name).strip() or name
    words = out.split()
    if len(words) > 1 and any(_has_thai(w) for w in words) and any(w.isascii() for w in words):
        lead = _has_thai(words[0])
        run = []
        for w in words:
            if _has_thai(w) != lead:
                break
            run.append(w)
        out = " ".join(run)
    if len(out) <= limit:
        return out
    cut = out.rfind(" ", 0, limit)
    return (out[:cut] if cut >= limit // 2 else out[:limit - 1]).rstrip(" -–|·,") + "…"


def _info_chips(st: CardState, fill, text, track: Optional[Track] = None) -> list[tuple]:
    """Language, views and year: nice to know, so they are the first to go when space
    runs out."""
    out = []
    lang = song_language(f"{track.title} {track.artist or ''}") if track is not None else None
    label = song_parts(track).label if track is not None else ""
    if label:  # the record label that uploaded it ('RS Music Thailand' -> 'RS Music')
        out.append(("disc", short_label(label), fill, text, False))
    if lang:
        out.append((f"flag:{lang}", LANGUAGES[lang], fill, text, False))
    if st.views:
        out.append(("play", short_count(st.views), fill, text, False))
    if st.year:
        out.append(("cal", st.year, fill, text, False))
    return out


_LIVE_SHOW = re.compile(r"แสดงสด|คอนเสิร์ต|\blive\b|\bconcert\b|\blive at\b", re.I)


def is_live_show(track: Track) -> bool:
    """A recorded live performance (not a live stream, which has no duration)."""
    return bool(track.duration) and bool(_LIVE_SHOW.search(track.title or ""))


def _badges(st: CardState, track: Optional[Track] = None) -> list[tuple]:
    out = []
    live = track is not None and is_live_show(track)
    if live:
        out.append(("live", "แสดงสด", (*RED, 225), WHITE, False))
    parts = song_parts(track) if track is not None else None
    if parts and parts.series:  # the show and its episode
        out.append(("tv", parts.series, (255, 255, 255, 46), WHITE, False))
    version = parts.version if parts else ""
    if version and not (live and re.fullmatch(r"(?i)live|แสดงสด", version.strip())):
        out.append(("tag", version, (255, 255, 255, 46), WHITE, False))
    if st.hot >= HOT_THRESHOLD:
        out.append(("hot", f"ฮิต ×{st.hot}", (255, 122, 26, 215), WHITE, False))
    if st.birthday:
        out.append(("cake", "วันเกิดคนขอ!", (235, 69, 158, 215), WHITE, False))
    return out


def _draw_label(canvas: Image.Image, g: Geo, st: CardState, track: Track, accent):
    d = _Draw(canvas)
    k = g.s
    fnt = font("Medium", round(19 * k))
    live = not track.duration
    if st.mode == "loading":
        text, color, icon = "กำลังโหลด · LOADING", (200, 200, 210), "dots"
    elif st.mode == "error":
        text, color, icon = "เล่นไม่ได้ · SKIPPED", (255, 120, 120), None
    elif st.mode == "share":
        text, color, icon = "กำลังฟัง · NOW LISTENING", accent, None
    elif st.paused:
        text, color, icon = "หยุดชั่วคราว · PAUSED", (200, 200, 210), "pause"
    else:
        text, color, icon = "กำลังเล่น · NOW PLAYING", accent, "eq"
    if live and st.mode in ("play", "share"):
        text = "LIVE · " + text
        icon = "live" if icon in (None, "eq") else icon
    icon_w = {"pause": 24, "dots": 30, "live": 22, "eq": 28}.get(icon, 0) * k
    x = _row_x(g, icon_w + d.textlength(text, font=fnt))
    cy = g.label_y + 12 * k
    if icon == "pause":
        d.rectangle((x, cy - 8 * k, x + 5 * k, cy + 8 * k), fill=color)
        d.rectangle((x + 10 * k, cy - 8 * k, x + 15 * k, cy + 8 * k), fill=color)
    elif icon == "dots":
        for i in range(3):
            d.ellipse((x + i * 8 * k, cy - 3 * k, x + (i * 8 + 6) * k, cy + 3 * k), fill=color)
    elif icon == "live":
        dot = (x + k, cy - 6 * k, x + 13 * k, cy + 6 * k)
        if st.blink:  # alternates every refresh: a slow blink
            d.ellipse(dot, fill=RED)
        else:
            d.ellipse(dot, outline=RED, width=max(round(2 * k), 2))
    d.text((x + icon_w, cy), text, font=fnt, fill=color, anchor="lm")
    if icon == "eq":
        spot = (x, cy, k, color)
        if not st.animate:
            _eq(canvas, spot, EQ_STILL)
        return spot
    return None


# Equalizer next to "กำลังเล่น": bar heights (0..1) per frame, a short seamless loop.
EQ_STILL = (0.55, 0.9, 0.4, 0.75)
EQ_FRAMES = 8      # 8 frames: a third less CPU than 12, and still smooth
EQ_FRAME_MS = 165


def _eq_frame(i: int) -> tuple:
    t = i / EQ_FRAMES * 2 * math.pi
    return tuple(0.5 + 0.45 * math.sin(t * m + p) for m, p in ((1, 0.0), (2, 1.9), (1, 3.7), (2, 5.1)))


def _eq(canvas: Image.Image, spot, heights):
    x, cy, k, color = spot
    d = _Draw(canvas)
    bw, gap, full = 4 * k, 2.5 * k, 18 * k
    for i, h in enumerate(heights):
        bh = max(full * h, 3 * k)
        bx = x + i * (bw + gap)
        d.rounded_rectangle((bx, cy + full / 2 - bh, bx + bw, cy + full / 2), max(k, 1), fill=color)


def _encode_eq(canvas: Image.Image, spot, vinyl: Optional["Vinyl"] = None,
               tint=None, pulse=None) -> bytes:
    """Animated WebP: only the equalizer (and the record's sheen) change between frames,
    so it stays small. tint: colour washes over the card (the hit part, an effect) to
    repeat on the record strip. pulse: (layers, x, y), cover overlays from quiet to loud,
    one picked per frame by how high the bars are (Bass boost thumps)."""
    frames = []
    strips = vinyl.strips if vinyl else []
    tints = [t for t in (tint if isinstance(tint, list) else [tint]) if t]
    for wash in tints:
        strips = [_tint_strip(st, wash) for st in strips]
    for i in range(EQ_FRAMES):
        frame = canvas.copy()
        heights = _eq_frame(i)
        _eq(frame, spot, heights)
        if strips:
            frame.alpha_composite(strips[i % len(strips)], (vinyl.x, vinyl.y))
        if pulse:
            layers, px, py = pulse
            level = heights[0]  # the first (bass) bar, 0.05 .. 0.95
            frame.alpha_composite(layers[min(int(level * len(layers)), len(layers) - 1)],
                                  (px, py))
        frames.append(frame.convert("RGB"))
    out = io.BytesIO()
    # One full picture, then only what changed: without kmin/kmax libwebp stores several
    # full key frames, about 2.5x the bytes and 3x the time for the same animation.
    frames[0].save(out, "WEBP", save_all=True, append_images=frames[1:], duration=EQ_FRAME_MS,
                   loop=0, quality=WEBP_QUALITY, method=2, minimize_size=False,
                   kmin=EQ_FRAMES, kmax=EQ_FRAMES + 1)
    return out.getvalue()


# 🎛️ effect looks: a colour wash over the card, and a pattern on the cover
FX_TINTS = {"nightcore": (255, 70, 170, 28), "slowed": (40, 80, 150, 62),
            "bass": (210, 30, 70, 24), "8d": (40, 200, 220, 20)}
COVER_RADIUS = 24
BASS_LEVELS = 3


def _cover_mask(size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    _Draw(mask).rounded_rectangle((0, 0, size, size), COVER_RADIUS, fill=255)
    return mask


def _clip_to_cover(layer: Image.Image) -> Image.Image:
    """Keep the pattern on the cover, and off the source badge in its bottom-left corner."""
    mask = _cover_mask(layer.width)
    _Draw(mask).rectangle((0, layer.height - 46, 160, layer.height), fill=0)
    layer.putalpha(ImageChops.multiply(layer.getchannel("A"), mask))
    return layer


def _sparkle(d, cx, cy, r, alpha):
    """A four-pointed star."""
    w = r * 0.22
    d.polygon([(cx, cy - r), (cx + w, cy - w), (cx + r, cy), (cx + w, cy + w), (cx, cy + r),
               (cx - w, cy + w), (cx - r, cy), (cx - w, cy - w)], fill=(255, 255, 255, alpha))


def _fx_cover(fx: str, size: int, seed: str, level: float = 0.5) -> Optional[Image.Image]:
    """The effect's pattern over the cover (size x size, clipped to its rounded corners):
    Nightcore sparkles, Slowed rain on glass, 8D sound rings from the middle,
    Bass boost speaker rings (level: how hard it thumps, for the animation)."""
    rnd = random.Random(f"{seed}|{fx}")
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = _Draw(layer)
    c = size / 2
    if fx == "nightcore":
        for _ in range(9):
            x, y = rnd.uniform(0.08, 0.92) * size, rnd.uniform(0.08, 0.92) * size
            _sparkle(d, x, y, rnd.uniform(0.025, 0.06) * size, rnd.randint(150, 230))
        for _ in range(14):  # tiny dots between the stars
            x, y, r = rnd.uniform(0, size), rnd.uniform(0, size), size * 0.006
            d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 230, 250, 200))
        glow = layer.filter(ImageFilter.GaussianBlur(size * 0.012))
        layer = Image.alpha_composite(glow, layer)
    elif fx == "slowed":
        w = max(round(size * 0.004), 1)
        for _ in range(46):  # streaks running down the glass
            x, y = rnd.uniform(0, size), rnd.uniform(-0.1, 1.0) * size
            n = rnd.uniform(0.04, 0.12) * size
            d.line((x, y, x - n * 0.18, y + n), fill=(225, 235, 255, rnd.randint(60, 120)),
                   width=w)
        for _ in range(16):  # drops
            x, y, r = rnd.uniform(0, size), rnd.uniform(0, size), rnd.uniform(0.006, 0.014) * size
            d.ellipse((x - r, y - r * 1.2, x + r, y + r * 1.2), fill=(235, 242, 255, 150))
    elif fx == "8d":
        w = max(round(size * 0.006), 1)
        for i in range(1, 6):
            r = c * i / 5.2
            d.ellipse((c - r, c - r, c + r, c + r), outline=(220, 255, 255, 120 - i * 16),
                      width=w)
        for ang in (-0.6, 2.5):  # two sounds orbiting
            x, y, r = c + c * 0.62 * math.cos(ang), c + c * 0.62 * math.sin(ang), size * 0.03
            d.ellipse((x - r, y - r, x + r, y + r), fill=(235, 255, 255, 210))
        layer = Image.alpha_composite(layer.filter(ImageFilter.GaussianBlur(size * 0.006)),
                                      layer)
    elif fx == "bass":
        w = max(round(size * (0.008 + 0.01 * level)), 1)
        for i in range(1, 4):
            r = c * (0.25 + i * 0.22) * (0.94 + 0.1 * level)
            d.ellipse((c - r, c - r, c + r, c + r),
                      outline=(255, 120, 150, int((70 + 120 * level) * (1.1 - i * 0.25))),
                      width=w)
        layer = Image.alpha_composite(layer.filter(ImageFilter.GaussianBlur(size * 0.012)),
                                      layer)
    else:
        return None
    return _clip_to_cover(layer)


def _draw_fx(canvas: Image.Image, g: Geo, fx: str, seed: str, animated: bool):
    """The effect's look on a playing card. Returns the Bass boost pulse for the animation
    (or None): the still card gets a medium thump instead."""
    tint = FX_TINTS.get(fx)
    if not tint:
        return None
    canvas.alpha_composite(Image.new("RGBA", canvas.size, tint))
    if fx == "bass" and animated:
        layers = [_fx_cover(fx, g.art, seed, (i + 0.5) / BASS_LEVELS) for i in range(BASS_LEVELS)]
        return layers, g.art_x, g.art_y
    canvas.alpha_composite(_fx_cover(fx, g.art, seed), (g.art_x, g.art_y))
    return None


# Card looks by what the clip is about (an effect's look wins over these)
_HORROR = re.compile(r"(?i)ผี|หลอน|สยอง|ลี้ลับ|อาถรรพ์|horror|ghost|haunted|creepy")
_LOVE = re.compile(r"(?i)ความรัก|รักเธอ|คิดถึง|หัวใจ|แฟน|\blove\b|❤|💕|💗")
GENRE_TINTS = {"horror": (70, 0, 8, 70), "love": (255, 120, 170, 22)}


def card_genre(track: Optional[Track]) -> str:
    """'horror' for ghost stories, 'love' for love songs, '' otherwise (by the title)."""
    title = (track.title or "") if track is not None else ""
    if _HORROR.search(title):
        return "horror"
    if _LOVE.search(title):
        return "love"
    return ""


def _draw_genre(canvas: Image.Image, g: Geo, genre: str, seed: str) -> Optional[tuple]:
    """Horror: a dark red wash and fog rising from the bottom. Love: a soft pink wash with
    a rosy glow behind the cover. Returns the wash, to repeat on the record strip."""
    tint = GENRE_TINTS.get(genre)
    if not tint:
        return None
    w, h = canvas.size
    canvas.alpha_composite(Image.new("RGBA", canvas.size, tint))
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = _Draw(layer)
    rnd = random.Random(f"{seed}|{genre}")
    if genre == "horror":
        for _ in range(14):  # fog banks along the bottom
            cx, cy = rnd.uniform(-0.1, 1.1) * w, h - rnd.uniform(0, 0.28) * h
            rx, ry = rnd.uniform(0.12, 0.3) * w, rnd.uniform(0.05, 0.12) * h
            d.ellipse((cx - rx, cy - ry, cx + rx, cy + ry),
                      fill=(200, 205, 215, rnd.randint(30, 60)))
        layer = layer.filter(ImageFilter.GaussianBlur(max(w, h) * 0.03))
    else:
        r = g.art * 0.75
        cx, cy = g.art_x + g.art / 2, g.art_y + g.art / 2
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 150, 190, 60))
        layer = layer.filter(ImageFilter.GaussianBlur(g.art * 0.18))
    canvas.alpha_composite(layer)
    return tint


def _can_animate() -> bool:
    """Animated WebP needs libwebp's mux support (feature names differ across Pillow)."""
    if EXT != "webp":
        return False
    try:
        frames = [Image.new("RGB", (2, 2), c) for c in ((0, 0, 0), (255, 255, 255))]
        out = io.BytesIO()
        frames[0].save(out, "WEBP", save_all=True, append_images=frames[1:], duration=50)
        return getattr(Image.open(io.BytesIO(out.getvalue())), "n_frames", 1) == 2
    except Exception:
        return False


ANIMATED = _can_animate()


THUMB = 26


def _draw_next(canvas: Image.Image, g: Geo, title: str, accent, bg,
               thumb: Optional[Image.Image], more: tuple = (), extra: int = 0):
    """'Up next' row: the next song's cover and title, then the covers of the songs after
    it (more) and how many others wait (+extra)."""
    d = _Draw(canvas)
    k = g.s
    size, thumb_w, gap = round(18 * k), round(THUMB * k), 8 * k
    head, body = font("Medium", size), font("Regular", size)
    prefix = "ถัดไป  "
    pw = d.textlength(prefix, font=head)
    plus = f"+{extra}" if extra > 0 else ""
    tail_w = len(more) * (thumb_w + 4 * k) + (d.textlength(plus, font=head) + 4 * k if plus else 0)
    tail_w += 2 * gap if tail_w else 0
    room = g.max_w - pw - thumb_w - gap - tail_w
    text = _fit(d, title, body, room)
    x = _row_x(g, pw + thumb_w + gap + d.textlength(text, font=body) + tail_w)
    d.text((x, g.next_y), prefix, font=head, fill=accent)
    tx = x + pw
    ty = g.next_y + k
    small = _cover(thumb, thumb_w, _mix(accent, bg, 0.4))
    canvas.alpha_composite(_rounded(small, round(6 * k)), (int(tx), int(ty)))
    d = _Draw(canvas)
    d.text((tx + thumb_w + gap, g.next_y), text, font=body, fill=_readable((215, 215, 225), bg))
    mx = tx + thumb_w + gap + d.textlength(text, font=body) + 2 * gap
    for img in more:
        cover = _cover(img, thumb_w, _mix(accent, bg, 0.4))
        canvas.alpha_composite(_rounded(cover, round(6 * k)), (int(mx), int(ty)))
        mx += thumb_w + 4 * k
    if plus:
        d = _Draw(canvas)
        d.text((mx + 2 * k, g.next_y), plus, font=head, fill=_readable((185, 185, 198), bg))


def _draw_up_next(canvas: Image.Image, g: Geo, title: str, thumb, accent, bg):
    """The song's last seconds: the next song takes the chips and 'up next' rows, big."""
    k = g.s
    size = round(64 * k)
    top = g.chips_y + 2
    cover = _rounded(_cover(thumb, size, _mix(accent, bg, 0.4)), round(10 * k))
    text_w = g.max_w - size - 16 * k
    d = _Draw(canvas)
    head, body = font("Medium", round(17 * k)), font("Bold", round(23 * k))
    name = _fit(d, title, body, text_w)
    width = size + 16 * k + max(d.textlength(name, font=body), d.textlength("ต่อไป · UP NEXT", font=head))
    x = _row_x(g, width)
    canvas.alpha_composite(cover, (int(x), int(top)))
    d = _Draw(canvas)
    tx = x + size + 16 * k
    d.text((tx, top + 4 * k), "ต่อไป · UP NEXT", font=head, fill=accent)
    d.text((tx, top + 26 * k), name, font=body, fill=WHITE)


def _draw_empty_queue(canvas: Image.Image, g: Geo, accent, bg, low: bool = False,
                      upcoming: str = "", autoplay: str = ""):
    """Fills the 'up next' row when nothing is queued: how to add a song. In the song's
    last minute (low) it turns into an amber warning that the music is about to stop.
    upcoming: what autoplay will play next (the show's next episode). autoplay: 'เพลง' or
    'คลิป', what autoplay picks when no next episode is known."""
    d = _Draw(canvas)
    k = g.s
    fnt = font("Medium" if low else "Regular", round(18 * k))
    head = font("Medium", round(18 * k))
    if upcoming or autoplay:
        b = f"Autoplay · ตอนต่อไป {upcoming}" if upcoming else f"Autoplay จะเลือก{autoplay}ต่อให้"
        a = "ถัดไป  "
        head_fill, fill = _mix(accent, bg, 0.35), _readable((215, 215, 228), bg, 4.0)
    elif low:
        a, b = "ใกล้หมดแล้ว  ", "เพลงจะหยุดในไม่ถึง 1 นาที · กด + เพิ่มเพลง"
        head_fill, fill = LOW_AMBER, _readable(LOW_AMBER, bg, 4.0)
    else:
        a, b = "ถัดไป  ", "คิวว่าง · กด + เพิ่มเพลง"  # "+" not ➕: Kanit has no emoji
        head_fill, fill = _mix(accent, bg, 0.35), _readable((150, 150, 165), bg, 3.0)
    width = d.textlength(a, font=head)
    b = _fit(d, b, fnt, g.max_w - width)
    x = _row_x(g, width + d.textlength(b, font=fnt))
    d.text((x, g.next_y), a, font=head, fill=head_fill)
    d.text((x + width, g.next_y), b, font=fnt, fill=fill)


def _right_time(track: Track, st: CardState) -> str:
    if st.time_mode == "remaining":
        return "-" + fmt_time(track.duration - st.position)
    if st.time_mode == "clock" and st.end_clock:
        return f"จบ {st.end_clock}"
    return fmt_time(track.duration)


def _draw_times(canvas: Image.Image, g: Geo, left: str, right: str, right_color=None,
                middle: str = ""):
    d = _Draw(canvas)
    size = 14 if g is MINI else round(18 * g.s)
    fnt = font("Regular", size)
    x0, total = _wave_box(g)
    if left:
        d.text((x0, g.time_y), left, font=fnt, fill=(210, 210, 220))
    if right:
        d.text((x0 + total, g.time_y), right, font=fnt, fill=right_color or (210, 210, 220),
               anchor="ra")
    if middle:  # current chapter
        mf = font("Medium", size - 2)
        text = _fit(d, middle, mf, total - 2 * max(d.textlength(left or "", font=fnt),
                                                   d.textlength(right or "", font=fnt)) - 40)
        d.text((x0 + total / 2, g.time_y + 1), text, font=mf, fill=(190, 190, 205), anchor="ma")


def _spinner(canvas: Image.Image, cx: float, cy: float, r: float):
    """A loading ring: a dark disc, a faint track and a bright arc."""
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = _Draw(layer)
    pad = r * 0.55
    d.ellipse((cx - r - pad, cy - r - pad, cx + r + pad, cy + r + pad), fill=(0, 0, 0, 150))
    box = (cx - r, cy - r, cx + r, cy + r)
    width = max(int(r * 0.22), 3)
    d.ellipse(box, outline=(255, 255, 255, 60), width=width)
    d.arc(box, -90, 30, fill=(255, 255, 255, 235), width=width)
    canvas.alpha_composite(layer)


WEBP_QUALITY = 78


def _encode(canvas: Image.Image) -> bytes:
    out = io.BytesIO()
    img = canvas.convert("RGB")
    if EXT == "webp":
        # 78: ~15% smaller than 82, no visible difference at the size Discord shows it.
        # method 2: much less CPU than the default, about the same size.
        img.save(out, "WEBP", quality=WEBP_QUALITY, method=2)
    else:
        img.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()


PAUSED_COLOR = 0.12  # colour left in a paused card
NIGHT_SHADE = (4, 4, 14, 80)
HOT_TINT = (255, 120, 30, 34)  # warm light over the card during the most replayed part
LOW_AMBER = (255, 184, 64)  # "queue almost empty" warning on the up-next row


def _grey(c) -> tuple[int, int, int]:
    v = int(0.3 * c[0] + 0.59 * c[1] + 0.11 * c[2])
    return _mix(c, (v, v, v), 1 - PAUSED_COLOR)


def _mood(canvas: Image.Image, st: CardState, accent, accent2):
    """Paused: almost no colour, so it reads as stopped at a glance (night is part of the
    base, see _render_base). Applied before the live parts are drawn. Returns accents."""
    if st.paused and st.mode == "play":
        rgb = ImageEnhance.Color(canvas.convert("RGB")).enhance(PAUSED_COLOR)
        rgb = ImageEnhance.Brightness(rgb).enhance(0.8)
        alpha = canvas.getchannel("A")
        canvas.paste(rgb.convert("RGBA"))
        canvas.putalpha(alpha)
        accent, accent2 = _grey(accent), _grey(accent2)
    return accent, accent2


def _pause_mark(canvas: Image.Image, cx: float, cy: float, r: float):
    """⏸ over the cover while paused: a dark see-through circle with two white bars."""
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = _Draw(layer)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(0, 0, 0, 120),
              outline=(255, 255, 255, 90), width=max(round(r * 0.05), 2))
    bw, bh, gap = r * 0.2, r * 0.9, r * 0.17
    for x0 in (cx - gap - bw, cx + gap):
        d.rounded_rectangle((x0, cy - bh / 2, x0 + bw, cy + bh / 2), bw * 0.35,
                            fill=(255, 255, 255, 235))
    canvas.alpha_composite(layer)


def _render_mini(track: Track, base: Base, st: CardState) -> bytes:
    g = MINI
    canvas = base.canvas.copy()
    _mood(canvas, st, base.accent, base.accent2)
    if st.paused:  # small pause mark on the cover
        d = _Draw(canvas)
        cx, cy = g.art_x + g.art / 2, g.art_y + g.art / 2
        d.ellipse((cx - 20, cy - 20, cx + 20, cy + 20), fill=(0, 0, 0))
        d.rectangle((cx - 8, cy - 9, cx - 3, cy + 9), fill=WHITE)
        d.rectangle((cx + 3, cy - 9, cx + 8, cy + 9), fill=WHITE)
    if not track.duration:
        _draw_wave(canvas, g, track, None, base.accent, base.accent2, base.bg)
        _draw_times(canvas, g, fmt_time(st.position), "LIVE", RED)
    else:
        ratio = min(max(st.position / track.duration, 0), 1)
        _draw_wave(canvas, g, track, ratio, base.accent, base.accent2, base.bg)
        _draw_times(canvas, g, fmt_time(st.position), _right_time(track, st))
    return _encode(canvas)


SERVER_ICON = 40  # px (wide card; the square card uses the same size)


def _server_icon(canvas: Image.Image, g: Geo, icon: Optional[bytes]):
    """The server's icon, round, in the top-right corner: which server this card is from."""
    img = _avatar(icon, SERVER_ICON)
    if img is None:
        return
    x, y = g.w - SERVER_ICON - 22, 18
    ring = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    _Draw(ring).ellipse((x - 2, y - 2, x + SERVER_ICON + 2, y + SERVER_ICON + 2),
                                 fill=(255, 255, 255, 70))
    canvas.alpha_composite(ring)
    canvas.alpha_composite(img, (x, y))


LISTENER = 40         # px (the server icon's size): listener avatars in the card's top row
LISTENER_STEP = 28    # they overlap
LISTENERS_SHOWN = 5
LISTENERS_MIN = 2     # one listener is the requester, already shown next to "ขอโดย"
ICON_GAP = 30         # px between the listeners and the server icon
HEADPHONES_W = 26


def _headphones(d, x: float, cy: float, color):
    """A small headphones icon (the font has no emoji)."""
    w = HEADPHONES_W
    d.arc((x + 2, cy - 12, x + w - 2, cy + 12), 180, 360, fill=color, width=3)
    d.rounded_rectangle((x, cy - 1, x + 7, cy + 12), 3, fill=color)
    d.rounded_rectangle((x + w - 7, cy - 1, x + w, cy + 12), 3, fill=color)


def _draw_listeners(canvas: Image.Image, g: Geo, avatars, total: int, has_icon: bool):
    """Who is listening: overlapping round avatars, then "+N". Wide: top right, left of the
    server icon. Square: top left (the server icon has the right)."""
    faces = [a for a in (_avatar(b, LISTENER) for b in avatars or ()) if a is not None]
    faces = faces[:LISTENERS_SHOWN]
    rest = total - len(faces)
    fnt = font("Medium", 25)
    d = _Draw(canvas)
    more = f"+{rest}" if rest > 0 else ""
    more_w = d.textlength(more, font=fnt) + 10 if more else 0
    width = (len(faces) - 1) * LISTENER_STEP + LISTENER + more_w if faces else more_w
    if not width:
        return
    y = 18
    phones = HEADPHONES_W + 8  # 🎧 in front: these are listeners, not part of the server icon
    if g.center:
        x = 22 + phones
    else:
        right = g.w - 22 - (SERVER_ICON + ICON_GAP if has_icon else 0)
        x = right - width
    _headphones(d, x - phones, y + LISTENER / 2, (225, 225, 235))
    ring = (*_mix((20, 20, 28), (0, 0, 0), 0.2), 255)
    for i, face in enumerate(faces):
        fx = int(x + i * LISTENER_STEP)
        edge = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        _Draw(edge).ellipse((fx - 2, y - 2, fx + LISTENER + 2, y + LISTENER + 2), fill=ring)
        canvas.alpha_composite(edge)
        canvas.alpha_composite(face, (fx, y))
    if more:
        d = _Draw(canvas)
        d.text((x + width - more_w + 8, y + LISTENER / 2), more, font=fnt,
               fill=(225, 225, 235), anchor="lm")


def render(track: Track, art_bytes: Optional[bytes], st: CardState = CardState(),
           next_art: Optional[bytes] = None, avatar: Optional[bytes] = None,
           icon: Optional[bytes] = None, listeners: tuple = ()) -> bytes:
    g = GEOS.get(st.layout, WIDE)
    theme = st.theme if st.theme in THEMES else "blur"
    base = _render_base(track, art_bytes, g, theme, None if g is MINI else avatar, st.night)
    if g is MINI:
        return _render_mini(track, base, st)
    g = _flow(g, base.text_bottom)
    canvas = base.canvas.crop((0, 0, g.w, g.h)) if g.h != base.canvas.height else base.canvas.copy()
    accent, accent2 = _mood(canvas, st, base.accent, base.accent2)
    pulse, genre_tint = None, None
    if st.mode == "play" and st.fx:
        pulse = _draw_fx(canvas, g, st.fx, track.url, st.animate and ANIMATED)
    elif st.mode == "play":
        genre_tint = _draw_genre(canvas, g, card_genre(track), track.url)
    if st.paused and st.mode == "play":
        _pause_mark(canvas, g.art_x + g.art / 2, g.art_y + g.art / 2, g.art * 0.17)
    bg = base.bg
    if icon and st.mode in ("play", "loading", "share"):
        _server_icon(canvas, g, icon)
    if st.mode == "play" and st.listener_count >= LISTENERS_MIN:
        _draw_listeners(canvas, g, listeners, st.listener_count, bool(icon))
    glow = theme == "neon"
    if st.mode == "error":
        canvas.alpha_composite(Image.new("RGBA", canvas.size, (120, 0, 0, 70)))
    if st.mode == "loading":  # the new song, faded, with a spinner on its cover
        canvas.alpha_composite(Image.new("RGBA", canvas.size, (8, 8, 12, 120)))
        _spinner(canvas, g.art_x + g.art / 2, g.art_y + g.art / 2, g.art * 0.13)
    eq_spot = _draw_label(canvas, g, st, track, accent)

    if st.mode == "error":
        d = _Draw(canvas)
        fnt = font("Regular", round(18 * g.s))
        y = g.chips_y
        cause, _, fix = (st.reason or "ไม่ทราบสาเหตุ").partition("\n")
        for line in _wrap(d, cause, fnt, g.max_w, 2 if fix else 3):
            _text(d, g, y, line, fnt, (255, 190, 190))
            y += 26 * g.s
        if fix:  # what to do about it, a little apart
            y += 8 * g.s
            for line in _wrap(d, "วิธีแก้: " + fix, fnt, g.max_w, 2):
                _text(d, g, y, line, fnt, (235, 235, 245))
                y += 26 * g.s
        return _encode(canvas)

    if st.mode == "loading":
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ld = _Draw(layer)
        k = g.s
        x = _row_x(g, (3 * 90 + 2 * 10) * k)
        for i in range(3):  # skeleton chips
            ld.rounded_rectangle((x + i * 100 * k, g.chips_y, x + (i * 100 + 90) * k,
                                  g.chips_y + 30 * k), 15 * k, fill=(255, 255, 255, 30))
        canvas.alpha_composite(layer)
        _draw_wave(canvas, g, track, None, accent, accent2, bg)
        return _encode(canvas)

    soft, chip_text = _chip_colors(accent)
    if st.mode == "play" and st.hot_part:  # the most replayed part: warmer, brighter
        canvas.alpha_composite(Image.new("RGBA", canvas.size, HOT_TINT))
    if st.mode == "play" and st.ending and st.next_title:
        arts = list(next_art) if isinstance(next_art, (list, tuple)) else [next_art]
        _draw_up_next(canvas, g, st.next_title, _open_art(arts[0] if arts else None), accent, bg)
    elif st.mode == "play":
        chips = [_volume_chip(st.volume, soft, chip_text)]
        if st.effect:
            chips.append(("fx", st.effect, soft, chip_text, False))
        talk = []  # after the badges: the show's episode chip says more than these
        if track.duration and track.duration >= LONG_CHIP_SECONDS:  # time left, by minute
            left = max(math.ceil((track.duration - st.position) / 60), 1)
            talk.append(("clock", f"เหลือ {left} นาที", soft, chip_text, False))
        if is_talk(track):
            talk.append(("mic", "คลิปเล่าเรื่อง", soft, chip_text, False))
        if st.hot_part:
            chips.append(("hot", "ท่อนฮิต", (255, 122, 26, 215), WHITE, False))
        chapter_now = current_chapter(track, st.position)
        if chapter_now:  # the part of the song playing now, e.g. "Chorus"
            chips.append(("chapter", _short(chapter_now, CHAPTER_CHARS), soft, chip_text, False))
        if st.loop != "off":
            chips.append(("loop", "เพลง" if st.loop == "track" else "คิว", soft, chip_text, False))
        if st.queue_len:
            chips.append(("queue", f"{st.queue_len}" + (f" · {queue_time(st.queue_secs)}"
                                                         if st.queue_secs else ""),
                          soft, chip_text, False))
        _draw_chips(canvas, g, chips + _badges(st, track) + talk
                    + _info_chips(st, soft, chip_text, track))
        if st.next_title:
            arts = list(next_art) if isinstance(next_art, (list, tuple)) else [next_art]
            more = tuple(_open_art(a) for a in arts[1:3])
            _draw_next(canvas, g, st.next_title, accent, bg, _open_art(arts[0] if arts else None),
                       more, max(st.queue_len - 1 - len(more), 0))
        elif st.loop == "off" and (st.autoplay_next or st.autoplay):
            _draw_empty_queue(canvas, g, accent, bg, upcoming=st.autoplay_next,
                              autoplay="คลิป" if is_talk(track) else "เพลง")
        elif st.loop == "off":
            _draw_empty_queue(canvas, g, accent, bg, st.queue_low)
    elif _badges(st, track):  # share card keeps the badges
        _draw_chips(canvas, g, _badges(st, track))

    chapter = current_chapter(track, st.position if st.mode == "play" else 0)
    if not track.duration:
        _draw_wave(canvas, g, track, None, accent, accent2, bg)
        _draw_times(canvas, g, fmt_time(st.position) if st.mode == "play" else "", "LIVE", RED)
    elif st.mode == "share":
        _draw_wave(canvas, g, track, 1.0, accent, accent2, bg, glow)
        _draw_times(canvas, g, "", fmt_time(track.duration))
    else:
        ratio = min(max(st.position / track.duration, 0), 1)
        _draw_wave(canvas, g, track, ratio, accent, accent2, bg, glow)
        # the chapter is a chip above when it is a playing card; the share card keeps it here
        _draw_times(canvas, g, fmt_time(st.position), _right_time(track, st),
                    middle="" if st.mode == "play" else chapter)
    if eq_spot and st.animate and ANIMATED:
        tints = [HOT_TINT if st.mode == "play" and st.hot_part else None,
                 FX_TINTS.get(st.fx) if st.mode == "play" else None, genre_tint]
        return _encode_eq(canvas, eq_spot, base.vinyl, tints, pulse)
    if pulse:  # animated look asked for, but the card ends up still
        canvas.alpha_composite(pulse[0][1], (pulse[1], pulse[2]))
    if eq_spot and st.animate:  # no animation support: still bars
        _eq(canvas, eq_spot, EQ_STILL)
    return _encode(canvas)


# --------------------------------------------------------------- quote card
QUOTE_W, QUOTE_H = 1200, 630


def _split_middle(d, text: str, fnt, max_w: float) -> Optional[list[str]]:
    """Two lines split at the space nearest the middle (Thai lyrics put spaces between
    phrases), or None when no split fits."""
    spaces = [i for i, ch in enumerate(text) if ch == " "]
    for i in sorted(spaces, key=lambda i: abs(i - len(text) / 2)):
        a, b = text[:i].strip(), text[i + 1:].strip()
        if a and b and max(d.textlength(a, font=fnt), d.textlength(b, font=fnt)) <= max_w:
            return [a, b]
    return None


QUOTE_LINES = 4  # lyric lines on one card
QUOTE_COVER = 380


def _quote_lines(d, text: list[str], max_w: float, height: float) -> tuple[list[str], int]:
    """Biggest size where every lyric line fits: whole, else split between phrases,
    else wrapped by characters as a last resort. Four lines get smaller letters than one."""
    for size in (60, 54, 48, 44, 40, 36, 32):
        fnt = font("Bold", size)
        out = []
        for ln in text:
            if d.textlength(ln, font=fnt) <= max_w:
                out.append(ln)
            elif (pair := _split_middle(d, ln, fnt, max_w)) is not None:
                out += pair
            else:
                break
        else:
            if len(out) * int(size * 1.4) <= height:
                return out, size
    fnt = font("Bold", 28)
    out = []
    for ln in text:
        out += _wrap(d, ln, fnt, max_w, 2)
    return out[:8], 28


def render_quote(lines: list[str], track: Track, art_bytes: Optional[bytes],
                 at: Optional[float] = None) -> bytes:
    """Lyric lines (up to four) as a shareable card: the cover big on the left, the words
    on the right over the blurred cover in its colours, then the song, the artist and
    when the part is sung ("ท่อนที่ 1:23")."""
    w, h = QUOTE_W, QUOTE_H
    art = _open_art(art_bytes)
    a1, a2 = _palette(art)
    if art is not None:
        canvas = ImageOps.fit(art.convert("RGB"), (w, h)).filter(
            ImageFilter.GaussianBlur(40)).convert("RGBA")
        canvas.alpha_composite(Image.new("RGBA", (w, h), (10, 10, 16, 150)))
    else:
        canvas = Image.new("RGBA", (w, h), (*_mix((14, 14, 20), a1, 0.2), 255))
    canvas.alpha_composite(_gradient((w, h), a1, a2, 80, 14))
    bg = tuple(int(v) for v in ImageStat.Stat(canvas.convert("RGB")).mean)
    accent = _readable(a1, bg)

    # the cover, big, with its own glow
    size = QUOTE_COVER
    cx, cy = 72, (h - size) // 2
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    _Draw(glow).rounded_rectangle((cx - 6, cy - 6, cx + size + 6, cy + size + 6), 30,
                                  fill=(*a1, 150))
    canvas.alpha_composite(glow.filter(ImageFilter.GaussianBlur(28)))
    _paste_cover(canvas, _cover(art, size, a1), cx, cy, radius=24)

    # the lyric, as big as fits
    tx = cx + size + 64
    max_w = w - tx - 64
    d = _Draw(canvas)
    text = [ln.strip() or "♪" for ln in lines if ln is not None][:QUOTE_LINES] or ["♪"]
    top, bottom = 70, h - 210
    wrapped, fsize = _quote_lines(d, text, max_w, bottom - top)
    fnt, lh = font("Bold", fsize), int(fsize * 1.4)
    y = top + (bottom - top - len(wrapped) * lh) / 2
    d.text((tx - 6, y - fsize * 0.75), "“", font=font("Bold", round(fsize * 1.8)), fill=accent)
    for i, ln in enumerate(wrapped):
        d.text((tx + fsize * 0.75, y + i * lh), ln, font=fnt, fill=WHITE)

    # the song under a thin line
    fy = h - 176
    d.line((tx, fy, w - 64, fy), fill=_mix(accent, bg, 0.5), width=2)
    title_f, artist_f, time_f = font("Bold", 34), font("Regular", 24), font("Medium", 20)
    d.text((tx, fy + 22), _fit(d, display_title(track), title_f, max_w), font=title_f,
           fill=WHITE)
    if display_artist(track):
        d.text((tx, fy + 68), _fit(d, display_artist(track), artist_f, max_w), font=artist_f,
               fill=_readable((200, 200, 212), bg))
    if at is not None:  # when this part is sung, as a small pill
        label = f"ท่อนที่ {fmt_time(at)}"
        pw = d.textlength(label, font=time_f) + 30
        py = fy + 112
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        _Draw(layer).rounded_rectangle((tx, py, tx + pw, py + 34), 17, fill=(*accent, 70))
        canvas.alpha_composite(layer)
        _Draw(canvas).text((tx + 15, py + 17), label, font=time_f, fill=WHITE, anchor="lm")
    return _encode(canvas)


async def make_quote_card(lines: list[str], track: Track,
                          at: Optional[float] = None) -> Optional[bytes]:
    try:
        art = await fetch_track_art(track)
        return await _run(render_quote, list(lines), track, art, at)
    except Exception as exc:
        log.warning("quote card failed: %s", exc)
        return None


# --------------------------------------------------------------- queue card
def render_queue(rows: list[dict], header: str, sub: str,
                 arts: list[Optional[bytes]]) -> bytes:
    """rows: dict(pos, title, artist, requester, duration, when)."""
    w = 1000
    row_h = 84
    h = 112 + row_h * max(len(rows), 1) + 24
    first = _open_art(arts[0]) if arts else None
    a1, a2 = _palette(first)
    canvas = Image.new("RGBA", (w, h), (*_mix((14, 14, 20), a1, 0.15), 255))
    canvas.alpha_composite(_gradient((w, h), a1, a2, 60, 20))
    bg = tuple(int(v) for v in ImageStat.Stat(canvas.convert("RGB")).mean)
    accent = _readable(a1, bg)
    d = _Draw(canvas)
    d.text((40, 30), "คิวถัดไป · UP NEXT", font=font("Medium", 19), fill=accent)
    d.text((40, 56), _fit(d, header, font("Bold", 30), w - 80), font=font("Bold", 30), fill=WHITE)
    d.text((w - 40, 66), sub, font=font("Regular", 17), fill=(200, 200, 212), anchor="ra")
    if not rows:
        d.text((40, 120), "คิวว่าง", font=font("Regular", 20), fill=(200, 200, 212))
    for i, r in enumerate(rows):
        y = 112 + i * row_h
        if i % 2 == 0:
            layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            _Draw(layer).rounded_rectangle((28, y - 6, w - 28, y + row_h - 10), 14,
                                                    fill=(255, 255, 255, 16))
            canvas.alpha_composite(layer)
        d = _Draw(canvas)
        d.text((62, y + row_h / 2 - 8), str(r["pos"]), font=font("Bold", 22), fill=accent,
               anchor="mm")
        cover = _cover(_open_art(arts[i] if i < len(arts) else None), 64, _mix(a1, bg, 0.3))
        canvas.alpha_composite(_rounded(cover, 10), (92, y))
        d = _Draw(canvas)
        right_w = 170
        d.text((172, y + 2), _fit(d, r["title"], font("Medium", 21), w - 172 - right_w - 40),
               font=font("Medium", 21), fill=WHITE)
        sub_line = " · ".join(x for x in (r.get("artist"), f"ขอโดย {r['requester']}") if x)
        d.text((172, y + 36), _fit(d, sub_line, font("Regular", 16), w - 172 - right_w - 40),
               font=font("Regular", 16), fill=(190, 190, 202))
        d.text((w - 44, y + 4), r["duration"], font=font("Medium", 18), fill=(225, 225, 232),
               anchor="ra")
        d.text((w - 44, y + 36), r["when"], font=font("Regular", 15), fill=(180, 180, 192),
               anchor="ra")
    return _encode(canvas)


# ------------------------------------------------------------ session recap
def summarize(plays: list[dict]) -> dict:
    """plays: one dict per finished track (title, url, thumbnail, requester_*, seconds)."""
    people = Counter(p["requester_name"] or "-" for p in plays if p.get("requester_id"))
    tracks = Counter(p["url"] for p in plays)
    by_url = {p["url"]: p for p in plays}
    return {
        "count": len(plays),
        "seconds": int(sum(p["seconds"] for p in plays)),
        "people": len(people),
        "top_person": people.most_common(1)[0] if people else None,
        "top_tracks": [(by_url[u], n) for u, n in tracks.most_common(3)],
    }


def _fmt_long(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h} ชม. {m} นาที" if h else f"{m} นาที"


def render_summary(stats: dict, arts: list[Optional[bytes]],
                   label: str = "สรุปเซสชัน · SESSION RECAP") -> bytes:
    w, h = 1000, 420
    first = _open_art(arts[0]) if arts else None
    a1, a2 = _palette(first)
    canvas = Image.new("RGBA", (w, h), (*_mix((14, 14, 20), a1, 0.15), 255))
    canvas.alpha_composite(_gradient((w, h), a1, a2, 60, 20))
    bg = tuple(int(v) for v in ImageStat.Stat(canvas.convert("RGB")).mean)
    accent = _readable(a1, bg)
    d = _Draw(canvas)
    d.text((40, 32), label, font=font("Medium", 19), fill=accent)
    d.text((40, 58), f"ฟังไปทั้งหมด {stats['count']} เพลง", font=font("Bold", 36), fill=WHITE)

    boxes = [(str(stats["count"]), "เพลง"), (_fmt_long(stats["seconds"]), "เวลารวม"),
             (str(stats["people"]), "คนขอเพลง")]
    bw = (w - 80 - 2 * 20) / 3
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ld = _Draw(layer)
    for i in range(3):
        x = 40 + i * (bw + 20)
        ld.rounded_rectangle((x, 122, x + bw, 206), 18, fill=(255, 255, 255, 28))
    canvas.alpha_composite(layer)
    d = _Draw(canvas)
    for i, (big, small) in enumerate(boxes):
        x = 40 + i * (bw + 20) + 22
        d.text((x, 130), _fit(d, big, font("Bold", 32), bw - 44), font=font("Bold", 32), fill=WHITE)
        d.text((x, 174), small, font=font("Regular", 17), fill=(200, 200, 212))

    if stats["top_person"]:
        name, n = stats["top_person"]
        d.text((40, 222), _fit(d, f"ขาประจำ  {name} · {n} เพลง", font("Medium", 20), w - 80),
               font=font("Medium", 20), fill=_readable((230, 230, 240), bg))

    d.text((40, 262), "เพลงยอดนิยม", font=font("Medium", 18), fill=accent)
    cw = (w - 80 - 2 * 20) / 3
    for i, (play, n) in enumerate(stats["top_tracks"]):
        x = int(40 + i * (cw + 20))
        art = _open_art(arts[i] if i < len(arts) else None)
        _paste_cover(canvas, _cover(art, 72, a1), x, 296, radius=12)
        d = _Draw(canvas)
        lines = _wrap(d, play["title"], font("Medium", 17), cw - 86, 2)
        for j, line in enumerate(lines):
            d.text((x + 84, 294 + j * 24), line, font=font("Medium", 17), fill=WHITE)
        d.text((x + 84, 294 + len(lines) * 24 + 2), f"เล่น {n} ครั้ง", font=font("Regular", 15),
               fill=(190, 190, 200))
    return _encode(canvas)


# -------------------------------------------------------------- fetching
_art_cache: "OrderedDict[str, bytes]" = OrderedDict()
_session: Optional[aiohttp.ClientSession] = None


def _http() -> aiohttp.ClientSession:
    """One shared HTTP session for cover downloads."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6))
    return _session


async def close():
    close_worker()
    if _session and not _session.closed:
        await _session.close()


async def fetch_art(url: Optional[str]) -> Optional[bytes]:
    if not url:
        return None
    if url in _art_cache:
        _art_cache.move_to_end(url)
        return _art_cache[url] or None  # b"" = known miss, do not ask again
    data = b""
    try:
        async with _http().get(url) as r:
            if r.status == 200:
                data = await r.read()
    except Exception as exc:
        log.debug("art fetch failed: %s", exc)
        return None  # network error: retry next time
    _art_cache[url] = data
    while len(_art_cache) > 200:
        _art_cache.popitem(last=False)
    return data or None


async def fetch_track_art(track: Track) -> Optional[bytes]:
    """YouTube: the HD thumbnail has no letterbox. Spotify keeps its album cover."""
    vid = track.video_id
    if vid and track.origin == "youtube":
        data = await fetch_art(f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg")
        if data:
            return data
    return await fetch_art(track.thumbnail)


# Drawing a card is a few hundred ms of CPU (12 animated frames). In a thread it competes
# with the audio sender for Python's lock (GIL) and the music stutters, e.g. when someone
# changes the volume. A worker process draws instead; falls back to threads if processes
# do not work on this system. The worker keeps its own caches (backgrounds, fonts).
_pool: Optional[ProcessPoolExecutor] = None
_pool_broken = False


def _card_pool() -> Optional[ProcessPoolExecutor]:
    global _pool
    if _pool_broken or not config.CARD_PROCESS:
        return None
    if _pool is None:
        from core.sources import lower_priority
        _pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"),
                                    initializer=lower_priority)
    return _pool


def _ready() -> bool:
    return True


def warm_worker():
    """Start the card process now (Pillow, fonts), not on the first song."""
    pool = _card_pool()
    if pool:
        pool.submit(_ready)


def close_worker():
    global _pool
    if _pool:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None


async def _run(func, *args):
    """func(*args) in the card process (or a thread). func must be a module function."""
    global _pool_broken
    loop = asyncio.get_running_loop()
    pool = _card_pool()
    if pool is not None:
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(pool, functools.partial(func, *args)), timeout=20)
        except (BrokenProcessPool, pickle.PicklingError) as exc:
            log.warning("card process failed (%s: %s), drawing in threads instead",
                        type(exc).__name__, exc)
            _pool_broken = True
            close_worker()
    return await asyncio.wait_for(
        loop.run_in_executor(None, functools.partial(func, *args)), timeout=8)


def _job_render(track, art, state, next_art, avatar, icon=None, listeners=()):
    """In the card process: the card, plus the cover colour the bot uses for the panel."""
    started = time.perf_counter()
    data = render(track, art, state, next_art, avatar, icon, listeners)
    return data, _accents.get(track.url), time.perf_counter() - started


def _job_warm(track, art, g, theme, avatar, night):
    _render_base(track, art, g, theme, avatar, night)
    return _accents.get(track.url)


def _remember_accent(track: Track, accent: Optional[int]):
    if accent is not None:
        _accents[track.url] = accent
        _accents.move_to_end(track.url)
        while len(_accents) > 200:
            _accents.popitem(last=False)


async def make_card(track: Track, state: CardState = CardState()) -> Optional[bytes]:
    try:
        await ensure_fonts(track.name, track.artist or "", state.next_title,
                           track.requester_name or "")
        art, next_art, avatar, icon, *more = await asyncio.gather(
            fetch_track_art(track), fetch_art(state.next_thumb or None),
            fetch_art(state.avatar or None), fetch_art(state.server_icon or None),
            *(fetch_art(u or None) for u in state.more_thumbs))
        if more:
            next_art = [next_art, *more]
        faces = ()
        if state.listener_count >= LISTENERS_MIN and state.mode == "play":
            faces = tuple(await asyncio.gather(*(fetch_art(u or None) for u in state.listeners)))
        data, accent, took = await _run(_job_render, track, art, state, next_art, avatar, icon,
                                        faces)
        _remember_accent(track, accent)
        if took > SLOW_RENDER:
            log.info("card took %.2fs to draw (%s%s)", took, state.layout,
                     ", animated" if state.animate else "")
        return data
    except Exception as exc:
        log.warning("card render failed: %r", exc)
        return None


async def warm(track: Track, theme: str, layout: str, avatar: str = "", night: bool = False):
    """Pre-render the static part of a card (download cover, blur, palette, title)
    before the track starts, so its panel appears without waiting."""
    try:
        art, av, _ = await asyncio.gather(fetch_track_art(track), fetch_art(avatar or None),
                                          ensure_fonts(track.name, track.artist or "",
                                                       track.requester_name or ""))
        g = GEOS.get(layout, WIDE)
        accent = await _run(_job_warm, track, art, g, theme if theme in THEMES else "blur",
                            None if g is MINI else av, night)
        _remember_accent(track, accent)
    except Exception as exc:
        log.debug("card warm-up failed: %s", exc)


async def make_queue_card(rows: list[dict], header: str, sub: str,
                          thumbs: list[Track]) -> Optional[bytes]:
    try:
        arts, _ = await asyncio.gather(
            asyncio.gather(*(fetch_track_art(t) for t in thumbs)),
            ensure_fonts(*(f"{r['title']} {r.get('artist') or ''} {r['requester']}" for r in rows)))
        return await _run(render_queue, rows, header, sub, list(arts))
    except Exception as exc:
        log.warning("queue card failed: %s", exc)
        return None


async def make_summary(plays: list[dict], label: str = "") -> Optional[bytes]:
    try:
        stats = summarize(plays)
        tracks = [Track(title=p["title"], url=p["url"], thumbnail=p.get("thumbnail"),
                        origin=p.get("origin") or "youtube") for p, _ in stats["top_tracks"]]
        arts, _ = await asyncio.gather(asyncio.gather(*(fetch_track_art(t) for t in tracks)),
                                       ensure_fonts(*(t.title for t in tracks)))
        if label:
            return await _run(render_summary, stats, list(arts), label)
        return await _run(render_summary, stats, list(arts))
    except Exception as exc:
        log.warning("summary render failed: %s", exc)
        return None
