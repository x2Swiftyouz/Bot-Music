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
import time
import unicodedata
import zlib
from collections import Counter, OrderedDict
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from typing import Optional

import aiohttp
from PIL import (Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps,
                 ImageStat, features)

import config
from core.sources import SOURCE_COLORS, Track, detect_source, fmt_time, split_feat

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
    views: int = 0              # view count chip (0 = none)
    year: str = ""              # release year chip


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


def _fit_title(draw, text: str, size: int, max_w: float, lines: int = 2, size2: int = 0):
    """Largest of size, size-4, size-8 that fits without '…'. Returns (font, lines).
    size2: a title that does not fit on one line at `size` uses two lines from size2 down."""
    if size2:
        fnt = font("Bold", size)
        if draw.textlength(text, font=fnt) <= max_w:
            return fnt, [text]
        size = size2
    for s in (size, size - 4, size - 8):
        fnt = font("Bold", s)
        wrapped = _wrap(draw, text, fnt, max_w, lines)
        if not wrapped[-1].endswith("…"):
            return fnt, wrapped
    return fnt, wrapped


def _title_parts(track: Track) -> tuple[str, str, str]:
    """(song, artists, featured) for the card: 'HK & GH - Lost or Love FT. A & B' with
    channel 'HK' -> ('Lost or Love', 'HK & GH', 'A & B')."""
    title, feat = split_feat(track.name)
    artist = (track.artist or "").strip()
    for sep in (" - ", " – ", " — "):
        left, found, right = title.partition(sep)
        if found and right.strip() and artist and (
                left.lower().startswith(artist.lower()) or artist.lower() in left.lower()):
            left, left_feat = split_feat(left.strip())
            return right.strip(), left, feat or left_feat
    return title, artist, feat


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
        return f"เล่นไม่ได้: {track.name}{who}. {st.reason}"[:1000]
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
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, *img.size), radius=radius, fill=255)
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
    elif kind == "cake":
        d.rounded_rectangle(box(0, 0, 16, 7), 2, fill=color)
        d.rectangle(box(7, -6, 9, 0), fill=color)
        d.ellipse(box(6, -10, 10, -6), fill=color)


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


def _cover(art: Optional[Image.Image], size, accent) -> Image.Image:
    w, h = (size, size) if isinstance(size, int) else size
    if art:
        return ImageOps.fit(art, (w, h))
    cover = Image.new("RGB", (w, h), accent)
    s = min(w, h)
    _note(ImageDraw.Draw(cover), (w - s * 0.42) / 2, (h - s * 0.42) / 2, s * 0.42, WHITE)
    return cover


def _shadowed(canvas: Image.Image, img: Image.Image, x: int, y: int, radius: int = 24):
    """Paste an RGBA image with a soft drop shadow."""
    w, h = img.size
    shadow = Image.new("RGBA", (w + 40, h + 40), (0, 0, 0, 0))
    alpha = img.getchannel("A").point(lambda a: 150 if a else 0)
    shadow.paste((0, 0, 0, 255), (20, 24), alpha)
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(10)), (x - 20, y - 20))
    canvas.alpha_composite(img, (x, y))


def _paste_cover(canvas: Image.Image, cover: Image.Image, x: int, y: int, radius: int = 24):
    _shadowed(canvas, _rounded(cover, radius), x, y, radius)


def _source_badge(img: Image.Image, x: float, y: float, track: Track):
    key = track.origin if track.origin in SOURCE_COLORS else detect_source(track.url)
    name = SOURCE_NAMES.get(key)
    if not name:
        return
    d = ImageDraw.Draw(img)
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
def _art_plain(canvas, g: Geo, art, a1, track):
    _paste_cover(canvas, _cover(art, g.art, a1), g.art_x, g.art_y)
    _source_badge(canvas, g.art_x + 12, g.art_y + g.art - 38, track)


def _art_neon(canvas, g: Geo, art, a1, track):
    box = (g.art_x - 2, g.art_y - 2, g.art_x + g.art + 2, g.art_y + g.art + 2)
    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).rounded_rectangle(box, 26, outline=(*a1, 255), width=8)
    canvas.alpha_composite(glow.filter(ImageFilter.GaussianBlur(14)))
    canvas.alpha_composite(_rounded(_cover(art, g.art, a1), 24), (g.art_x, g.art_y))
    ImageDraw.Draw(canvas).rounded_rectangle(box, 26, outline=_mix(a1, WHITE, 0.35), width=3)
    _source_badge(canvas, g.art_x + 12, g.art_y + g.art - 38, track)


