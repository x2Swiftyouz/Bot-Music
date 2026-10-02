"""Image cards (Pillow): now playing, loading, error, share and session recap.
Thai + Latin text with the bundled Kanit font."""

import asyncio
import colorsys
import functools
import io
import logging
import math
import os
import random
import unicodedata
import zlib
from collections import Counter
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
FILENAME = f"nowplaying.{EXT}"

THEMES = ("blur", "solid", "minimal")
LAYOUTS = ("wide", "square")
HOT_THRESHOLD = config.HOT_THRESHOLD

SOURCE_NAMES = {"youtube": "YouTube", "soundcloud": "SoundCloud", "spotify": "Spotify",
                "bandcamp": "Bandcamp", "twitch": "Twitch"}
DEFAULT_ACCENT = (88, 101, 242)
WHITE = (255, 255, 255)
RED = (237, 66, 69)

# Thai: vowels written before their consonant, and vowels that must stay after it.
LEAD_VOWELS = "เแโใไ"
FOLLOW_VOWELS = "ะาำๅ"

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


WIDE = Geo(w=1000, h=430, art=350, art_x=40, art_y=40, x0=430, max_w=530, center=False,
           label_y=40, title_y=72, title_size=38, chips_y=250, next_y=292,
           wave_y=322, wave_h=40, time_y=366)
SQUARE = Geo(w=640, h=720, art=300, art_x=170, art_y=36, x0=40, max_w=560, center=True,
             label_y=352, title_y=384, title_size=32, chips_y=548, next_y=590,
             wave_y=624, wave_h=40, time_y=668)
GEOS = {"wide": WIDE, "square": SQUARE}


@dataclass(frozen=True)
class CardState:
    """Everything that changes the dynamic part of a card."""
    position: float = 0
    volume: int = 50
    loop: str = "off"
    queue_len: int = 0
    paused: bool = False
    remaining: bool = False
    next_title: str = ""
    hot: int = 0
    birthday: bool = False
    blink: bool = True
    theme: str = "blur"
    layout: str = "wide"
    mode: str = "play"      # play | share | loading | error
    reason: str = ""


# -------------------------------------------------------------------- text
def _safe_cut(text: str, cut: int) -> int:
    """Move a cut point left so Thai marks and vowels stay with their consonant."""
    while 1 < cut < len(text) and (unicodedata.category(text[cut]) == "Mn"
                                   or text[cut] in FOLLOW_VOWELS
                                   or text[cut - 1] in LEAD_VOWELS):
        cut -= 1
    return cut


def _fit(draw: ImageDraw.ImageDraw, text: str, fnt, max_w: int) -> str:
    if draw.textlength(text, font=fnt) <= max_w:
        return text
    cut = len(text)
    while cut > 1 and draw.textlength(text[:cut] + "…", font=fnt) > max_w:
        cut -= 1
    return text[:_safe_cut(text, cut)].rstrip() + "…"


def _wrap(draw: ImageDraw.ImageDraw, text: str, fnt, max_w: int, lines: int) -> list[str]:
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


def display_title(track: Track) -> str:
    """'Artist - Song' becomes 'Song' when the artist line already shows the artist."""
    artist = (track.artist or "").strip()
    title = track.title
    for sep in (" - ", " – ", " — "):
        if artist and title.lower().startswith(artist.lower() + sep):
            return title[len(artist) + len(sep):].strip() or title
    return title


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
    if theme == "blur" and art:
        bg = ImageOps.fit(art, (g.w, g.h)).filter(ImageFilter.GaussianBlur(28))
        bg = Image.blend(bg, Image.new("RGB", (g.w, g.h), (12, 12, 18)), 0.62)
    elif theme == "minimal":
        bg = Image.new("RGB", (g.w, g.h), (20, 20, 26))
    else:
        bg = Image.new("RGB", (g.w, g.h), _mix((14, 14, 20), a1, 0.18))
    canvas = bg.convert("RGBA")
    if theme != "minimal":  # two-tone accent glow
        canvas.alpha_composite(_gradient((g.w, g.h), a1, a2, 70, 22))
    return canvas


def _cover(art: Optional[Image.Image], size: int, accent) -> Image.Image:
    if art:
        return ImageOps.fit(art, (size, size))
    cover = Image.new("RGB", (size, size), accent)
    _note(ImageDraw.Draw(cover), size * 0.36, size * 0.28, size * 0.42, WHITE)
    return cover


def _paste_cover(canvas: Image.Image, cover: Image.Image, x: int, y: int, radius: int = 24):
    size = cover.size[0]
    shadow = Image.new("RGBA", (size + 40, size + 40), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((20, 24, size + 20, size + 24), radius + 4,
                                             fill=(0, 0, 0, 150))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(10)), (x - 20, y - 20))
    canvas.alpha_composite(_rounded(cover, radius), (x, y))


