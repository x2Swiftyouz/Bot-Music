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
import os
import random
import unicodedata
import zlib
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Optional

import aiohttp
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat, features

import config
from core.sources import SOURCE_COLORS, Track, detect_source, fmt_time

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


WIDE = Geo(w=1000, h=446, art=350, art_x=40, art_y=48, x0=430, max_w=530, center=False,
           label_y=40, title_y=72, title_size=38, chips_y=250, next_y=292,
           wave_y=336, wave_h=40, time_y=382)
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


def _fit_title(draw, text: str, size: int, max_w: float, lines: int = 2):
    """Largest of size, size-4, size-8 that fits without '…'. Returns (font, lines)."""
    for s in (size, size - 4, size - 8):
        fnt = font("Bold", s)
        wrapped = _wrap(draw, text, fnt, max_w, lines)
        if not wrapped[-1].endswith("…"):
            return fnt, wrapped
    return fnt, wrapped


def display_title(track: Track) -> str:
    """'Artist - Song' becomes 'Song' when the artist line already shows the artist."""
    artist = (track.artist or "").strip()
    title = track.title
    for sep in (" - ", " – ", " — "):
        if artist and title.lower().startswith(artist.lower() + sep):
            return title[len(artist) + len(sep):].strip() or title
    return title


def alt_text(track: Track, st: CardState) -> str:
    """Screen-reader description of a card (Discord attachment alt text)."""
    who = f" โดย {track.artist}" if track.artist else ""
    if st.mode == "error":
        return f"เล่นไม่ได้: {track.title}{who}. {st.reason}"[:1000]
    if st.mode == "loading":
        return f"กำลังโหลด {track.title}{who}"[:1000]
    if not track.duration:
        timing = "ถ่ายทอดสด"
    elif st.mode == "share":
        timing = f"ความยาว {fmt_time(track.duration)}"
    else:
        timing = f"{fmt_time(st.position)} จาก {fmt_time(track.duration)}"
    state = "หยุดชั่วคราว" if st.paused else ("กำลังฟัง" if st.mode == "share" else "กำลังเล่น")
    text = f"{state} {track.title}{who}, {timing}, ขอโดย {track.requester_name or '-'}"
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


def _icon(d: ImageDraw.ImageDraw, kind: str, x: float, cy: float, color, muted=False):
    """Small chip icons drawn with shapes (Kanit has no emoji)."""
    if kind == "vol":
        d.rectangle((x, cy - 3, x + 4, cy + 3), fill=color)
        d.polygon([(x + 4, cy - 3), (x + 9, cy - 7), (x + 9, cy + 7), (x + 4, cy + 3)], fill=color)
        if muted:
            d.line((x + 11, cy - 4, x + 16, cy + 4), fill=color, width=2)
            d.line((x + 11, cy + 4, x + 16, cy - 4), fill=color, width=2)
        else:
            d.arc((x + 5, cy - 6, x + 15, cy + 6), -55, 55, fill=color, width=2)
    elif kind == "loop":
        d.arc((x, cy - 7, x + 15, cy + 7), 20, 320, fill=color, width=2)
        d.polygon([(x + 10, cy - 8), (x + 17, cy - 8), (x + 14, cy - 2)], fill=color)
    elif kind == "queue":
        for i in range(3):
            d.rounded_rectangle((x, cy - 6 + i * 5, x + 15 - i * 3, cy - 4 + i * 5), 1, fill=color)
    elif kind == "hot":
        d.ellipse((x + 2, cy - 2, x + 14, cy + 8), fill=color)
        d.polygon([(x + 2, cy + 3), (x + 9, cy - 9), (x + 14, cy + 3)], fill=color)
    elif kind == "cake":
        d.rounded_rectangle((x, cy, x + 16, cy + 7), 2, fill=color)
        d.rectangle((x + 7, cy - 6, x + 9, cy), fill=color)
        d.ellipse((x + 6, cy - 10, x + 10, cy - 6), fill=color)


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