def _art_polaroid(canvas, g: Geo, art, a1, track):
    s = g.art
    pad, bottom = max(s // 22, 10), max(s // 6, 34)
    frame = Image.new("RGBA", (s, s), (246, 243, 236, 255))
    photo = _cover(art, (s - 2 * pad, s - pad - bottom), a1)
    frame.paste(photo, (pad, pad))
    d = ImageDraw.Draw(frame)
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
    d = ImageDraw.Draw(body)
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
    _text(ImageDraw.Draw(layer), g, y, text, fnt, (*color, 255))
    canvas.alpha_composite(layer.filter(ImageFilter.GaussianBlur(7)))


@dataclass
class Base:
    canvas: Image.Image
    accent: tuple
    accent2: tuple
    bg: tuple
    text_bottom: int


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
    ImageDraw.Draw(mask).ellipse((0, 0, *img.size), fill=255)
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
           len(art_bytes or b""), len(avatar_bytes or b""), g, theme, night)
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
    if g is MINI:
        _paste_cover(canvas, _cover(art, g.art, a1), g.art_x, g.art_y, radius=14)
    else:
        ART_BLOCKS.get(theme, _art_plain)(canvas, g, art, a1, track)

    if night:  # darker background and cover; the text drawn next stays bright
        canvas.alpha_composite(Image.new("RGBA", canvas.size, NIGHT_SHADE))

    # Text colours checked against the real background behind the text column.
    region = (0, g.label_y, g.w, g.h) if g.center else (g.x0, 0, g.w, g.h)
    bg = tuple(int(v) for v in ImageStat.Stat(canvas.crop(region).convert("RGB")).mean)
    accent = _readable(a1, bg)
    accent2 = _readable(a2, bg)

    d = ImageDraw.Draw(canvas)
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
        y = g.title_y
        for line in lines:
            if theme == "neon":
                _glow_text(canvas, g, y, line, title_font, a1)
                d = ImageDraw.Draw(canvas)
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
            d = ImageDraw.Draw(canvas)
            base = Base(canvas, accent, accent2, bg, y + 32)

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


def _draw_wave(canvas: Image.Image, g: Geo, track: Track, ratio: Optional[float], c1, c2,
               bg, glow: bool = False):
    """ratio None = everything dim (live / loading), 1.0 = everything lit (share card)."""
    n = g.max_w // (WAVE_BAR + WAVE_GAP)
    x0, total = _wave_box(g)
    cy = g.wave_y + g.wave_h / 2
    heat = tuple(track._heatmap or ())
    heights = _wave_heights(track.video_id or track.url, n, heat)
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    lit = []
    for i, hgt in enumerate(heights):
        x = x0 + i * (WAVE_BAR + WAVE_GAP)
        half = max(hgt * g.wave_h / 2, 2)
        box = (x, cy - half, x + WAVE_BAR, cy + half)
        if ratio is not None and (i + 0.5) / n <= ratio:
            lit.append((box, _mix(c1, c2, i / max(n - 1, 1))))
        else:
            ld.rounded_rectangle(box, 2, fill=(255, 255, 255, 70))
    canvas.alpha_composite(layer)
    if glow and lit:
        gl = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(gl)
        for box, color in lit:
            gd.rounded_rectangle(box, 2, fill=(*color, 255))
        canvas.alpha_composite(gl.filter(ImageFilter.GaussianBlur(6)))
    d = ImageDraw.Draw(canvas)
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
        px = x0 + total * ratio
        d.rounded_rectangle((px - 1, g.wave_y - 4, px + 1, g.wave_y + g.wave_h + 4), 1, fill=WHITE)


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
    d = ImageDraw.Draw(canvas)
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
    ld = ImageDraw.Draw(layer)
    spots = []
    for (icon, text, fill, color, muted), w in zip(chips, sizes):
        ld.rounded_rectangle((x, y0, x + w, y1), 15 * k, fill=fill)
        spots.append((x + 13 * k, icon, text, color, muted))
        x += w + gap
    canvas.alpha_composite(layer)
    d = ImageDraw.Draw(canvas)
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


