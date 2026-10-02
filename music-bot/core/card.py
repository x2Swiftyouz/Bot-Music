"""Now-playing image card (Pillow). Thai + Latin text with the bundled Kanit font."""

import asyncio
import io
import logging
import os
import unicodedata
from typing import Optional

import aiohttp
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, features

from core.sources import Track, fmt_time

log = logging.getLogger("musicbot.card")

FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "fonts")
W, H = 1000, 360
ART = 280
LAYOUT = ImageFont.Layout.RAQM if features.check("raqm") else ImageFont.Layout.BASIC

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


def _fit(draw: ImageDraw.ImageDraw, text: str, fnt, max_w: int) -> str:
    if draw.textlength(text, font=fnt) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=fnt) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


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
        while cut > 1 and cut < len(rest) and unicodedata.category(rest[cut]) == "Mn":
            cut -= 1  # keep Thai vowel/tone marks with their base letter
        space = rest.rfind(" ", 0, cut)
        if space > cut * 0.6:
            cut = space
        out.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        out.append(_fit(draw, rest, fnt, max_w))
    return out


def _dominant(img: Image.Image) -> tuple[int, int, int]:
    small = img.convert("RGB").resize((24, 24))
    pixels = sorted(small.getdata(), key=lambda p: max(p) - min(p), reverse=True)[:120]
    r, g, b = (sum(c) // len(pixels) for c in zip(*pixels))
    return r, g, b


def _rounded(img: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, *img.size), radius=radius, fill=255)
    img = img.convert("RGBA")
    img.putalpha(mask)
    return img


_base_cache: dict[tuple, tuple] = {}

PAD = (H - ART) // 2
X0 = PAD + ART + 40
MAX_W = W - X0 - 40


def _note(d: ImageDraw.ImageDraw, x: float, y: float, size: float, color):
    """Draw a music note (the font has no ♪ glyph)."""
    r = size * 0.22
    stem_x = x + r * 2
    d.ellipse((x, y + size - 2 * r, x + 2 * r, y + size), fill=color)
    d.rectangle((stem_x - max(size * 0.07, 1.5), y, stem_x, y + size - r), fill=color)
    d.polygon([(stem_x, y), (stem_x + size * 0.35, y + size * 0.2),
               (stem_x + size * 0.35, y + size * 0.38), (stem_x, y + size * 0.2)], fill=color)


def _render_base(track: Track, art_bytes: Optional[bytes]):
    """Static part (background, cover, title, requester). Cached per track."""
    key = (track.url, track.title, track.requester_name, len(art_bytes or b""))
    if key in _base_cache:
        return _base_cache[key]
    art = None
    if art_bytes:
        try:
            art = Image.open(io.BytesIO(art_bytes)).convert("RGB")
        except Exception:
            art = None
    accent = _dominant(art) if art else (88, 101, 242)

    if art:
        bg = ImageOps.fit(art, (W, H)).filter(ImageFilter.GaussianBlur(28))
        bg = Image.blend(bg, Image.new("RGB", (W, H), (12, 12, 18)), 0.62)
    else:
        bg = Image.new("RGB", (W, H), (20, 20, 28))
    canvas = bg.convert("RGBA")
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for x in range(W):  # accent glow from the left
        a = int(45 * max(0, 1 - x / (W * 0.7)))
        od.line([(x, 0), (x, H)], fill=(*accent, a))
    canvas = Image.alpha_composite(canvas, overlay)

    if art:
        cover = ImageOps.fit(art, (ART, ART))
    else:
        cover = Image.new("RGB", (ART, ART), accent)
        _note(ImageDraw.Draw(cover), ART * 0.36, ART * 0.28, ART * 0.42, (255, 255, 255))
    shadow = Image.new("RGBA", (ART + 40, ART + 40), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((20, 24, ART + 20, ART + 24), 28, fill=(0, 0, 0, 150))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(10)), (PAD - 20, PAD - 20))
    canvas.alpha_composite(_rounded(cover, 24), (PAD, PAD))

    d = ImageDraw.Draw(canvas)
    title_font = font("Bold", 40)
    y = PAD + 28
    for line in _wrap(d, track.title, title_font, MAX_W, 2):
        d.text((X0, y), line, font=title_font, fill=(255, 255, 255))
        y += 52
    sub = f"ขอโดย {track.requester_name or '-'}"
    d.text((X0, y + 4), _fit(d, sub, font("Regular", 22), MAX_W), font=font("Regular", 22),
           fill=(200, 200, 210))

    if len(_base_cache) > 8:
        _base_cache.clear()
    _base_cache[key] = (canvas, accent)
    return _base_cache[key]