def _render_base(track: Track, art_bytes: Optional[bytes], g: Geo, theme: str) -> Base:
    """Static part (background, cover, title, artist, requester). Cached per track."""
    key = (track.url, track.title, track.artist, track.requester_name,
           len(art_bytes or b""), g, theme)
    if key in _base_cache:
        _base_cache.move_to_end(key)
        return _base_cache[key]
    art = _open_art(art_bytes)
    a1, a2 = _palette(art)
    canvas = _background(g, theme, art, a1, a2)
    if g is MINI:
        _paste_cover(canvas, _cover(art, g.art, a1), g.art_x, g.art_y, radius=14)
    else:
        ART_BLOCKS.get(theme, _art_plain)(canvas, g, art, a1, track)

    # Text colours checked against the real background behind the text column.
    region = (0, g.label_y, g.w, g.h) if g.center else (g.x0, 0, g.w, g.h)
    bg = tuple(int(v) for v in ImageStat.Stat(canvas.crop(region).convert("RGB")).mean)
    accent = _readable(a1, bg)
    accent2 = _readable(a2, bg)

    d = ImageDraw.Draw(canvas)
    if g is MINI:
        fnt = font("Bold", g.title_size)
        d.text((g.x0, g.title_y), _fit(d, display_title(track), fnt, g.max_w), font=fnt, fill=WHITE)
        sub = " · ".join(x for x in (track.artist, f"ขอโดย {track.requester_name or '-'}") if x)
        small = font("Regular", 15)
        d.text((g.x0, g.title_y + 32), _fit(d, sub, small, g.max_w), font=small,
               fill=_readable((200, 200, 212), bg))
        base = Base(canvas, accent, accent2, bg, g.h)
    else:
        title_font, lines = _fit_title(d, display_title(track), g.title_size, g.max_w)
        lh = int(title_font.size * 1.3)
        y = g.title_y
        for line in lines:
            if theme == "neon":
                _glow_text(canvas, g, y, line, title_font, a1)
                d = ImageDraw.Draw(canvas)
            _text(d, g, y, line, title_font, WHITE)
            y += lh
        if track.artist:
            fnt = font("Medium", 23)
            _text(d, g, y + 2, _fit(d, track.artist, fnt, g.max_w), fnt,
                  _readable((225, 225, 235), bg))
            y += 32
        fnt = font("Regular", 19)
        _text(d, g, y + 4, _fit(d, f"ขอโดย {track.requester_name or '-'}", fnt, g.max_w), fnt,
              _readable((185, 185, 198), bg))
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
    fnt = font("Medium", 17)
    d = ImageDraw.Draw(canvas)
    sizes = []
    for icon, text, *_ in chips:
        sizes.append((ICON_W + 8 if icon else 0) + d.textlength(text, font=fnt) + 26)
    while chips and sum(sizes) + 10 * (len(sizes) - 1) > g.max_w:
        chips, sizes = chips[:-1], sizes[:-1]
    if not chips:
        return
    x = _row_x(g, sum(sizes) + 10 * (len(sizes) - 1))
    y0, y1 = g.chips_y, g.chips_y + 30
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    spots = []
    for (icon, text, fill, color, muted), w in zip(chips, sizes):
        ld.rounded_rectangle((x, y0, x + w, y1), 15, fill=fill)
        spots.append((x + 13, icon, text, color, muted))
        x += w + 10
    canvas.alpha_composite(layer)
    d = ImageDraw.Draw(canvas)
    cy = (y0 + y1) / 2
    for tx, icon, text, color, muted in spots:
        if icon:
            _icon(d, icon, tx, cy, color, muted)
            tx += ICON_W + 8
        d.text((tx, cy), text, font=fnt, fill=color, anchor="lm")