def _volume_chip(volume: int, soft=(255, 255, 255, 38), text=(240, 240, 245)) -> tuple:
    if volume >= 130:
        fill, color = (*RED, 215), WHITE
    elif volume > 100:
        fill, color = (*ORANGE, 205), WHITE
    else:
        fill, color = soft, text
    return ("vol", f"{volume}%", fill, color, volume == 0)


def short_count(n: int) -> str:
    """1234 -> 1.2K, 3400000 -> 3.4M, 1200000000 -> 1.2B."""
    for size, unit in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= size:
            v = n / size
            text = f"{v:.1f}".rstrip("0").rstrip(".") if v < 10 else f"{v:.0f}"
            return text + unit
    return str(n)


def _info_chips(st: CardState, fill, text) -> list[tuple]:
    """Views and year: nice to know, so they are the first to go when space runs out."""
    out = []
    if st.views:
        out.append(("play", short_count(st.views), fill, text, False))
    if st.year:
        out.append(("cal", st.year, fill, text, False))
    return out


def _badges(st: CardState) -> list[tuple]:
    out = []
    if st.hot >= HOT_THRESHOLD:
        out.append(("hot", f"ฮิต ×{st.hot}", (255, 122, 26, 215), WHITE, False))
    if st.birthday:
        out.append(("cake", "วันเกิดคนขอ!", (235, 69, 158, 215), WHITE, False))
    return out


def _draw_label(canvas: Image.Image, g: Geo, st: CardState, track: Track, accent):
    d = ImageDraw.Draw(canvas)
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
EQ_FRAMES = 12
EQ_FRAME_MS = 110


def _eq_frame(i: int) -> tuple:
    t = i / EQ_FRAMES * 2 * math.pi
    return tuple(0.5 + 0.45 * math.sin(t * m + p) for m, p in ((1, 0.0), (2, 1.9), (1, 3.7), (2, 5.1)))


def _eq(canvas: Image.Image, spot, heights):
    x, cy, k, color = spot
    d = ImageDraw.Draw(canvas)
    bw, gap, full = 4 * k, 2.5 * k, 18 * k
    for i, h in enumerate(heights):
        bh = max(full * h, 3 * k)
        bx = x + i * (bw + gap)
        d.rounded_rectangle((bx, cy + full / 2 - bh, bx + bw, cy + full / 2), max(k, 1), fill=color)


def _encode_eq(canvas: Image.Image, spot) -> bytes:
    """Animated WebP: only the equalizer changes between frames, so it stays small."""
    frames = []
    for i in range(EQ_FRAMES):
        frame = canvas.copy()
        _eq(frame, spot, _eq_frame(i))
        frames.append(frame.convert("RGB"))
    out = io.BytesIO()
    frames[0].save(out, "WEBP", save_all=True, append_images=frames[1:], duration=EQ_FRAME_MS,
                   loop=0, quality=82, method=2, minimize_size=False)  # 2: ~40% less CPU
    return out.getvalue()


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
               thumb: Optional[Image.Image]):
    """'Up next' row with a small cover in front of the title."""
    d = ImageDraw.Draw(canvas)
    k = g.s
    size, thumb_w, gap = round(18 * k), round(THUMB * k), 8 * k
    head, body = font("Medium", size), font("Regular", size)
    prefix = "ถัดไป  "
    pw = d.textlength(prefix, font=head)
    room = g.max_w - pw - thumb_w - gap
    text = _fit(d, title, body, room)
    x = _row_x(g, pw + thumb_w + gap + d.textlength(text, font=body))
    d.text((x, g.next_y), prefix, font=head, fill=accent)
    tx = x + pw
    ty = g.next_y + k
    small = _cover(thumb, thumb_w, _mix(accent, bg, 0.4))
    canvas.alpha_composite(_rounded(small, round(6 * k)), (int(tx), int(ty)))
    d = ImageDraw.Draw(canvas)
    d.text((tx + thumb_w + gap, g.next_y), text, font=body, fill=_readable((215, 215, 225), bg))