def render(track: Track, art_bytes: Optional[bytes], *, position: float = 0,
           volume: int = 50, loop: str = "off", queue_len: int = 0,
           paused: bool = False, remaining: bool = False) -> bytes:
    base, accent = _render_base(track, art_bytes)
    canvas = base.copy()
    d = ImageDraw.Draw(canvas)
    bright = tuple(min(255, c + 90) for c in accent)

    label = "หยุดชั่วคราว · PAUSED" if paused else "กำลังเล่น · NOW PLAYING"
    label_color = (200, 200, 210) if paused else bright
    lx = X0
    if paused:
        d.rectangle((lx, PAD + 2, lx + 5, PAD + 20), fill=label_color)
        d.rectangle((lx + 10, PAD + 2, lx + 15, PAD + 20), fill=label_color)
        lx += 26
    d.text((lx, PAD - 4), label, font=font("Medium", 20), fill=label_color)

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    loop_txt = {"off": "Loop ปิด", "track": "Loop เพลง", "queue": "Loop คิว"}[loop]
    chips = [f"VOL {volume}%", loop_txt]
    if queue_len:
        chips.append(f"คิว {queue_len}")
    chip_font = font("Medium", 17)
    chip_y = PAD + ART - 96
    cx, chip_pos = X0, []
    for text in chips:
        tw = d.textlength(text, font=chip_font)
        ld.rounded_rectangle((cx, chip_y, cx + tw + 24, chip_y + 30), 15, fill=(255, 255, 255, 38))
        chip_pos.append((cx + 12, chip_y + 15, text))
        cx += tw + 34

    bar_y = PAD + ART - 38
    ld.rounded_rectangle((X0, bar_y, X0 + MAX_W, bar_y + 8), 4, fill=(255, 255, 255, 55))
    canvas = Image.alpha_composite(canvas, layer)
    d = ImageDraw.Draw(canvas)
    for tx, ty, text in chip_pos:
        d.text((tx, ty), text, font=chip_font, fill=(240, 240, 245), anchor="lm")
    if track.duration:
        ratio = min(max(position / track.duration, 0), 1)
        fill_w = max(int(MAX_W * ratio), 8)
        d.rounded_rectangle((X0, bar_y, X0 + fill_w, bar_y + 8), 4,
                            fill=tuple(min(255, c + 40) for c in accent))
        d.ellipse((X0 + fill_w - 8, bar_y - 4, X0 + fill_w + 8, bar_y + 12), fill=(255, 255, 255))
    small = font("Regular", 18)
    if not track.duration:
        right = "LIVE"
    elif remaining:
        right = "-" + fmt_time(track.duration - position)
    else:
        right = fmt_time(track.duration)
    d.text((X0, bar_y + 16), fmt_time(position), font=small, fill=(210, 210, 220))
    d.text((X0 + MAX_W, bar_y + 16), right, font=small, fill=(210, 210, 220), anchor="ra")

    out = io.BytesIO()
    canvas.convert("RGB").save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()


_art_cache: dict[str, bytes] = {}


async def fetch_art(url: Optional[str]) -> Optional[bytes]:
    if not url:
        return None
    if url in _art_cache:
        return _art_cache[url]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    return None
                data = await r.read()
    except Exception as exc:
        log.debug("art fetch failed: %s", exc)
        return None
    if len(_art_cache) > 100:
        _art_cache.clear()
    _art_cache[url] = data
    return data


async def make_card(track: Track, **kwargs) -> Optional[bytes]:
    try:
        art = await fetch_art(track.thumbnail)
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, lambda: render(track, art, **kwargs)), timeout=8)
    except Exception as exc:
        log.warning("card render failed: %s", exc)
        return None