def _volume_chip(volume: int) -> tuple:
    if volume >= 130:
        fill = (*RED, 215)
    elif volume > 100:
        fill = (*ORANGE, 205)
    else:
        fill = (255, 255, 255, 38)
    color = WHITE if volume > 100 else (240, 240, 245)
    return ("vol", f"{volume}%", fill, color, volume == 0)


def _badges(st: CardState) -> list[tuple]:
    out = []
    if st.hot >= HOT_THRESHOLD:
        out.append(("hot", f"ฮิต ×{st.hot}", (255, 122, 26, 215), WHITE, False))
    if st.birthday:
        out.append(("cake", "วันเกิดคนขอ!", (235, 69, 158, 215), WHITE, False))
    return out


def _draw_label(canvas: Image.Image, g: Geo, st: CardState, track: Track, accent):
    d = ImageDraw.Draw(canvas)
    fnt = font("Medium", 19)
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
        text, color, icon = "กำลังเล่น · NOW PLAYING", accent, None
    if live and st.mode in ("play", "share"):
        text = "LIVE · " + text
        icon = icon or "live"
    icon_w = {"pause": 24, "dots": 30, "live": 22}.get(icon, 0)
    x = _row_x(g, icon_w + d.textlength(text, font=fnt))
    cy = g.label_y + 12
    if icon == "pause":
        d.rectangle((x, cy - 8, x + 5, cy + 8), fill=color)
        d.rectangle((x + 10, cy - 8, x + 15, cy + 8), fill=color)
    elif icon == "dots":
        for i in range(3):
            d.ellipse((x + i * 8, cy - 3, x + i * 8 + 6, cy + 3), fill=color)
    elif icon == "live":
        if st.blink:  # alternates every refresh: a slow blink
            d.ellipse((x + 1, cy - 6, x + 13, cy + 6), fill=RED)
        else:
            d.ellipse((x + 1, cy - 6, x + 13, cy + 6), outline=RED, width=2)
    d.text((x + icon_w, cy), text, font=fnt, fill=color, anchor="lm")


THUMB = 26


def _draw_next(canvas: Image.Image, g: Geo, title: str, accent, bg,
               thumb: Optional[Image.Image]):
    """'Up next' row with a small cover in front of the title."""
    d = ImageDraw.Draw(canvas)
    head, body = font("Medium", 18), font("Regular", 18)
    prefix = "ถัดไป  "
    pw = d.textlength(prefix, font=head)
    room = g.max_w - pw - THUMB - 8
    text = _fit(d, title, body, room)
    x = _row_x(g, pw + THUMB + 8 + d.textlength(text, font=body))
    d.text((x, g.next_y), prefix, font=head, fill=accent)
    tx = x + pw
    ty = g.next_y + 1
    small = _cover(thumb, THUMB, _mix(accent, bg, 0.4))
    canvas.alpha_composite(_rounded(small, 6), (int(tx), int(ty)))
    d = ImageDraw.Draw(canvas)
    d.text((tx + THUMB + 8, g.next_y), text, font=body, fill=_readable((215, 215, 225), bg))


def _right_time(track: Track, st: CardState) -> str:
    if st.time_mode == "remaining":
        return "-" + fmt_time(track.duration - st.position)
    if st.time_mode == "clock" and st.end_clock:
        return f"จบ {st.end_clock}"
    return fmt_time(track.duration)


def _draw_times(canvas: Image.Image, g: Geo, left: str, right: str, right_color=None,
                middle: str = ""):
    d = ImageDraw.Draw(canvas)
    size = 14 if g is MINI else 18
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


def _encode(canvas: Image.Image) -> bytes:
    out = io.BytesIO()
    img = canvas.convert("RGB")
    if EXT == "webp":
        img.save(out, "WEBP", quality=82, method=4)
    else:
        img.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()