def _draw_empty_queue(canvas: Image.Image, g: Geo, accent, bg):
    """Fills the 'up next' row when nothing is queued: how to add a song."""
    d = ImageDraw.Draw(canvas)
    k = g.s
    fnt = font("Regular", round(18 * k))
    head = font("Medium", round(18 * k))
    a, b = "ถัดไป  ", "คิวว่าง · กด ➕ เพิ่มเพลง"
    b = b.replace("➕ ", "+ ")  # Kanit has no emoji
    x = _row_x(g, d.textlength(a, font=head) + d.textlength(b, font=fnt))
    d.text((x, g.next_y), a, font=head, fill=_mix(accent, bg, 0.35))
    d.text((x + d.textlength(a, font=head), g.next_y), b, font=fnt,
           fill=_readable((150, 150, 165), bg, 3.0))


def _right_time(track: Track, st: CardState) -> str:
    if st.time_mode == "remaining":
        return "-" + fmt_time(track.duration - st.position)
    if st.time_mode == "clock" and st.end_clock:
        return f"จบ {st.end_clock}"
    return fmt_time(track.duration)


def _draw_times(canvas: Image.Image, g: Geo, left: str, right: str, right_color=None,
                middle: str = ""):
    d = ImageDraw.Draw(canvas)
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
    d = ImageDraw.Draw(layer)
    pad = r * 0.55
    d.ellipse((cx - r - pad, cy - r - pad, cx + r + pad, cy + r + pad), fill=(0, 0, 0, 150))
    box = (cx - r, cy - r, cx + r, cy + r)
    width = max(int(r * 0.22), 3)
    d.ellipse(box, outline=(255, 255, 255, 60), width=width)
    d.arc(box, -90, 30, fill=(255, 255, 255, 235), width=width)
    canvas.alpha_composite(layer)


def _encode(canvas: Image.Image) -> bytes:
    out = io.BytesIO()
    img = canvas.convert("RGB")
    if EXT == "webp":
        img.save(out, "WEBP", quality=82, method=4)
    else:
        img.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()


PAUSED_COLOR = 0.12  # colour left in a paused card
NIGHT_SHADE = (4, 4, 14, 80)


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


def _render_mini(track: Track, base: Base, st: CardState) -> bytes:
    g = MINI
    canvas = base.canvas.copy()
    _mood(canvas, st, base.accent, base.accent2)
    if st.paused:  # small pause mark on the cover
        d = ImageDraw.Draw(canvas)
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


def render(track: Track, art_bytes: Optional[bytes], st: CardState = CardState(),
           next_art: Optional[bytes] = None, avatar: Optional[bytes] = None) -> bytes:
    g = GEOS.get(st.layout, WIDE)
    theme = st.theme if st.theme in THEMES else "blur"
    base = _render_base(track, art_bytes, g, theme, None if g is MINI else avatar, st.night)
    if g is MINI:
        return _render_mini(track, base, st)
    g = _flow(g, base.text_bottom)
    canvas = base.canvas.crop((0, 0, g.w, g.h)) if g.h != base.canvas.height else base.canvas.copy()
    accent, accent2 = _mood(canvas, st, base.accent, base.accent2)
    bg = base.bg
    glow = theme == "neon"
    if st.mode == "error":
        canvas.alpha_composite(Image.new("RGBA", canvas.size, (120, 0, 0, 70)))
    if st.mode == "loading":  # the new song, faded, with a spinner on its cover
        canvas.alpha_composite(Image.new("RGBA", canvas.size, (8, 8, 12, 120)))
        _spinner(canvas, g.art_x + g.art / 2, g.art_y + g.art / 2, g.art * 0.13)
    eq_spot = _draw_label(canvas, g, st, track, accent)

    if st.mode == "error":
        d = ImageDraw.Draw(canvas)
        fnt = font("Regular", round(18 * g.s))
        y = g.chips_y
        for line in _wrap(d, st.reason or "ไม่ทราบสาเหตุ", fnt, g.max_w, 3):
            _text(d, g, y, line, fnt, (255, 190, 190))
            y += 26 * g.s
        return _encode(canvas)

    if st.mode == "loading":
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        k = g.s
        x = _row_x(g, (3 * 90 + 2 * 10) * k)
        for i in range(3):  # skeleton chips
            ld.rounded_rectangle((x + i * 100 * k, g.chips_y, x + (i * 100 + 90) * k,
                                  g.chips_y + 30 * k), 15 * k, fill=(255, 255, 255, 30))
        canvas.alpha_composite(layer)
        _draw_wave(canvas, g, track, None, accent, accent2, bg)
        return _encode(canvas)

    soft, chip_text = _chip_colors(accent)
    if st.mode == "play":
        chips = [_volume_chip(st.volume, soft, chip_text)]
        if st.loop != "off":
            chips.append(("loop", "เพลง" if st.loop == "track" else "คิว", soft, chip_text, False))
        if st.queue_len:
            chips.append(("queue", f"{st.queue_len}", soft, chip_text, False))
        _draw_chips(canvas, g, chips + _badges(st) + _info_chips(st, soft, chip_text))
        if st.next_title:
            _draw_next(canvas, g, st.next_title, accent, bg, _open_art(next_art))
        elif st.loop == "off":
            _draw_empty_queue(canvas, g, accent, bg)
    elif _badges(st):  # share card keeps the badges
        _draw_chips(canvas, g, _badges(st))

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
        _draw_times(canvas, g, fmt_time(st.position), _right_time(track, st), middle=chapter)
    if eq_spot and st.animate and ANIMATED:
        return _encode_eq(canvas, eq_spot)
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