def _source_badge(canvas: Image.Image, g: Geo, track: Track):
    key = track.origin if track.origin in SOURCE_COLORS else detect_source(track.url)
    name = SOURCE_NAMES.get(key)
    if not name:
        return
    d = ImageDraw.Draw(canvas)
    fnt = font("Medium", 15)
    tw = d.textlength(name, font=fnt)
    x, y = g.art_x + 12, g.art_y + g.art - 12 - 26
    color = SOURCE_COLORS[key]
    rgb = ((color >> 16) & 255, (color >> 8) & 255, color & 255)
    d.rounded_rectangle((x, y, x + tw + 34, y + 26), 13, fill=rgb)
    if key == "youtube":
        d.polygon([(x + 10, y + 7), (x + 10, y + 19), (x + 20, y + 13)], fill=WHITE)
    else:
        d.ellipse((x + 10, y + 8, x + 20, y + 18), fill=WHITE)
    d.text((x + 26, y + 13), name, font=fnt, fill=WHITE, anchor="lm")


# ----------------------------------------------------------------- drawing
def _row_x(g: Geo, width: float) -> float:
    return (g.w - width) / 2 if g.center else g.x0


def _text(d: ImageDraw.ImageDraw, g: Geo, y: float, text: str, fnt, fill):
    if g.center:
        d.text((g.w / 2, y), text, font=fnt, fill=fill, anchor="ma")
    else:
        d.text((g.x0, y), text, font=fnt, fill=fill)


_base_cache: dict[tuple, tuple] = {}


def _render_base(track: Track, art_bytes: Optional[bytes], g: Geo, theme: str):
    """Static part (background, cover, title, artist, requester). Cached per track."""
    key = (track.url, track.title, track.artist, track.requester_name,
           len(art_bytes or b""), g, theme)
    if key in _base_cache:
        return _base_cache[key]
    art = _open_art(art_bytes)
    a1, a2 = _palette(art)
    canvas = _background(g, theme, art, a1, a2)
    _paste_cover(canvas, _cover(art, g.art, a1), g.art_x, g.art_y)
    _source_badge(canvas, g, track)

    # Text colours checked against the real background behind the text column.
    region = (0, g.label_y, g.w, g.h) if g.center else (g.x0, 0, g.w, g.h)
    bg = tuple(int(v) for v in ImageStat.Stat(canvas.crop(region).convert("RGB")).mean)
    accent = _readable(a1, bg)
    accent2 = _readable(a2, bg)

    d = ImageDraw.Draw(canvas)
    title_font = font("Bold", g.title_size)
    lh = int(g.title_size * 1.3)
    y = g.title_y
    for line in _wrap(d, display_title(track), title_font, g.max_w, 2):
        _text(d, g, y, line, title_font, WHITE)
        y += lh
    if track.artist:
        fnt = font("Medium", 23)
        _text(d, g, y + 2, _fit(d, track.artist, fnt, g.max_w), fnt, _readable((225, 225, 235), bg))
        y += 32
    fnt = font("Regular", 19)
    _text(d, g, y + 4, _fit(d, f"ขอโดย {track.requester_name or '-'}", fnt, g.max_w), fnt,
          _readable((185, 185, 198), bg))

    if len(_base_cache) > 8:
        _base_cache.clear()
    _base_cache[key] = (canvas, accent, accent2, bg)
    return _base_cache[key]


@functools.lru_cache(maxsize=64)
def _wave_heights(seed: str, n: int) -> tuple[float, ...]:
    """Fake but stable waveform: the same track always draws the same shape."""
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


def _draw_wave(canvas: Image.Image, g: Geo, seed: str, ratio: Optional[float], c1, c2):
    """ratio None = everything dim (live / loading), 1.0 = everything lit (share card)."""
    n = g.max_w // (WAVE_BAR + WAVE_GAP)
    x0, total = _wave_box(g)
    cy = g.wave_y + g.wave_h / 2
    heights = _wave_heights(seed, n)
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
    d = ImageDraw.Draw(canvas)
    for box, color in lit:
        d.rounded_rectangle(box, 2, fill=color)
    if ratio is not None and 0 < ratio < 1:
        px = x0 + total * ratio
        d.rounded_rectangle((px - 1, g.wave_y - 4, px + 1, g.wave_y + g.wave_h + 4), 1, fill=WHITE)


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


def _draw_next(canvas: Image.Image, g: Geo, title: str, accent, bg):
    d = ImageDraw.Draw(canvas)
    head, body = font("Medium", 18), font("Regular", 18)
    prefix = "ถัดไป  "
    pw = d.textlength(prefix, font=head)
    text = _fit(d, title, body, g.max_w - pw)
    x = _row_x(g, pw + d.textlength(text, font=body))
    d.text((x, g.next_y), prefix, font=head, fill=accent)
    d.text((x + pw, g.next_y), text, font=body, fill=_readable((215, 215, 225), bg))