def _render_mini(track: Track, base: Base, st: CardState) -> bytes:
    g = MINI
    canvas = base.canvas.copy()
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
           next_art: Optional[bytes] = None) -> bytes:
    g = GEOS.get(st.layout, WIDE)
    theme = st.theme if st.theme in THEMES else "blur"
    base = _render_base(track, art_bytes, g, theme)
    if g is MINI:
        return _render_mini(track, base, st)
    g = _flow(g, base.text_bottom)
    canvas = base.canvas.crop((0, 0, g.w, g.h)) if g.h != base.canvas.height else base.canvas.copy()
    accent, accent2, bg = base.accent, base.accent2, base.bg
    glow = theme == "neon"
    if st.mode == "error":
        canvas.alpha_composite(Image.new("RGBA", canvas.size, (120, 0, 0, 70)))
    _draw_label(canvas, g, st, track, accent)

    if st.mode == "error":
        d = ImageDraw.Draw(canvas)
        fnt = font("Regular", 18)
        y = g.chips_y
        for line in _wrap(d, st.reason or "ไม่ทราบสาเหตุ", fnt, g.max_w, 3):
            _text(d, g, y, line, fnt, (255, 190, 190))
            y += 26
        return _encode(canvas)

    if st.mode == "loading":
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        x = _row_x(g, 3 * 90 + 2 * 10)
        for i in range(3):  # skeleton chips
            ld.rounded_rectangle((x + i * 100, g.chips_y, x + i * 100 + 90, g.chips_y + 30), 15,
                                 fill=(255, 255, 255, 30))
        canvas.alpha_composite(layer)
        _draw_wave(canvas, g, track, None, accent, accent2, bg)
        return _encode(canvas)

    soft = (255, 255, 255, 38)
    if st.mode == "play":
        chips = [_volume_chip(st.volume)]
        if st.loop != "off":
            chips.append(("loop", "เพลง" if st.loop == "track" else "คิว", soft,
                          (240, 240, 245), False))
        if st.queue_len:
            chips.append(("queue", f"{st.queue_len}", soft, (240, 240, 245), False))
        _draw_chips(canvas, g, chips + _badges(st))
        if st.next_title:
            _draw_next(canvas, g, st.next_title, accent, bg, _open_art(next_art))
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
    return _encode(canvas)


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


async def _run(fn):
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(loop.run_in_executor(None, fn), timeout=8)


async def make_card(track: Track, state: CardState = CardState()) -> Optional[bytes]:
    try:
        art, next_art = await asyncio.gather(
            fetch_track_art(track), fetch_art(state.next_thumb or None))
        return await _run(lambda: render(track, art, state, next_art))
    except Exception as exc:
        log.warning("card render failed: %s", exc)
        return None


async def warm(track: Track, theme: str, layout: str):
    """Pre-render the static part of a card (download cover, blur, palette, title)
    before the track starts, so its panel appears without waiting."""
    try:
        art = await fetch_track_art(track)
        g = GEOS.get(layout, WIDE)
        await _run(lambda: _render_base(track, art, g, theme if theme in THEMES else "blur"))
    except Exception as exc:
        log.debug("card warm-up failed: %s", exc)


async def make_queue_card(rows: list[dict], header: str, sub: str,
                          thumbs: list[Track]) -> Optional[bytes]:
    try:
        arts = await asyncio.gather(*(fetch_track_art(t) for t in thumbs))
        return await _run(lambda: render_queue(rows, header, sub, list(arts)))
    except Exception as exc:
        log.warning("queue card failed: %s", exc)
        return None


async def make_summary(plays: list[dict]) -> Optional[bytes]:
    try:
        stats = summarize(plays)
        tracks = [Track(title=p["title"], url=p["url"], thumbnail=p.get("thumbnail"),
                        origin=p.get("origin") or "youtube") for p, _ in stats["top_tracks"]]
        arts = await asyncio.gather(*(fetch_track_art(t) for t in tracks))
        return await _run(lambda: render_summary(stats, list(arts)))
    except Exception as exc:
        log.warning("summary render failed: %s", exc)
        return None