def _quote_lines(d, text: list[str], max_w: float, height: float) -> tuple[list[str], int]:
    """Biggest size where every lyric line fits: whole, else split between phrases,
    else wrapped by characters as a last resort."""
    for size in (60, 54, 48, 42):
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
            if len(out) * int(size * 1.45) <= height:
                return out, size
    fnt = font("Bold", 36)
    out = []
    for ln in text:
        out += _wrap(d, ln, fnt, max_w, 3)
    return out[:5], 36


def render_quote(lines: list[str], track: Track, art_bytes: Optional[bytes]) -> bytes:
    """A lyric line (or two) as a shareable card: big text over the blurred cover,
    with the cover, title and artist at the bottom."""
    w, h = QUOTE_W, QUOTE_H
    art = _open_art(art_bytes)
    a1, a2 = _palette(art)
    if art is not None:
        canvas = ImageOps.fit(art.convert("RGB"), (w, h)).filter(
            ImageFilter.GaussianBlur(36)).convert("RGBA")
        canvas.alpha_composite(Image.new("RGBA", (w, h), (10, 10, 16, 150)))
    else:
        canvas = Image.new("RGBA", (w, h), (*_mix((14, 14, 20), a1, 0.2), 255))
    canvas.alpha_composite(_gradient((w, h), a1, a2, 70, 10))
    bg = tuple(int(v) for v in ImageStat.Stat(canvas.convert("RGB")).mean)
    accent = _readable(a1, bg)
    d = ImageDraw.Draw(canvas)

    # the lyric: as big as fits in four lines
    text = [ln.strip() or "♪" for ln in lines if ln is not None][:2] or ["♪"]
    max_w, top, bottom = w - 240, 90, h - 190
    wrapped, size = _quote_lines(d, text, max_w, bottom - top)
    fnt, lh = font("Bold", size), int(size * 1.45)
    y = top + (bottom - top - len(wrapped) * lh) / 2
    d.text((100 - 8, y - size * 0.9), "“", font=font("Bold", size * 2), fill=accent)
    for i, ln in enumerate(wrapped):
        d.text((100 + 40, y + i * lh), ln, font=fnt, fill=WHITE)

    # footer: cover, title, artist
    s_ = 104
    cover = _rounded(_cover(art, s_, a1), 14)
    fy = h - 60 - s_
    canvas.alpha_composite(cover, (100, fy))
    d = ImageDraw.Draw(canvas)
    tx = 100 + s_ + 24
    title_f, artist_f = font("Bold", 32), font("Regular", 24)
    d.text((tx, fy + 14), _fit(d, display_title(track), title_f, w - tx - 100), font=title_f,
           fill=WHITE)
    if display_artist(track):
        d.text((tx, fy + 60), _fit(d, display_artist(track), artist_f, w - tx - 100), font=artist_f,
               fill=_readable((200, 200, 212), bg))
    d.line((100, fy - 28, w - 100, fy - 28), fill=(*_mix(accent, bg, 0.5), ), width=2)
    return _encode(canvas)