def _draw_times(canvas: Image.Image, g: Geo, left: str, right: str, right_color=None):
    d = ImageDraw.Draw(canvas)
    fnt = font("Regular", 18)
    x0, total = _wave_box(g)
    if left:
        d.text((x0, g.time_y), left, font=fnt, fill=(210, 210, 220))
    if right:
        d.text((x0 + total, g.time_y), right, font=fnt, fill=right_color or (210, 210, 220),
               anchor="ra")


def _encode(canvas: Image.Image) -> bytes:
    out = io.BytesIO()
    img = canvas.convert("RGB")
    if EXT == "webp":
        img.save(out, "WEBP", quality=82, method=4)
    else:
        img.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()


def render(track: Track, art_bytes: Optional[bytes], st: CardState = CardState()) -> bytes:
    g = GEOS.get(st.layout, WIDE)
    theme = st.theme if st.theme in THEMES else "blur"
    base, accent, accent2, bg = _render_base(track, art_bytes, g, theme)
    canvas = base.copy()
    if st.mode == "error":
        canvas.alpha_composite(Image.new("RGBA", canvas.size, (120, 0, 0, 70)))
    _draw_label(canvas, g, st, track, accent)
    seed = track.video_id or track.url

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
        _draw_wave(canvas, g, seed, None, accent, accent2)
        return _encode(canvas)

    if st.mode == "play":
        soft = (255, 255, 255, 38)
        chips = [("vol", f"{st.volume}%", soft, (240, 240, 245), st.volume == 0)]
        if st.loop != "off":
            chips.append(("loop", "เพลง" if st.loop == "track" else "คิว", soft,
                          (240, 240, 245), False))
        if st.queue_len:
            chips.append(("queue", f"{st.queue_len}", soft, (240, 240, 245), False))
        if st.hot >= HOT_THRESHOLD:
            chips.append(("hot", f"ฮิต ×{st.hot}", (255, 122, 26, 215), WHITE, False))
        if st.birthday:
            chips.append(("cake", "วันเกิดคนขอ!", (235, 69, 158, 215), WHITE, False))
        _draw_chips(canvas, g, chips)
        if st.next_title:
            _draw_next(canvas, g, st.next_title, accent, bg)
    elif st.birthday or st.hot >= HOT_THRESHOLD:  # share card keeps the badges
        chips = []
        if st.hot >= HOT_THRESHOLD:
            chips.append(("hot", f"ฮิต ×{st.hot}", (255, 122, 26, 215), WHITE, False))
        if st.birthday:
            chips.append(("cake", "วันเกิดคนขอ!", (235, 69, 158, 215), WHITE, False))
        _draw_chips(canvas, g, chips)

    if not track.duration:
        _draw_wave(canvas, g, seed, None, accent, accent2)
        _draw_times(canvas, g, fmt_time(st.position) if st.mode == "play" else "", "LIVE", RED)
    elif st.mode == "share":
        _draw_wave(canvas, g, seed, 1.0, accent, accent2)
        _draw_times(canvas, g, "", fmt_time(track.duration))
    else:
        ratio = min(max(st.position / track.duration, 0), 1)
        _draw_wave(canvas, g, seed, ratio, accent, accent2)
        right = ("-" + fmt_time(track.duration - st.position)) if st.remaining \
            else fmt_time(track.duration)
        _draw_times(canvas, g, fmt_time(st.position), right)
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
_art_cache: dict[str, bytes] = {}


async def fetch_art(url: Optional[str]) -> Optional[bytes]:
    if not url:
        return None
    if url in _art_cache:
        return _art_cache[url] or None  # b"" = known miss, do not ask again
    data = b""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as s:
            async with s.get(url) as r:
                if r.status == 200:
                    data = await r.read()
    except Exception as exc:
        log.debug("art fetch failed: %s", exc)
        return None  # network error: retry next time
    if len(_art_cache) > 200:
        _art_cache.clear()
    _art_cache[url] = data
    return data or None


async def fetch_track_art(track: Track) -> Optional[bytes]:
    """YouTube: the HD thumbnail has no letterbox. Spotify keeps its album cover."""
    vid = track.video_id
    if vid and track.origin == "youtube":
        data = await fetch_art(f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg")
        if data:
            return data
    return await fetch_art(track.thumbnail)


async def _run(fn) -> Optional[bytes]:
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(loop.run_in_executor(None, fn), timeout=8)


async def make_card(track: Track, state: CardState = CardState()) -> Optional[bytes]:
    try:
        art = await fetch_track_art(track)
        return await _run(lambda: render(track, art, state))
    except Exception as exc:
        log.warning("card render failed: %s", exc)
        return None


async def make_summary(plays: list[dict]) -> Optional[bytes]:
    try:
        stats = summarize(plays)
        arts = []
        for play, _ in stats["top_tracks"]:
            t = Track(title=play["title"], url=play["url"], thumbnail=play.get("thumbnail"),
                      origin=play.get("origin") or "youtube")
            arts.append(await fetch_track_art(t))
        return await _run(lambda: render_summary(stats, arts))
    except Exception as exc:
        log.warning("summary render failed: %s", exc)
        return None