async def make_quote_card(lines: list[str], track: Track) -> Optional[bytes]:
    try:
        art = await fetch_track_art(track)
        return await _run(render_quote, list(lines), track, art)
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
    d = ImageDraw.Draw(canvas)
    d.text((40, 30), "คิวถัดไป · UP NEXT", font=font("Medium", 19), fill=accent)
    d.text((40, 56), _fit(d, header, font("Bold", 30), w - 80), font=font("Bold", 30), fill=WHITE)
    d.text((w - 40, 66), sub, font=font("Regular", 17), fill=(200, 200, 212), anchor="ra")
    if not rows:
        d.text((40, 120), "คิวว่าง", font=font("Regular", 20), fill=(200, 200, 212))
    for i, r in enumerate(rows):
        y = 112 + i * row_h
        if i % 2 == 0:
            layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            ImageDraw.Draw(layer).rounded_rectangle((28, y - 6, w - 28, y + row_h - 10), 14,
                                                    fill=(255, 255, 255, 16))
            canvas.alpha_composite(layer)
        d = ImageDraw.Draw(canvas)
        d.text((62, y + row_h / 2 - 8), str(r["pos"]), font=font("Bold", 22), fill=accent,
               anchor="mm")
        cover = _cover(_open_art(arts[i] if i < len(arts) else None), 64, _mix(a1, bg, 0.3))
        canvas.alpha_composite(_rounded(cover, 10), (92, y))
        d = ImageDraw.Draw(canvas)
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


def render_summary(stats: dict, arts: list[Optional[bytes]]) -> bytes:
    w, h = 1000, 420
    first = _open_art(arts[0]) if arts else None
    a1, a2 = _palette(first)
    canvas = Image.new("RGBA", (w, h), (*_mix((14, 14, 20), a1, 0.15), 255))
    canvas.alpha_composite(_gradient((w, h), a1, a2, 60, 20))
    bg = tuple(int(v) for v in ImageStat.Stat(canvas.convert("RGB")).mean)
    accent = _readable(a1, bg)
    d = ImageDraw.Draw(canvas)
    d.text((40, 32), "สรุปเซสชัน · SESSION RECAP", font=font("Medium", 19), fill=accent)
    d.text((40, 58), f"ฟังไปทั้งหมด {stats['count']} เพลง", font=font("Bold", 36), fill=WHITE)

    boxes = [(str(stats["count"]), "เพลง"), (_fmt_long(stats["seconds"]), "เวลารวม"),
             (str(stats["people"]), "คนขอเพลง")]
    bw = (w - 80 - 2 * 20) / 3
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    for i in range(3):
        x = 40 + i * (bw + 20)
        ld.rounded_rectangle((x, 122, x + bw, 206), 18, fill=(255, 255, 255, 28))
    canvas.alpha_composite(layer)
    d = ImageDraw.Draw(canvas)
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
        d = ImageDraw.Draw(canvas)
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


def _job_render(track, art, state, next_art, avatar):
    """In the card process: the card, plus the cover colour the bot uses for the panel."""
    started = time.perf_counter()
    data = render(track, art, state, next_art, avatar)
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
        art, next_art, avatar = await asyncio.gather(
            fetch_track_art(track), fetch_art(state.next_thumb or None),
            fetch_art(state.avatar or None))
        data, accent, took = await _run(_job_render, track, art, state, next_art, avatar)
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
        art, av = await asyncio.gather(fetch_track_art(track), fetch_art(avatar or None))
        g = GEOS.get(layout, WIDE)
        accent = await _run(_job_warm, track, art, g, theme if theme in THEMES else "blur",
                            None if g is MINI else av, night)
        _remember_accent(track, accent)
    except Exception as exc:
        log.debug("card warm-up failed: %s", exc)


async def make_queue_card(rows: list[dict], header: str, sub: str,
                          thumbs: list[Track]) -> Optional[bytes]:
    try:
        arts = await asyncio.gather(*(fetch_track_art(t) for t in thumbs))
        return await _run(render_queue, rows, header, sub, list(arts))
    except Exception as exc:
        log.warning("queue card failed: %s", exc)
        return None


async def make_summary(plays: list[dict]) -> Optional[bytes]:
    try:
        stats = summarize(plays)
        tracks = [Track(title=p["title"], url=p["url"], thumbnail=p.get("thumbnail"),
                        origin=p.get("origin") or "youtube") for p, _ in stats["top_tracks"]]
        arts = await asyncio.gather(*(fetch_track_art(t) for t in tracks))
        return await _run(render_summary, stats, list(arts))
    except Exception as exc:
        log.warning("summary render failed: %s", exc)
        return None
