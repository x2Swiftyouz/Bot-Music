"""Embeds and interactive views (buttons, pagination, select menus)."""

import io
from collections import Counter
import time
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

import discord

import config
from core import clock
from core.checks import UserError, control
from core.lyrics import Lyrics
from core.sources import SOURCE_COLORS, Track, detect_source, fmt_time, split_feat

if TYPE_CHECKING:
    from core.player import GuildPlayer

LOOP_ICON = {"off": "➡️ ปิด", "track": "🔂 เพลงเดียว", "queue": "🔁 ทั้งคิว"}


def progress_bar(pos: float, total: Optional[int], width: int = 18) -> str:
    if not total:
        return "🔴 LIVE"
    ratio = min(max(pos / total, 0), 1)
    filled = int(ratio * width)
    return "▬" * filled + "🔘" + "▬" * (width - filled)


def panel_color(p: "GuildPlayer") -> int:
    """The colour of whatever the bot shows now: the playing song's cover, else the last
    one's, so queue pages and the queue-end box match the panel."""
    t = p.current or (p.history[-1] if getattr(p, "history", None) else None)
    return track_color(t) if t else SOURCE_COLORS["other"]


def track_color(track: Track) -> int:
    """The cover's main colour once a card was drawn for it, else the source's colour."""
    from core.card import accent_of
    accent = accent_of(track)
    if accent is not None:
        return accent
    key = track.origin if track.origin in SOURCE_COLORS else detect_source(track.url)
    return SOURCE_COLORS.get(key, SOURCE_COLORS["other"])


END_TS_SLACK = 2  # seconds the end time may drift before the panel text changes


def end_timestamp(p: "GuildPlayer") -> Optional[int]:
    """Unix time the current song ends. Kept steady between ticks so an unchanged
    panel is not edited again only because the clock moved by a second."""
    t = p.current
    if not t or not t.duration or p.is_paused:
        return None
    end = int(clock.now() + (t.duration - p.position) / getattr(p, "speed", 1.0))
    old = getattr(p, "_end_ts", None)
    if old is None or abs(old - end) > END_TS_SLACK:
        p._end_ts = old = end
    return old


def _countdown(p: "GuildPlayer") -> str:
    """Time left as a Discord timestamp: each viewer's client counts it down by itself."""
    t = p.current
    if not t.duration:
        return "🔴 LIVE"
    if p.is_paused:
        return f"⏸ หยุดที่ `{fmt_time(p.position)} / {t.fmt_duration()}`"
    return f"⏳ จบ <t:{end_timestamp(p)}:R>"


def _time_line(p: "GuildPlayer") -> str:
    t, pos = p.current, p.position
    right = f"`{t.fmt_duration()}`"
    if p.time_format == 1 and t.duration:
        right = f"`-{fmt_time(t.duration - pos)}`"
    elif p.time_format == 2 and t.duration and not p.is_paused:
        # Discord shows this timestamp in each viewer's own time zone
        right = f"จบ <t:{end_timestamp(p)}:t>"
    line = f"`{fmt_time(pos)}` {progress_bar(pos, t.duration)} {right}"
    if t.duration and not p.is_paused and p.time_format != 2:
        line += f"\n-# ⏳ จบ <t:{end_timestamp(p)}:R>"
    return line


def _link(t: Track) -> str:
    """The song as a link, named like on the card: 'ซบที่ไหล่ · KRK' (no featured artists,
    credits or channel tags)."""
    from core.card import _title_parts
    song, artist, _ = _title_parts(t)
    name = _plain(f"{song} · {artist}" if artist and artist.casefold() not in song.casefold()
                  else song, 80)
    # Discord shows a link whose text has emoji ("ແສນດີ ❤️") as raw [..](..): leave them out
    name = _no_emoji(name) or "เพลง"
    return f"**[{name}]({t.url})**" if t.url.startswith("http") else f"**{name}**"


NEXT_CHARS = 32  # the next song's name on the line under the card


def _next_name(t: Track) -> str:
    """The next song by its song name only ('คนบาป'), short, with no markdown in it."""
    from core.card import display_title
    name = " ".join((display_title(t) or t.name).split())
    name = name if len(name) <= NEXT_CHARS else name[:NEXT_CHARS - 1].rstrip() + "…"
    return discord.utils.escape_markdown(name)


def _no_emoji(text: str) -> str:
    import unicodedata
    out = "".join(ch for ch in text if unicodedata.category(ch) not in ("So", "Sk", "Cs", "Co")
                  and ch not in "\ufe0e\ufe0f\u200d\u20e3")
    return " ".join(out.split()).replace(" · ·", " ·").strip(" ·")


def build_now_playing(p: "GuildPlayer", card: Optional[str] = None) -> discord.Embed:
    """card: filename of the attached card image ("" = none, default: the panel's own).
    With a card the text only adds what the picture cannot do: a clickable title and a
    countdown that runs by itself. Everything else is already on the card."""
    t = p.current
    if not t:
        return build_idle_embed()
    card_name = (p.card_name if p.has_card else "") if card is None else card
    has_card = bool(card_name)
    e = discord.Embed(color=track_color(t))
    if has_card:
        e.set_image(url=f"attachment://{card_name}")
        if p.loading:
            e.description = f"{_link(t)} · ⏳ กำลังโหลด…"
            return e
        e.description = f"{_link(t)} · {_countdown(p)}"
        if p.queue:
            nxt = _next_name(p.queue[0])
            if p.compact:  # the mini card has no "up next" row
                e.description += f"\n-# ถัดไป: {nxt}"
            else:  # the card shows it too, but this line is what people read
                e.description += f" · ต่อไป: {nxt}"
        if karaoke := live_lyrics_text(p):
            if p.compact:
                e.description += "\n" + karaoke
            else:
                e.add_field(name="🎙 เนื้อเพลงสด", value=karaoke, inline=False)
        e.set_footer(text=status_line(p))
        return e

    # no card: the text carries everything
    e.title = t.name[:250]
    e.url = t.url if t.url.startswith("http") else None
    if t.thumbnail:
        e.set_thumbnail(url=t.thumbnail)
    if p.loading:
        e.set_author(name="⏳ กำลังโหลด…")
        e.description = f"-# ขอโดย {t.requester_name or '-'}"
        return e
    e.set_author(name="⏸ หยุดชั่วคราว" if p.is_paused else "▶️ กำลังเล่น")
    artist = f"**{t.artist}**\n" if t.artist else ""
    e.description = artist + _time_line(p)
    if p.compact:
        e.description += f"\n-# ขอโดย {t.requester_name or '-'}"
        if p.queue:
            e.description += f" · ถัดไป: {p.queue[0].name[:50]}"
        if karaoke := live_lyrics_text(p):
            e.description += "\n" + karaoke
        e.set_footer(text=status_line(p))
        return e
    e.add_field(name="ขอโดย", value=t.requester_name or "-", inline=True)
    if p.queue:
        nxt = p.queue[0]
        remain = p.total_remaining()
        tail = f" · รวม {fmt_time(remain)}" if remain else ""
        e.add_field(
            name=f"ถัดไป ({len(p.queue)} เพลงในคิว{tail})",
            value=f"{nxt.name[:90]} `[{nxt.fmt_duration()}]`",
            inline=False,
        )
    if karaoke := live_lyrics_text(p):
        e.add_field(name="🎙 เนื้อเพลงสด", value=karaoke, inline=False)
    e.set_footer(text=status_line(p))
    return e


def status_line(p: "GuildPlayer") -> str:
    """Small status strip under the panel: who just did what, then every mode that is on."""
    parts = []
    if hasattr(p, "active_note") and (note := p.active_note()):
        parts.append(note)
    if getattr(p, "away_until", 0) and p.is_paused:
        if config.UI_STYLE == "groove":  # counts down by itself
            when = f"ออก <t:{int(p.away_until)}:R>"
        else:  # embed footers show timestamps as raw text
            when = f"ออกในอีก {max(int((p.away_until - clock.now()) // 60), 1)} นาที"
        parts.append(f"💤 หยุดรอคนกลับเข้าห้อง · {when}")
    elif p.is_paused:
        parts.append("⏸ หยุดอยู่")
    if p.loop_mode == "track":
        parts.append("🔂 วนเพลงนี้")
    elif p.loop_mode == "queue":
        parts.append("🔁 วนทั้งคิว")
    if p.compact:  # the full panel shows these on its buttons and volume menu already
        vol = int(round(p.volume * 100))
        parts.append(f"{'🔇' if vol == 0 else '🔊'} {vol}%")
        if p.live_lyrics:
            parts.append("🎙 เนื้อสด")
    if listeners := len(p.humans_in_channel()):
        parts.append(f"🎧 {listeners} คนฟังอยู่")
    if p.normalize:
        parts.append("🎚 ความดังเท่ากัน")
    if getattr(p, "effect", "off") != "off":
        from core.player import EFFECTS
        name, emoji, *_ = EFFECTS[p.effect]
        parts.append(f"{emoji} {name}")
    if p.stay_247:
        parts.append("🌙 24/7")
    if getattr(p, "autoplay", False):  # its switch is inside ⚙️ now
        parts.append("📻 Autoplay")
    if getattr(p, "fair_queue", False) and p.queue:
        parts.append("⚖️ ผลัดกันเล่น")
    if p.skip_votes:
        parts.append(f"🗳 โหวตข้าม {len(p.skip_votes)}/{p.skip_need()}")
    return " · ".join(parts)


LYRICS_LEAD = 1.0  # seconds: the panel edit reaches people a little late
LYRIC_DOTS = 5    # progress through the current live lyric line


def live_lyrics_text(p: "GuildPlayer") -> Optional[str]:
    """Previous, current and next line of synced lyrics, for the panel."""
    if not p.live_lyrics or not p.current:
        return None
    if p.lyrics_url != p.current.url:
        return "-# กำลังหาเนื้อเพลง…"
    lyr = p.lyrics
    if not lyr or not lyr.synced:
        return "-# ไม่มีเนื้อเพลงแบบ synced สำหรับเพลงนี้ (ใช้ 🎤 ดูแบบเต็ม)"
    idx = lyr.line_at(p.position + LYRICS_LEAD)
    lines = []
    if idx >= 1:
        lines.append(f"-# {lyr.synced[idx - 1][1] or '♪'}")
    if idx >= 0:
        line = f"**{lyr.synced[idx][1] or '♪'}**"
        if idx + 1 < len(lyr.synced):  # how far into this line we are
            start, end = lyr.synced[idx][0], lyr.synced[idx + 1][0]
            if end - start >= 2:  # short lines change before the panel can show it
                done = min(max((p.position + LYRICS_LEAD - start) / (end - start), 0), 1)
                filled = min(int(done * LYRIC_DOTS), LYRIC_DOTS - 1) + 1
                line += "  " + "▰" * filled + "▱" * (LYRIC_DOTS - filled)
        lines.append(line)
    else:
        lines.append("-# ♪ …")
    if idx + 1 < len(lyr.synced):
        lines.append(lyr.synced[idx + 1][1] or "♪")
    return "\n".join(lines)[:1000]


def build_request_idle_embed() -> discord.Embed:
    e = discord.Embed(title="🎵 ห้องขอเพลง", color=SOURCE_COLORS["other"])
    e.description = (
        "**พิมพ์ชื่อเพลง หรือวางลิงก์ในห้องนี้ได้เลย**\n"
        "YouTube · Spotify · SoundCloud · ไฟล์เสียงแนบ\n\n"
        "-# เข้าห้องเสียงก่อน ข้อความจะถูกลบอัตโนมัติ panel จะแสดงตรงนี้ตอนเพลงเล่น")
    return e


class RequestIdleView(discord.ui.View):
    """Header of an idle request channel. Same custom_id as the panel's ➕ button."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(emoji="➕", label="ขอเพลง", style=discord.ButtonStyle.secondary,
                       custom_id="mb:add")
    async def add(self, inter, _):
        await act_add(inter)


def recap_line(plays: list[dict]) -> str:
    """'🎵 เล่นไป 5 เพลง · ⏱ 18 นาที · 👑 ขาประจำ @name (3 เพลง)' for this queue run."""
    from collections import Counter
    from core.card import _fmt_long
    seconds = int(sum(p["seconds"] for p in plays))
    parts = [f"🎵 เล่นไป **{len(plays)}** เพลง", f"⏱ {_fmt_long(seconds)}"]
    people = Counter(p["requester_id"] for p in plays if p.get("requester_id"))
    if len(people) > 1 or (people and len(plays) > 1):
        uid, n = people.most_common(1)[0]
        parts.append(f"👑 ขาประจำ <@{uid}> ({n} เพลง)")
    return " · ".join(parts)


def build_idle_embed(with_buttons: bool = False, recap: Optional[list[dict]] = None,
                     color: Optional[int] = None) -> discord.Embed:
    """recap: the songs this queue run played (shown above the buttons). color: the last
    song's cover colour, so the box keeps its look when the queue ends."""
    e = discord.Embed(color=SOURCE_COLORS["other"] if color is None else color)
    e.title = "⏹ จบคิวแล้ว"
    e.description = ("เล่นซ้ำเพลงล่าสุด เปิด 📻 Autoplay หรือเพิ่มเพลงใหม่ได้จากปุ่มด้านล่าง"
                     if with_buttons else "ใช้ `/play` เพื่อเล่นต่อ")
    if recap:
        e.description = f"{recap_line(recap)}\n-# {e.description}"
    return e


IDLE_REPLAYS = 3


class IdleView(discord.ui.View):
    """Under "queue ended": replay one of the last songs, turn on autoplay, or add a song."""

    def __init__(self, p: "GuildPlayer"):
        super().__init__(timeout=max(config.IDLE_TIMEOUT or 0, 300))
        self.guild_id = p.guild.id
        seen, self.recent = set(), []
        for t in reversed(p.history):
            if t.url not in seen and t.url.startswith("http"):
                seen.add(t.url)
                self.recent.append(t)
            if len(self.recent) >= IDLE_REPLAYS:
                break
        for i, t in enumerate(self.recent):
            button = discord.ui.Button(emoji="▶️", label=_plain(t.name, 70) or "เพลง",
                                       style=discord.ButtonStyle.secondary, row=0)
            button.callback = self._replay(t)
            self.add_item(button)
        self.autoplay_btn.disabled = getattr(p, "autoplay", False) or not self.recent

    def _replay(self, track: Track):
        async def callback(inter: discord.Interaction):
            music = inter.client.get_cog("Music")
            copy = Track.from_dict(track.to_dict())
            try:
                music.check_cooldown(inter.user.id)
                await inter.response.defer(ephemeral=True, thinking=True)
                embed = await music.enqueue(inter.user, inter.channel, copy.url, tracks=[copy])
            except UserError as exc:
                if inter.response.is_done():
                    return await inter.followup.send(f"⚠️ {exc}", ephemeral=True)
                return await inter.response.send_message(f"⚠️ {exc}", ephemeral=True)
            await inter.followup.send(embed=embed, ephemeral=True)
            await audit_inter(inter, "replay (queue end)", copy.url)
        return callback

    @discord.ui.button(emoji="📻", label="เปิด Autoplay", style=discord.ButtonStyle.secondary, row=1)
    async def autoplay_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        p = inter.client.players.get(inter.guild_id)
        if not p:
            return await inter.response.send_message("บอทออกจากห้องแล้ว ใช้ `/play` เพื่อเริ่มใหม่",
                                                     ephemeral=True)
        if not p.autoplay:
            p.toggle_autoplay()
            await inter.client.db.set_setting(inter.guild_id, "autoplay", 1)
        button.disabled = True
        await inter.response.edit_message(view=self)
        await audit_inter(inter, "autoplay", "on (queue end)")

    @discord.ui.button(emoji="➕", label="เพิ่มเพลง", style=discord.ButtonStyle.success, row=1)
    async def add_btn(self, inter: discord.Interaction, _):
        await inter.response.send_modal(AddSongModal())


def queue_items(p: "GuildPlayer", query: str = "") -> list[tuple[int, Track]]:
    """(1-based queue position, track), filtered by title / artist / requester."""
    q = query.casefold().strip()
    return [(i, t) for i, t in enumerate(p.queue, 1)
            if not q or q in f"{t.title} {t.artist} {t.requester_name}".casefold()]


class RowsEmbed(discord.Embed):
    """An embed that also carries one row per track, so the Groove look can draw each
    song as its own line with a small cover (see core/look.py). Classic mode ignores it."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.groove_head = ""                                   # line above the rows
        self.groove_rows: list[tuple[str, Optional[str]]] = []  # (text, cover url)


def _plain(text: str, limit: int = 55) -> str:
    """Title safe inside a markdown link (Groove strips brackets too)."""
    text = "".join(ch for ch in text if ch not in "[]()")
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def queue_row(p: "GuildPlayer", pos: int, t: Track) -> str:
    link = f"[{_plain(t.name)}]({t.url})" if t.url.startswith("http") else _plain(t.name)
    sub = [t.artist] if t.artist else []
    sub.append(f"ขอโดย {t.requester_name or '-'}")
    eta = p.eta(pos - 1)
    sub.append("ถัดไป" if pos == 1 else (f"เล่น {relative_ts(eta)}" if eta is not None else ""))
    return f"**{pos}.** {link} `{t.fmt_duration()}`\n-# " + " · ".join(x for x in sub if x)


QUEUE_PER_PAGE = 5 if config.UI_STYLE == "groove" else 10  # V2 messages: max 40 components


def build_queue_pages(p: "GuildPlayer", per_page: int = QUEUE_PER_PAGE,
                      query: str = "") -> list[discord.Embed]:
    items = queue_items(p, query)
    pages = []
    total_pages = max(1, -(-len(items) // per_page))
    remain = p.total_remaining()
    title = f"🔍 ค้นในคิว: {query}" if query else "📜 คิวเพลง"
    for page in range(total_pages):
        e = RowsEmbed(title=title[:256], color=panel_color(p))
        if p.current:
            c = p.current
            e.groove_head = (f"**กำลังเล่น:** [{_plain(c.name)}]({c.url}) "
                             f"`{fmt_time(p.position)}/{c.fmt_duration()}`")
        for i, t in items[page * per_page:(page + 1) * per_page]:
            e.groove_rows.append((queue_row(p, i, t), t.thumbnail))
        if p.current:
            e.add_field(
                name="กำลังเล่น",
                value=f"[{p.current.name[:80]}]({p.current.url}) "
                      f"`{fmt_time(p.position)}/{p.current.fmt_duration()}`",
                inline=False,
            )
        lines = []
        for i, t in items[page * per_page:(page + 1) * per_page]:
            lines.append(f"`{i}.` {t.name[:70]} `[{t.fmt_duration()}]` · {t.requester_name}")
        e.description = "\n".join(lines) or ("ไม่พบเพลงที่ค้น" if query else "คิวว่าง")
        e.set_footer(text=(
            f"หน้า {page + 1}/{total_pages} · "
            + (f"พบ {len(items)} จาก {len(p.queue)} เพลง" if query else f"{len(items)} เพลง")
            + (f" · รวม {fmt_time(remain)}" if remain else "")
            + f" · loop {p.loop_mode}"
        ))
        pages.append(e)
    return pages


class PagesView(discord.ui.View):
    """Generic ◀ ▶ paginator."""

    def __init__(self, pages: list[discord.Embed], author_id: int, timeout: float = 180,
                 deny_text: str = "ใช้ `/queue` เปิดของตัวเอง"):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.index = 0
        self.author_id = author_id
        self.deny_text = deny_text
        self.message: Optional[discord.Message] = None
        self._sync()

    def _sync(self):
        self.prev_btn.disabled = self.index == 0
        self.next_btn.disabled = self.index >= len(self.pages) - 1

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.author_id:
            await inter.response.send_message(self.deny_text, ephemeral=True)
            return False
        return True

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, inter: discord.Interaction, _):
        self.index -= 1
        self._sync()
        await inter.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next_btn(self, inter: discord.Interaction, _):
        self.index += 1
        self._sync()
        await inter.response.edit_message(embed=self.pages[self.index], view=self)

    async def on_timeout(self):
        if self.message:
            try:  # keep the page, drop the buttons
                await self.message.edit(embed=self.pages[self.index], view=None)
            except discord.HTTPException:
                pass


class QueueView(PagesView):
    """Queue pages plus a multi-select picker: play now, move up, remove,
    search inside the queue, a menu of my own songs to remove, remove duplicates."""

    PER_PAGE = QUEUE_PER_PAGE

    def __init__(self, p: "GuildPlayer", author_id: int):
        self.p = p
        self.query = ""
        self.selected: list[Track] = []
        self.picker = discord.ui.Select(placeholder="เลือกเพลง (เลือกได้หลายเพลง)", row=1,
                                        options=[discord.SelectOption(label="-")])
        self.picker.callback = self._picked
        # one step for the most common wish: take my own song(s) out, on any page
        self.mine_picker = discord.ui.Select(placeholder="🗑 ลบเพลงที่ฉันขอ", row=3,
                                             options=[discord.SelectOption(label="-")])
        self.mine_picker.callback = self._remove_mine
        self._mine: list[Track] = []
        super().__init__(build_queue_pages(p, self.PER_PAGE), author_id)
        self.add_item(self.picker)
        self.add_item(self.mine_picker)
        self._sync()

    def _sync(self):
        super()._sync()
        if not hasattr(self, "picker"):
            return
        start = self.index * self.PER_PAGE
        items = queue_items(self.p, self.query)[start:start + self.PER_PAGE]
        on_page = {id(t) for _, t in items}
        self.selected = [t for t in self.selected if id(t) in on_page]
        chosen = {id(t) for t in self.selected}
        if items:
            self.picker.options = [
                discord.SelectOption(
                    label=f"{i}. {t.name}"[:100], value=str(i),
                    description=f"{t.fmt_duration()} · {t.requester_name or '-'}"[:100],
                    default=id(t) in chosen)
                for i, t in items]
            self.picker.max_values = len(items)
            self.picker.disabled = False
        else:
            self.picker.options = [discord.SelectOption(label="ไม่มีเพลง", value="0")]
            self.picker.max_values = 1
            self.picker.disabled = True
        n = len(self.selected)
        self.jump_btn.disabled = n != 1
        self.top_btn.disabled = self.remove_btn.disabled = n == 0
        q = self.p.queue
        first = q[0] if q else None
        last = q[-1] if q else None
        self.up_btn.disabled = n == 0 or all(t is first for t in self.selected)
        self.down_btn.disabled = n == 0 or all(t is last for t in self.selected)
        self.remove_btn.label = f"ลบ {n}" if n > 1 else None
        self.search_btn.label = "ล้างการค้นหา" if self.query else "ค้นหา"
        self.search_btn.emoji = "✖️" if self.query else "🔍"
        self.dedupe_btn.disabled = not self.p.queue
        self.image_btn.disabled = not queue_items(self.p, self.query)
        self._sync_mine()

    MINE_MAX = 25  # options a menu can hold

    def _sync_mine(self):
        if not hasattr(self, "mine_picker"):
            return
        mine = [(i, t) for i, t in enumerate(self.p.queue, 1)
                if t.requester_id == self.author_id][:self.MINE_MAX]
        self._mine = [t for _, t in mine]
        menu = self.mine_picker
        if mine:
            menu.options = [discord.SelectOption(
                label=f"{i}. {t.name}"[:100], value=str(n), emoji="🗑",
                description=f"{t.fmt_duration()} · ลำดับที่ {i}"[:100])
                for n, (i, t) in enumerate(mine)]
            menu.max_values = len(mine)
            menu.placeholder = f"🗑 ลบเพลงที่ฉันขอ ({len(mine)} เพลง) เลือกแล้วลบทันที"
            menu.disabled = False
        else:
            menu.options = [discord.SelectOption(label="ไม่มีเพลงของคุณ", value="-")]
            menu.max_values = 1
            menu.placeholder = "🗑 คุณไม่มีเพลงในคิว"
            menu.disabled = True

    async def _remove_mine(self, inter: discord.Interaction):
        picked = [self._mine[int(v)] for v in self.mine_picker.values
                  if v.isdigit() and int(v) < len(self._mine)]

        def run(p):
            ids = {id(t) for t in p.queue}
            alive = [t for t in picked if id(t) in ids and t.requester_id == inter.user.id]
            if not alive:
                raise UserError("เพลงที่เลือกไม่อยู่ในคิวแล้ว")
            p.remove_tracks(alive)
            if len(alive) == 1:
                return f"🗑 ลบ **{alive[0].name}** แล้ว (ใช้ /undo เพื่อย้อน)"
            return f"🗑 ลบเพลงของคุณ {len(alive)} เพลงแล้ว (ใช้ /undo เพื่อย้อน)"
        await self._apply(inter, "remove mine (menu)", run)

    def _refresh(self):
        self.pages = build_queue_pages(self.p, self.PER_PAGE, self.query)
        self.index = min(self.index, len(self.pages) - 1)
        self._sync()

    async def _picked(self, inter: discord.Interaction):
        q = list(self.p.queue)
        self.selected = [q[int(v) - 1] for v in self.picker.values if 0 < int(v) <= len(q)]
        self._refresh()
        await inter.response.edit_message(embed=self.pages[self.index], view=self)

    async def _apply(self, inter: discord.Interaction, label: str, action):
        """Run a queue change. action(player) returns the message, or raises UserError."""
        try:
            p = control(inter)
            if p is not self.p:
                raise UserError("คิวนี้หมดอายุแล้ว เปิด `/queue` ใหม่")
            text = action(p)
        except UserError as exc:
            return await inter.response.send_message(str(exc), ephemeral=True)
        self.selected = []
        self._refresh()
        await inter.response.edit_message(content=text, embed=self.pages[self.index], view=self)
        await audit_inter(inter, label, text[:150])
        await p.update_panel()

    def _alive(self, p: "GuildPlayer") -> list[Track]:
        """Selected tracks still in the queue (it may have changed since)."""
        ids = {id(t) for t in p.queue}
        alive = [t for t in self.selected if id(t) in ids]
        if not alive:
            raise UserError("เพลงที่เลือกไม่อยู่ในคิวแล้ว")
        return alive

    @discord.ui.button(emoji="▶️", label="เล่นเลย", style=discord.ButtonStyle.secondary, row=0)
    async def jump_btn(self, inter: discord.Interaction, _):
        def run(p):
            t = self._alive(p)[0]
            p.jump(next(i for i, x in enumerate(p.queue, 1) if x is t))
            return f"⏩ ไปที่ **{t.name}**"
        await self._apply(inter, "jump", run)

    @discord.ui.button(emoji="⬆️", label="ถัดไป", style=discord.ButtonStyle.secondary, row=0)
    async def top_btn(self, inter: discord.Interaction, _):
        def run(p):
            alive = self._alive(p)
            p.move_to_front(alive)
            if len(alive) == 1:
                return f"⬆️ ย้าย **{alive[0].name}** ขึ้นเป็นเพลงถัดไป"
            return f"⬆️ ย้าย {len(alive)} เพลงขึ้นต้นคิว"
        await self._apply(inter, "move", run)

    @discord.ui.button(emoji="🗑", style=discord.ButtonStyle.secondary, row=0)
    async def remove_btn(self, inter: discord.Interaction, _):
        def run(p):
            alive = self._alive(p)
            admin = p.is_admin(inter.user)
            mine = [t for t in alive if admin or t.requester_id == inter.user.id]
            if not mine:
                raise UserError("ลบได้เฉพาะเพลงของตัวเอง")
            p.remove_tracks(mine)
            skipped = len(alive) - len(mine)
            text = f"🗑 ลบ **{mine[0].name}**" if len(mine) == 1 else f"🗑 ลบ {len(mine)} เพลง"
            return text + (f" (ข้าม {skipped} เพลงของคนอื่น)" if skipped else "")
        await self._apply(inter, "remove", run)

    async def _nudge(self, inter: discord.Interaction, step: int):
        """⬆ / ⬇: move the selection one place and keep it selected, so pressing again
        keeps moving it. The page follows the song."""
        try:
            p = control(inter)
            if p is not self.p:
                raise UserError("คิวนี้หมดอายุแล้ว เปิด `/queue` ใหม่")
            alive = self._alive(p)
            if not p.move_by(alive, step):
                raise UserError("เลื่อนต่อไม่ได้แล้ว")
        except UserError as exc:
            return await inter.response.send_message(str(exc), ephemeral=True)
        self.selected = alive
        if not self.query:  # show the page the (first) song is on now
            pos = next(i for i, t in enumerate(p.queue) if t is alive[0])
            self.index = pos // self.PER_PAGE
        self.pages = build_queue_pages(p, self.PER_PAGE, self.query)
        self.index = min(self.index, len(self.pages) - 1)
        self._sync()
        where = next(i for i, t in enumerate(p.queue, 1) if t is alive[0])
        text = (f"{'⬆️' if step < 0 else '⬇️'} **{alive[0].name}** อยู่ลำดับที่ {where}"
                if len(alive) == 1 else f"{'⬆️' if step < 0 else '⬇️'} เลื่อน {len(alive)} เพลง")
        await inter.response.edit_message(content=text, embed=self.pages[self.index], view=self)
        await audit_inter(inter, "move up" if step < 0 else "move down", text[:150])
        await p.update_panel()

    @discord.ui.button(emoji="🔼", label="เลื่อนขึ้น", style=discord.ButtonStyle.secondary, row=2)
    async def up_btn(self, inter: discord.Interaction, _):
        await self._nudge(inter, -1)

    @discord.ui.button(emoji="🔽", label="เลื่อนลง", style=discord.ButtonStyle.secondary, row=2)
    async def down_btn(self, inter: discord.Interaction, _):
        await self._nudge(inter, 1)

    @discord.ui.button(emoji="🔍", label="ค้นหา", style=discord.ButtonStyle.secondary, row=2)
    async def search_btn(self, inter: discord.Interaction, _):
        if self.query:
            self.query, self.index, self.selected = "", 0, []
            self._refresh()
            return await inter.response.edit_message(embed=self.pages[0], view=self)
        await inter.response.send_modal(QueueSearchModal(self))

    @discord.ui.button(emoji="♻️", label="ลบเพลงซ้ำ", style=discord.ButtonStyle.secondary, row=2)
    async def dedupe_btn(self, inter: discord.Interaction, _):
        def run(p):
            n = p.dedupe()
            if not n:
                raise UserError("ไม่มีเพลงซ้ำในคิว")
            return f"♻️ ลบเพลงซ้ำ {n} เพลง (ใช้ /undo เพื่อย้อน)"
        await self._apply(inter, "dedupe", run)


    @discord.ui.button(emoji="🖼", label="รูปคิว", style=discord.ButtonStyle.secondary, row=2)
    async def image_btn(self, inter: discord.Interaction, _):
        """Queue card: the next songs on this page as one shareable image."""
        await inter.response.defer(thinking=True)
        data = await build_queue_card(self.p, self.query, self.index * self.PER_PAGE)
        if not data:
            return await inter.followup.send("สร้างรูปไม่ได้ ลองใหม่", ephemeral=True)
        from core.card import new_filename
        name = new_filename("queue")
        await inter.followup.send(file=discord.File(
            io.BytesIO(data), filename=name,
            description=f"คิวเพลง {len(self.p.queue)} เพลง"))


async def build_queue_card(p: "GuildPlayer", query: str = "", start: int = 0,
                           count: int = 5) -> Optional[bytes]:
    from core.card import make_queue_card
    from core.player import clock_after
    items = queue_items(p, query)[start:start + count]
    rows = []
    for pos, t in items:
        eta = p.eta(pos - 1)
        when = "ถัดไป" if pos == 1 else (f"เล่น {clock_after(eta)}" if eta is not None else "")
        rows.append({"pos": pos, "title": t.name, "artist": t.artist,
                     "requester": t.requester_name or "-", "duration": t.fmt_duration(),
                     "when": when})
    remain = p.total_remaining()
    header = f"{len(p.queue)} เพลงในคิว" + (f" · ค้น \"{query}\"" if query else "")
    sub = f"รวม {fmt_time(remain)} · จบ {clock_after(remain)}" if remain else ""
    return await make_queue_card(rows, header, sub, [t for _, t in items])


class QueueSearchModal(discord.ui.Modal, title="ค้นหาในคิว"):
    query = discord.ui.TextInput(label="ชื่อเพลง ศิลปิน หรือชื่อคนขอ", max_length=100)

    def __init__(self, view: QueueView):
        super().__init__()
        self.view = view

    async def on_submit(self, inter: discord.Interaction):
        v = self.view
        v.query, v.index, v.selected = str(self.query.value).strip(), 0, []
        v._refresh()
        await inter.response.edit_message(embed=v.pages[0], view=v)


ADD_MODES = (("end", "ต่อท้ายคิว", "➕", "รอคิวตามปกติ"),
             ("next", "เป็นเพลงถัดไป", "⏭", "เล่นต่อจากเพลงนี้"),
             ("now", "เล่นทันที", "▶️", "ข้ามเพลงที่เล่นอยู่ (ถ้าคุณข้ามได้)"))


class AddSongModal(discord.ui.Modal, title="เพิ่มเพลง"):
    query = discord.ui.TextInput(label="ชื่อเพลง หรือลิงก์",
                                 placeholder="เช่น ลมหายใจ bodyslam หรือลิงก์ YouTube / Spotify",
                                 max_length=300)
    how = discord.ui.Label(text="เพิ่มแบบไหน", component=discord.ui.Select(
        options=[discord.SelectOption(label=label, value=value, emoji=emoji, description=desc,
                                      default=value == "end")
                 for value, label, emoji, desc in ADD_MODES]))

    async def on_submit(self, inter: discord.Interaction):
        music = inter.client.get_cog("Music")
        mode = (self.how.component.values or ["end"])[0]
        try:
            music.check_cooldown(inter.user.id)
            await inter.response.defer(ephemeral=True, thinking=True)
            embed = await music.enqueue(inter.user, inter.channel, str(self.query.value),
                                        front=mode in ("next", "now"))
        except UserError as exc:
            if inter.response.is_done():
                return await inter.followup.send(f"⚠️ {exc}", ephemeral=True)
            return await inter.response.send_message(f"⚠️ {exc}", ephemeral=True)
        extra = play_now(inter) if mode == "now" else ""
        await inter.followup.send(content=extra or None, embed=embed, ephemeral=True)
        await audit_inter(inter, f"play (panel, {mode})", str(self.query.value))


def play_now(inter: discord.Interaction) -> str:
    """'เล่นทันที': the song was put first in the queue; skip what is playing if this
    person may (else it simply plays next). Returns a note for them, or ""."""
    p = inter.client.players.get(inter.guild_id)
    if not p or not p.current or not p.queue or p.queue[0].requester_id != inter.user.id:
        return ""
    if not p.can_skip_now(inter.user):
        return "ℹ️ เพลงที่เล่นอยู่เป็นของคนอื่น เลยใส่เป็นเพลงถัดไปแทน (กด ⏭ เพื่อโหวตข้าม)"
    p.note(f"▶️ {who_label(inter.user)} เล่น {p.queue[0].name[:40]} ทันที")
    p.skip()
    return ""


LYRICS_COLOR = 0xEB459E  # lyrics of a song that is not playing (no cover colour to use)


def _lyric_key(line: str) -> str:
    return " ".join(line.casefold().split()).strip(" .,!?…~-")


def _fold_lines(lines: list[str]) -> list[str]:
    """'โอ้ โอ้' twice in a row -> one line with '(×2)'."""
    out, prev, n = [], None, 0
    for line in lines + [None]:
        if line is not None and prev is not None and _lyric_key(line) == _lyric_key(prev):
            n += 1
            continue
        if prev is not None:
            out.append(prev + (f" (×{n})" if n > 1 else ""))
        prev, n = line, 1
    return out


def lyric_blocks(plain: str) -> list[tuple[str, str]]:
    """The lyrics as stanzas: (markdown, plain text). A stanza sung again right after itself
    shows once with '×N'; a stanza that comes back (the chorus) is bold under a small
    '🔁 ท่อนฮุก' label, so the main part stands out."""
    stanzas, cur = [], []
    for line in plain.splitlines():
        if line.strip():
            cur.append(line.strip())
        elif cur:
            stanzas.append(cur)
            cur = []
    if cur:
        stanzas.append(cur)
    keys = [tuple(_lyric_key(x) for x in st) for st in stanzas]
    seen = Counter(keys)
    out, i = [], 0
    while i < len(stanzas):
        n = 1
        while i + n < len(stanzas) and keys[i + n] == keys[i]:
            n += 1
        lines = _fold_lines(stanzas[i])
        safe = [discord.utils.escape_markdown(x) for x in lines]
        chorus = seen[keys[i]] > 1 and len(stanzas[i]) > 1
        if chorus:
            label = "-# 🔁 ท่อนฮุก" + (f" · ร้อง {n} รอบ" if n > 1 else "")
            text = label + "\n" + "\n".join(f"**{x}**" for x in safe)
        else:
            text = "\n".join(safe) + (f"\n-# ×{n}" if n > 1 else "")
        out.append((text, "\n".join(lines)))
        i += n
    return out


def _md_plain_lines(text: str, plain: str) -> list[Optional[str]]:
    """Pair each markdown line of a stanza with its plain line (None for the labels)."""
    rest = iter(plain.splitlines())
    return [None if md.startswith("-# ") else next(rest, None) for md in text.splitlines()]


def build_lyrics_pages(lyr: Lyrics, max_chars: int = 1800,
                       color: Optional[int] = None) -> list[discord.Embed]:
    return [e for e, _ in lyrics_pages(lyr, max_chars, color)]


def lyrics_pages(lyr: Lyrics, max_chars: int = 1800,
                 color: Optional[int] = None) -> list[tuple[discord.Embed, str]]:
    """Lyrics pages (embed, the page's plain lines for the quote card). Stanzas are kept
    whole on a page; the colour is the song's cover colour when it is playing."""
    color = LYRICS_COLOR if color is None else color
    head = f"🎤 {lyr.title}" + (f" · {lyr.artist}" if lyr.artist else "")
    if lyr.instrumental and not lyr.plain:
        return [(discord.Embed(title=head[:256], description="เพลงบรรเลง ไม่มีเนื้อร้อง",
                               color=color), "")]
    blocks = []
    for text, plain in lyric_blocks(lyr.plain):
        if len(text) <= max_chars:
            blocks.append((text, plain))
            continue
        # one huge stanza (lyrics without blank lines): cut it between lines
        part, part_raw = "", []
        for md, line in zip(text.splitlines(), _md_plain_lines(text, plain)):
            if part and len(part) + len(md) + 1 > max_chars:
                blocks.append((part, "\n".join(part_raw)))
                part, part_raw = "", []
            part += ("\n" if part else "") + md
            if line is not None:
                part_raw.append(line)
        if part:
            blocks.append((part, "\n".join(part_raw)))
    chunks, cur, raw = [], "", ""
    for text, plain in blocks:
        if cur and len(cur) + len(text) + 2 > max_chars:
            chunks.append((cur, raw))
            cur, raw = "", ""
        cur += ("\n\n" if cur else "") + text
        raw += ("\n" if raw else "") + plain
    if cur.strip():
        chunks.append((cur, raw))
    chunks = chunks or [("(ไม่มีเนื้อเพลง)", "")]
    tag = " · synced" if lyr.synced else ""
    return [(discord.Embed(title=head[:256], description=c[:4096], color=color)
             .set_footer(text=f"lrclib.net{tag} · หน้า {i}/{len(chunks)}"), r)
            for i, (c, r) in enumerate(chunks, 1)]


def build_lyrics_now(lyr: Lyrics, position: float, color: Optional[int] = None) -> discord.Embed:
    """Lines around the one playing now (synced lyrics only)."""
    idx = lyr.line_at(position)
    lines = []
    for i in range(max(idx - 3, 0), min(idx + 7, len(lyr.synced))):
        text = lyr.synced[i][1] or "♪"
        lines.append(f"**▶ {text}**" if i == idx else (f"-# {text}" if i < idx else text))
    if idx < 0:
        lines.insert(0, "-# ♪ ยังไม่ถึงท่อนร้อง")
    e = discord.Embed(title=f"🎤 {lyr.title}"[:256], description="\n".join(lines)[:4000],
                      color=LYRICS_COLOR if color is None else color)
    e.set_footer(text=f"ตอนนี้ {fmt_time(position)} · กด 📍 อีกครั้งเพื่ออัปเดต")
    return e


class LyricsView(PagesView):
    """Lyrics pages, plus a 'now' button that jumps to the current line (synced only)."""

    def __init__(self, lyr: Lyrics, author_id: int, p: Optional["GuildPlayer"] = None,
                 url: str = ""):
        # the song's colour while it plays, like the panel
        self.color = (track_color(p.current) if p and p.current and url
                      and p.current.url == url else None)
        pages = lyrics_pages(lyr, color=self.color)
        super().__init__([e for e, _ in pages], author_id, timeout=600,
                         deny_text="ใช้ `/lyrics` เปิดของตัวเอง")
        self.raw = [r for _, r in pages]  # each page's lines without the markdown
        self.lyr, self.p, self.url = lyr, p, url
        if not (lyr.synced and p and url):
            self.remove_item(self.now_btn)
        if not (lyr.plain or lyr.synced):
            self.remove_item(self.quote_btn)

    @discord.ui.button(emoji="🖼", label="การ์ดเนื้อเพลง", style=discord.ButtonStyle.secondary)
    async def quote_btn(self, inter: discord.Interaction, _):
        p = self.p
        playing = bool(p and p.current and p.current.url == self.url)
        track = p.current if playing else Track(title=self.lyr.title, url=self.url or "",
                                                artist=self.lyr.artist)
        position = p.position if playing and self.lyr.synced else None
        page = self.raw[self.index] if self.index < len(self.raw) else ""
        view = QuoteView(self.lyr, track, inter.user.id, position, page)
        await inter.response.send_message(
            f"🖼 เลือก 1–{QUOTE_MAX} บรรทัดที่จะทำเป็นการ์ด แล้วบอทจะส่งลงห้องนี้", view=view,
            ephemeral=True)

    @discord.ui.button(emoji="📍", label="ท่อนปัจจุบัน", style=discord.ButtonStyle.secondary)
    async def now_btn(self, inter: discord.Interaction, _):
        p = self.p
        if not p or not p.current or p.current.url != self.url:
            return await inter.response.send_message("เพลงเปลี่ยนแล้ว", ephemeral=True)
        await inter.response.edit_message(embed=build_lyrics_now(self.lyr, p.position, self.color),
                                          view=self)


QUOTE_MAX = 4  # lyric lines on one shared card


class QuoteView(discord.ui.View):
    """Pick one to four lyric lines; the bot posts them as an image card in the channel,
    with a ▶ button that jumps to that part while the song plays."""

    def __init__(self, lyr: Lyrics, track: Track, author_id: int,
                 position: Optional[float], page_text: str):
        super().__init__(timeout=180)
        self.track, self.author_id = track, author_id
        options, self.lines, self.times = [], [], []
        if position is not None and lyr.synced:  # around the line playing now
            now = lyr.line_at(position + LYRICS_LEAD)
            start = max(now - 4, 0)
            for i in range(start, min(start + 25, len(lyr.synced))):
                at, text = lyr.synced[i]
                if text.strip():
                    self.lines.append(text.strip())
                    self.times.append(at)
                    options.append(discord.SelectOption(
                        label=text.strip()[:100], description=fmt_time(at),
                        value=str(len(self.lines) - 1), default=i == now))
        else:  # the lyrics page being read; synced lyrics still tell when a line is sung
            sung = {}
            for at, text in lyr.synced:
                sung.setdefault(_lyric_key(text), at)
            for text in page_text.splitlines():
                if text.strip() and len(self.lines) < 25:
                    at = sung.get(_lyric_key(text))
                    self.lines.append(text.strip())
                    self.times.append(at)
                    options.append(discord.SelectOption(
                        label=text.strip()[:100], value=str(len(self.lines) - 1),
                        description=fmt_time(at) if at is not None else None))
        self.pick.options = options or [discord.SelectOption(label="♪", value="-1")]
        self.pick.max_values = min(QUOTE_MAX, len(self.pick.options))

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        return inter.user.id == self.author_id

    @discord.ui.select(placeholder=f"เลือกบรรทัด (ได้ถึง {QUOTE_MAX} บรรทัดติดกัน)", min_values=1)
    async def pick(self, inter: discord.Interaction, select: discord.ui.Select):
        idx = sorted(int(v) for v in select.values if int(v) >= 0)
        lines = [self.lines[i] for i in idx] or ["♪"]
        at = self.times[idx[0]] if idx and idx[0] < len(self.times) else None
        await inter.response.edit_message(content="🖼 กำลังทำการ์ด…", view=None)
        from core.card import EXT, display_title, make_quote_card
        data = await make_quote_card(lines, self.track, at)
        if not data:
            return await inter.edit_original_response(content="ทำการ์ดไม่สำเร็จ ลองใหม่อีกครั้ง")
        file = discord.File(io.BytesIO(data), filename=f"lyrics.{EXT}",
                            description=" / ".join(lines)[:1000])
        view = part_view(self.track, at)
        try:
            title = _plain(display_title(self.track), 80)
            kwargs = {"view": view} if view else {}
            await inter.channel.send(f"🎤 {who_label(inter.user)} แชร์ท่อนจาก **{title}**",
                                     file=file, allowed_mentions=discord.AllowedMentions.none(),
                                     **kwargs)
        except discord.HTTPException:
            return await inter.edit_original_response(content="ส่งการ์ดในห้องนี้ไม่ได้")
        await inter.edit_original_response(content="✅ ส่งการ์ดเนื้อเพลงแล้ว")


def build_added_embed(p: "GuildPlayer", tracks: list[Track], index: int,
                      label: str = "") -> discord.Embed:
    """Short reply for /play: one line with the queue position and when it will play
    (a timestamp that counts down by itself). index: 0-based queue position."""
    first = tracks[0]
    starts_now = not p.current and index == 0
    secs = p.eta(index)
    eta = relative_ts(secs)
    e = discord.Embed(color=track_color(first))
    if len(tracks) == 1:
        if starts_now:
            e.description = f"{label or '▶️'} {_link(first)} `{first.fmt_duration()}` · กำลังจะเล่น"
        else:
            where = "ถัดไป" if index == 0 else f"คิวที่ {index + 1}"
            e.description = (f"{label or '➕'} {_link(first)} `{first.fmt_duration()}`"
                             f" · {where}" + (f" · เล่น {eta}" if secs is not None else ""))
        return e
    total = (f" · รวม {fmt_time(sum(t.duration for t in tracks))}"
             if all(t.duration for t in tracks) else "")
    when = "เริ่มเลย" if starts_now else f"คิวที่ {index + 1}–{index + len(tracks)}"
    if not starts_now and secs is not None:
        when += f" · เริ่ม {eta}"
    head = f"{label} · " if label else "➕ "
    names = " · ".join(_plain(t.name, 40) for t in tracks[:3])
    more = f" และอีก {len(tracks) - 3} เพลง" if len(tracks) > 3 else ""
    e.description = f"{head}เพิ่ม **{len(tracks)} เพลง**{total} · {when}\n-# {names}{more}"
    return e


class SearchView(discord.ui.View):
    """Dropdown of search results."""

    def __init__(self, tracks: list[Track], author_id: int,
                 on_pick: Callable[[discord.Interaction, Track], Awaitable[None]]):
        super().__init__(timeout=60)
        self.tracks = tracks
        self.author_id = author_id
        self.on_pick = on_pick
        select = discord.ui.Select(
            placeholder="เลือกเพลง",
            options=[
                discord.SelectOption(label=t.name[:100], description=t.fmt_duration(),
                                     value=str(i))
                for i, t in enumerate(tracks)
            ],
        )
        select.callback = self._picked
        self.add_item(select)

    async def _picked(self, inter: discord.Interaction):
        if inter.user.id != self.author_id:
            return await inter.response.send_message("ไม่ใช่ผลค้นของคุณ", ephemeral=True)
        track = self.tracks[int(inter.data["values"][0])]
        self.stop()
        await self.on_pick(inter, track)


# ---------------------------------------------------------- panel actions
# Shared by the full panel and the compact panel. Return text = ephemeral reply.

def who_label(user) -> str:
    """How a person is named on the panel's status line: a mention pill in the Groove
    look (it renders there and never pings: panels send no mentions), else the name."""
    if config.UI_STYLE == "groove" and getattr(user, "id", None):
        return f"<@{user.id}>"
    name = getattr(user, "display_name", None) or getattr(user, "name", "?")
    return name if len(name) <= 20 else name[:19] + "…"


def _who(inter: discord.Interaction) -> str:
    return who_label(inter.user)


async def _act(inter: discord.Interaction, action: Callable[["GuildPlayer"], Optional[str]],
               label: str, note=None):
    """note: text for the panel's status line ({who} = the person who pressed), or a
    function (player, reply) -> text, evaluated after the action."""
    try:
        p = control(inter)
    except UserError as exc:
        return await inter.response.send_message(str(exc), ephemeral=True)
    msg = action(p)
    if callable(note):
        note = note(p, msg)
    if note and hasattr(p, "note"):
        p.note(note.replace("{who}", _who(inter)))
    if msg:
        await inter.response.send_message(msg, ephemeral=True)
    else:
        await inter.response.defer()
    await audit_inter(inter, label)
    await p.update_panel()


async def act_prev(inter):
    await _act(inter, lambda p: None if p.previous() else "ไม่มีเพลงก่อนหน้า", "previous",
               note=lambda p, msg: None if msg else "⏮ {who} ย้อนเพลงก่อนหน้า")


async def act_pause(inter):
    await _act(inter, lambda p: (p.toggle_pause(), None)[1], "pause",
               note=lambda p, _: "⏸ {who} หยุดเพลง" if p.is_paused else "▶️ {who} เล่นต่อ")


async def act_skip(inter):
    def note(p, msg):
        if msg and msg.startswith("🗳"):
            return "🗳 {who} โหวตข้าม"
        return "⏭ {who} ข้ามเพลง"
    await _act(inter, lambda p: p.vote_skip(inter.user), "skip", note=note)


async def act_mute(inter):
    await _act(inter, lambda p: (p.toggle_mute(), None)[1], "mute",
               note=lambda p, _: ("🔇 {who} ปิดเสียง" if p.volume <= 0
                                  else f"🔊 {{who}} เปิดเสียง ({int(round(p.volume * 100))}%)"))


async def act_seek(inter, direction: int):
    """⏪ (-1) / ⏩ (+1): 10 seconds, 30 on a long talk clip."""
    step = {}

    def run(p: "GuildPlayer") -> Optional[str]:
        if not p.current or not p.current.duration:
            return "เพลงนี้กรอไม่ได้"
        step["s"] = delta = direction * seek_step(p)
        target = max(p.position + delta, 0)
        if target >= p.current.duration - 1:
            return "เกินความยาวเพลง ใช้ ⏭ แทน"
        p.restart_at(target)
        return None

    def note(p, msg):
        if msg:
            return None
        s = abs(step["s"])
        return f"⏪ {{who}} ย้อน {s} วิ" if direction < 0 else f"⏩ {{who}} ข้ามไป {s} วิ"
    await _act(inter, run, f"seek {direction:+d}", note=note)


async def act_lyrics(inter):
    from core import lyrics
    p = inter.client.players.get(inter.guild_id)
    if not p or not p.current:
        return await inter.response.send_message("ไม่มีเพลงเล่นอยู่", ephemeral=True)
    track = p.current
    await inter.response.defer(ephemeral=True, thinking=True)
    lyr = await lyrics.find(track)
    if not lyr:
        return await inter.followup.send(
            f"🎤 หาเนื้อเพลง **{_plain(track.name, 80)}** ไม่เจอในฐานข้อมูลเนื้อเพลง"
            " (เพลงรีมิกซ์หรือเพลงใหม่มักยังไม่มี) ลอง `/lyrics ชื่อเพลง ศิลปิน`", ephemeral=True)
    view = LyricsView(lyr, inter.user.id, p, track.url)
    await inter.followup.send(embed=view.pages[0], view=view, ephemeral=True)


async def act_add(inter):
    await inter.response.send_modal(AddSongModal())


LYRICS_MISSING = {
    "none": "🎙 เพลงนี้หาเนื้อเพลงไม่เจอ เลยเปิดเนื้อสดไม่ได้ ลอง `/lyrics ชื่อเพลง` ดูนะ",
    "plain": "🎙 เพลงนี้มีแต่เนื้อเพลงธรรมดา ไม่มีเวลากำกับแต่ละบรรทัด เลยเปิดเนื้อสดไม่ได้"
             " กด 🎤 เพื่ออ่านเนื้อเต็มได้",
}


async def act_live_lyrics(inter):
    def run(p: "GuildPlayer") -> str:
        state = p.lyrics_state() if hasattr(p, "lyrics_state") else "unknown"
        if not p.live_lyrics and state in LYRICS_MISSING:
            return LYRICS_MISSING[state]
        on = p.toggle_live_lyrics()
        return "🎙 เปิดเนื้อเพลงสดบน panel" if on else "ปิดเนื้อเพลงสดแล้ว"
    def note(p, msg):
        if msg in LYRICS_MISSING.values():
            return None
        return "🎙 {who} เปิดเนื้อสด" if p.live_lyrics else "🎙 {who} ปิดเนื้อสด"
    await _act(inter, run, "live lyrics", note=note)


HOT_LEAD = 3  # seconds before the most replayed point, so the hit part starts cleanly


def hot_position(p: "GuildPlayer") -> Optional[float]:
    """Where 🔥 jumps: just before the part people replay most (None = no data)."""
    from core.card import heat_peak
    t = p.current
    if not t or not t.duration or getattr(p, "loading", False):
        return None
    peak = heat_peak(tuple(t._heatmap or ()))
    if peak is None:
        return None
    return max(peak * t.duration - HOT_LEAD, 0.0)


async def act_hot(inter):
    def run(p: "GuildPlayer") -> Optional[str]:
        target = hot_position(p)
        if target is None:
            return "เพลงนี้ไม่มีข้อมูลท่อนฮิต"
        p.restart_at(target)
        return None
    await _act(inter, run, "hot part",
               note=lambda p, msg: None if msg else "🔥 {who} ข้ามไปท่อนฮิต")


async def _toggle_autoplay(inter, p: "GuildPlayer") -> str:
    on = p.toggle_autoplay()
    p.note(f"📻 {_who(inter)} {'เปิด' if on else 'ปิด'} autoplay")
    await inter.client.db.set_setting(inter.guild_id, "autoplay", int(on))
    await audit_inter(inter, "autoplay", "on" if on else "off")
    return ("📻 เปิด autoplay: คิวหมดแล้วบอทจะเล่นเพลงคล้ายกันต่อเอง" if on
            else "📻 ปิด autoplay แล้ว")


async def act_autoplay(inter):
    try:
        p = control(inter)
    except UserError as exc:
        return await inter.response.send_message(str(exc), ephemeral=True)
    text = await _toggle_autoplay(inter, p)
    await inter.response.send_message(text, ephemeral=True)
    await p.update_panel()


# ⚙️ quick settings on the panel (admins): key, emoji, label
QUICK_SETTINGS = (
    ("normalize", "⚖️", "ความดังเท่ากันทุกเพลง"),
    ("fair", "🤝", "ผลัดกันเล่น (คิวสลับตามคน)"),
    ("voteskip", "🗳️", "โหวตข้ามเพลง"),
    ("247", "🌙", "อยู่ในห้องตลอด 24/7"),
    ("theme", "🎨", "เปลี่ยนธีมการ์ด"),
    ("layout", "🖼️", "สลับการ์ด แนวนอน / จัตุรัส"),
    ("compact", "📱", "Panel แบบย่อ (มือถือ)"),
)
QUICK_TOGGLES = {"normalize": ("normalize", "normalize"), "fair": ("fair_queue", "fair_queue"),
                 "voteskip": ("vote_skip_enabled", "vote_skip"), "247": ("stay_247", "stay_247"),
                 "compact": ("compact", "compact")}  # key: (player attribute, settings column)


def _quick_options(p: "GuildPlayer") -> list[discord.SelectOption]:
    """The quick settings, each saying what it is now and what picking it does."""
    from cogs.settings import LAYOUT_NAMES, THEME_NAMES
    out = []
    for key, emoji, label in QUICK_SETTINGS:
        if key in QUICK_TOGGLES:
            on = bool(getattr(p, QUICK_TOGGLES[key][0], False))
            desc = f"ตอนนี้: {'เปิด' if on else 'ปิด'} · เลือกเพื่อ{'ปิด' if on else 'เปิด'}"
        elif key == "theme":
            theme = getattr(p, "card_theme", "blur")
            desc = f"ตอนนี้: {THEME_NAMES.get(theme, theme)} · เลือกเพื่อเปลี่ยน"
        else:
            layout = getattr(p, "card_layout", "wide")
            desc = f"ตอนนี้: {LAYOUT_NAMES.get(layout, layout)}"
        out.append(discord.SelectOption(label=label, value=key, emoji=emoji, description=desc))
    return out


QUICK_ADMIN_ONLY = "⚙️ ตั้งค่าเซิร์ฟเวอร์ใช้ได้เฉพาะแอดมิน (สิทธิ์จัดการเซิร์ฟเวอร์)"


async def act_quick(inter, key: str):
    """A quick setting picked from an old panel's menu (panels now open SettingsView)."""
    try:
        p = control(inter)
    except UserError as exc:
        return await inter.response.send_message(str(exc), ephemeral=True)
    if not p.is_admin(inter.user):
        return await inter.response.send_message(QUICK_ADMIN_ONLY, ephemeral=True)
    text, repost = await _apply_quick(inter, p, key)
    await inter.response.send_message(f"⚙️ {text}", ephemeral=True)
    await _after_quick(p, repost)


async def _after_quick(p: "GuildPlayer", repost: bool):
    if repost and p.current:
        await p.send_panel()  # compact changes the panel's whole shape
    else:
        await p.update_panel()


async def _apply_quick(inter, p: "GuildPlayer", key: str) -> tuple[str, bool]:
    """Apply one quick setting: the same change as the matching /settings command.
    Returns (what changed, whether the panel must be posted again)."""
    from cogs.settings import LAYOUT_NAMES, THEME_NAMES
    from core.card import LAYOUTS, THEMES
    db = inter.client.db
    repost = False
    if key in QUICK_TOGGLES:
        attr, column = QUICK_TOGGLES[key]
        on = not bool(getattr(p, attr, False))
        if key == "normalize":
            p.set_normalize(on)
        else:
            setattr(p, attr, on)
        repost = key == "compact"
        await db.set_setting(inter.guild_id, column, int(on))
        label = next(lb for k, _, lb in QUICK_SETTINGS if k == key)
        text = f"{label}: {'เปิด' if on else 'ปิด'}"
    elif key == "theme":
        p.card_theme = THEMES[(THEMES.index(p.card_theme) + 1) % len(THEMES)
                              if p.card_theme in THEMES else 0]
        await db.set_setting(inter.guild_id, "card_theme", p.card_theme)
        text = f"ธีมการ์ด: {THEME_NAMES.get(p.card_theme, p.card_theme)}"
    elif key == "layout":
        p.card_layout = "square" if p.card_layout == "wide" else LAYOUTS[0]
        await db.set_setting(inter.guild_id, "card_layout", p.card_layout)
        text = f"รูปทรงการ์ด: {LAYOUT_NAMES.get(p.card_layout, p.card_layout)}"
    else:
        return "ไม่รู้จักตัวเลือกนี้", False
    p.note(f"⚙️ {_who(inter)} ตั้ง {text}")
    await audit_inter(inter, "quick setting", text)
    return text, repost


SHUFFLED = "🔀 สลับคิวแล้ว · เพลงศิลปินเดียวกันจะไม่อยู่ติดกัน (/undo เพื่อย้อน)"

SETTINGS_TEXT = ("### ⚙️ ตั้งค่า\n📻 **Autoplay** ทุกคนกดได้: คิวหมดแล้วเล่นเพลงคล้ายกันต่อเอง\n"
                 "🎛️ **เอฟเฟกต์เสียง** ทุกคนเลือกได้ ใช้กับทุกเพลงจนกว่าจะเปลี่ยนหรือบอทออกจากห้อง\n"
                 "-# ตั้งค่าเซิร์ฟเวอร์ในเมนูด้านล่างใช้ได้เฉพาะแอดมิน")


def _effect_options(p: "GuildPlayer") -> list[discord.SelectOption]:
    from core.player import EFFECTS
    now = getattr(p, "effect", "off")
    return [discord.SelectOption(label=name, value=key, emoji=emoji, default=key == now,
                                 description=EFFECT_INFO.get(key))
            for key, (name, emoji, *_) in EFFECTS.items()]


EFFECT_INFO = {"off": "เสียงเดิมของเพลง", "bass": "เบสหนักขึ้น",
               "nightcore": "เร็วขึ้น 1.25 เท่า เสียงสูงขึ้น",
               "slowed": "ช้าลง เสียงต่ำลง มีเสียงก้อง", "8d": "เสียงวนรอบหัว ใส่หูฟังจะชัดสุด"}


def apply_effect(p: "GuildPlayer", key: str) -> str:
    """Switch the effect; returns what the status line and reply say ({who} = presser)."""
    if not p.set_effect(key):
        raise UserError("ไม่รู้จักเอฟเฟกต์นี้")
    return _effect_note(p)


class SettingsView(discord.ui.View):
    """⚙️ on the panel: settings only the presser sees. Autoplay for everyone, the server
    settings for admins (the menu is greyed out for others)."""

    def __init__(self, p: "GuildPlayer", user):
        super().__init__(timeout=180)
        self.p = p
        on = bool(getattr(p, "autoplay", False))
        self.autoplay_btn.label = f"Autoplay: {'เปิด' if on else 'ปิด'}"
        self.autoplay_btn.style = (discord.ButtonStyle.primary if on
                                   else discord.ButtonStyle.secondary)
        self.quick.options = _quick_options(p)
        self.effect.options = _effect_options(p)
        if not p.is_admin(user):
            self.quick.disabled = True
            self.quick.placeholder = "⚙️ ตั้งค่าเซิร์ฟเวอร์ (เฉพาะแอดมิน)"

    async def _refresh(self, inter, text: str):
        await inter.response.edit_message(content=f"{SETTINGS_TEXT}\n\n✅ {text}",
                                          view=SettingsView(self.p, inter.user))

    @discord.ui.button(emoji="📻", label="Autoplay", row=0)
    async def autoplay_btn(self, inter: discord.Interaction, _):
        try:
            p = control(inter)
        except UserError as exc:
            return await inter.response.send_message(str(exc), ephemeral=True)
        text = await _toggle_autoplay(inter, p)
        await self._refresh(inter, text)
        await p.update_panel()

    @discord.ui.select(placeholder="🎛️ เอฟเฟกต์เสียง", row=1,
                       options=[discord.SelectOption(label="-", value="-")])
    async def effect(self, inter: discord.Interaction, select: discord.ui.Select):
        try:
            p = control(inter)
            note = apply_effect(p, select.values[0])
        except UserError as exc:
            return await inter.response.send_message(str(exc), ephemeral=True)
        p.note(note.replace("{who}", _who(inter)))
        await audit_inter(inter, "effect", select.values[0])
        await self._refresh(inter, note.replace("{who} ", ""))
        await p.update_panel()

    @discord.ui.select(placeholder="⚙️ ตั้งค่าเซิร์ฟเวอร์ (แอดมิน)", row=2,
                       options=[discord.SelectOption(label="-", value="-")])
    async def quick(self, inter: discord.Interaction, select: discord.ui.Select):
        try:
            p = control(inter)
        except UserError as exc:
            return await inter.response.send_message(str(exc), ephemeral=True)
        if not p.is_admin(inter.user):
            return await inter.response.send_message(QUICK_ADMIN_ONLY, ephemeral=True)
        text, repost = await _apply_quick(inter, p, select.values[0])
        await self._refresh(inter, text)
        await _after_quick(p, repost)


async def act_effect(inter, key: str):
    """🎛️ menu on the mobile panel: switch the sound effect for everyone."""
    def run(p: "GuildPlayer") -> Optional[str]:
        try:
            apply_effect(p, key)
        except UserError as exc:
            return str(exc)
        return None
    await _act(inter, run, f"effect {key}", note=lambda p, msg: None if msg else _effect_note(p))


def _effect_note(p: "GuildPlayer") -> str:
    """Status line text for the effect now on ({who} = the person who picked it)."""
    from core.player import EFFECTS
    if p.effect == "off":
        return "🎵 {who} ปิดเอฟเฟกต์เสียง"
    name, emoji, *_ = EFFECTS[p.effect]
    return f"{emoji} {{who}} เปิดเอฟเฟกต์ {name}"


PART_PREFIX = "mb:part:"  # ▶ under a shared lyric card: mb:part:<seconds>:<song key>
PART_LEAD = 1.5          # start a little before the line so it is heard from its start


def song_key(track: Track) -> str:
    """Short id of a song for a button's custom_id: the YouTube id, else a URL hash."""
    import hashlib
    return track.video_id or hashlib.sha1((track.url or "").encode()).hexdigest()[:12]


def part_view(track: Track, at: Optional[float]) -> Optional[discord.ui.View]:
    """'▶ ฟังท่อนนี้ (1:23)': handled by act_part through its custom_id, so it keeps working
    after the view times out and after a restart."""
    if at is None or not (track.url or "").startswith("http"):
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(emoji="▶️", label=f"ฟังท่อนนี้ ({fmt_time(at)})",
                                    style=discord.ButtonStyle.secondary,
                                    custom_id=f"{PART_PREFIX}{int(at)}:{song_key(track)}"))
    return view


async def act_part(inter: discord.Interaction, custom_id: str):
    """Jump to a shared lyric card's part, when that song is the one playing."""
    try:
        at_text, key = custom_id[len(PART_PREFIX):].split(":", 1)
        at = int(at_text)
    except ValueError:
        return await inter.response.send_message("ปุ่มนี้ใช้ไม่ได้แล้ว", ephemeral=True)

    def run(p: "GuildPlayer") -> Optional[str]:
        t = p.current
        if not t or song_key(t) != key:
            return "▶ ใช้ได้ตอนเพลงนี้กำลังเล่นอยู่ ใส่เพลงนี้เข้าคิวก่อนแล้วกดอีกครั้ง"
        if not t.duration or at >= t.duration:
            return "ข้ามไปท่อนนี้ไม่ได้"
        p.restart_at(max(at - PART_LEAD, 0))
        return None
    await _act(inter, run, f"part {at}",
               note=lambda p, msg: None if msg else f"🎤 {{who}} ข้ามไปท่อนที่ {fmt_time(at)}")


async def act_settings(inter):
    try:
        p = control(inter)
    except UserError as exc:
        return await inter.response.send_message(str(exc), ephemeral=True)
    await inter.response.send_message(SETTINGS_TEXT, view=SettingsView(p, inter.user),
                                      ephemeral=True)


VOLUME_BACK = "back"  # volume menu: return to the volume before the last change


async def act_volume(inter, value: str):
    def run(p: "GuildPlayer") -> Optional[str]:
        if value == VOLUME_BACK:
            if p.prev_volume is None:
                return "ไม่มีความดังก่อนหน้านี้"
            p.set_volume(p.prev_volume)
        else:
            p.set_volume(int(value))
        return None

    def note(p, msg):
        if msg:
            return None
        now = int(round(p.volume * 100))
        if value == VOLUME_BACK:
            return f"↩️ {{who}} กลับไปความดังเดิม {now}%"
        return f"🔊 {{who}} ปรับเสียงเป็น {now}%"
    await _act(inter, run, f"volume {value}", note=note)


STOP_CONFIRM_SECONDS = 5


async def act_stop(inter):
    try:
        p = control(inter)
    except UserError as exc:
        return await inter.response.send_message(str(exc), ephemeral=True)
    listeners = len(p.humans_in_channel())
    if listeners > 1 and not p.stop_armed():
        # others are listening: ⏹ is red and close to other buttons, so ask once more
        p.arm_stop(STOP_CONFIRM_SECONDS)
        await inter.response.send_message(
            f"⏹ มีคนฟังอยู่ {listeners} คน กด ⏹ อีกครั้งภายใน {STOP_CONFIRM_SECONDS} วินาที"
            " เพื่อหยุดและล้างคิว", ephemeral=True)
        await p.update_panel()
        return
    await inter.response.defer()
    await audit_inter(inter, "stop")
    await p.destroy()


async def act_queue(inter):
    p = inter.client.players.get(inter.guild_id)
    if not p or (not p.current and not p.queue):
        return await inter.response.send_message("คิวว่าง", ephemeral=True)
    view = QueueView(p, inter.user.id)
    await inter.response.send_message(embed=view.pages[0], view=view, ephemeral=True)


async def audit_inter(inter: discord.Interaction, action: str, detail: str = ""):
    try:
        await inter.client.db.audit(inter.guild_id, inter.user.id, action, detail)
    except Exception:
        pass


VOLUME_PRESETS = (10, 25, 50, 75, 100, 125)
VOLUME_NAMES = {  # preset: (name, description, emoji). Volume follows loudness as heard
    10: ("เบามาก", "ได้ยินแผ่วๆ เปิดคลอระหว่างคุยกัน", "🔈"),          # (player.loudness_gain)
    25: ("เบา", "ดังราวหนึ่งในสี่ของเสียงเต็ม ฟังตอนทำงาน", "🔈"),
    50: ("กลาง", "ดังราวครึ่งหนึ่งของเสียงเต็ม", "🔉"),
    75: ("ค่อนข้างดัง", "ดังราวสามในสี่ของเสียงเต็ม", "🔉"),
    100: ("เต็ม", "เสียงเต็ม ไม่ลดไม่เพิ่ม", "🔊"),
    125: ("ดังพิเศษ", "ขยายเกินเสียงเต็ม เพลงที่ดังอยู่แล้วอาจแตก", "📢"),
}


def _volume_option(v: int) -> discord.SelectOption:
    name, desc, emoji = VOLUME_NAMES[v]
    return discord.SelectOption(label=f"{v}% · {name}", value=str(v), description=desc,
                                emoji=emoji)
SEEK_STEP = 10       # seconds for the ⏪ ⏩ panel buttons
TALK_SEEK_STEP = 30  # ...on a long talk clip (a story, a podcast)


def seek_step(p: "GuildPlayer") -> int:
    from core.card import is_talk
    return TALK_SEEK_STEP if is_talk(p.current) else SEEK_STEP


def _seek_state(view, p: "GuildPlayer"):
    """⏪ needs something to rewind, ⏩ needs room before the end."""
    t = p.current
    if not t or not t.duration or p.loading:
        view.rewind.disabled = view.forward.disabled = True
        return
    pos = p.position
    view.rewind.disabled = pos < 1
    view.forward.disabled = pos + seek_step(p) >= t.duration - 1


def _lyrics_state(view, p: "GuildPlayer"):
    """🎤 and 🎙 stay pressable without lyrics: pressing says why (see LYRICS_MISSING).
    Green 🎙 = synced lyrics are ready to follow along; blue = live lyrics on."""
    state = p.lyrics_state() if hasattr(p, "lyrics_state") else "unknown"
    view.lyrics_btn.disabled = not p.current
    live = view.live_lyrics_btn
    live.disabled = not p.current
    if p.live_lyrics:
        live.style = discord.ButtonStyle.primary
    elif state == "synced":
        live.style = discord.ButtonStyle.success


def _counts(view, p: "GuildPlayer"):
    """Skip votes so far on ⏭ (for a short while). The queue length is on the card, not
    on 📜: the buttons stay icons only, an even 5 x 3 grid."""
    if p.skip_votes and p.current:
        view.skip.label = f"{len(p.skip_votes)}/{p.skip_need()}"
        view.skip.style = discord.ButtonStyle.primary


class PanelView(discord.ui.View):
    """Now-playing controls. Persistent (fixed custom_id), state-aware when given a player."""

    def __init__(self, p: Optional["GuildPlayer"] = None):
        super().__init__(timeout=None)
        if p is None:
            return
        self.prev.disabled = not p.history
        self.shuffle.disabled = len(p.queue) < 2
        self.queue_btn.disabled = not p.queue and not p.current
        if p.is_paused:
            self.pause.emoji = "▶️"
            self.pause.style = discord.ButtonStyle.primary
        if p.loop_mode != "off":
            self.loop.style = discord.ButtonStyle.primary
            self.loop.emoji = "🔂" if p.loop_mode == "track" else "🔁"
        self.hot_btn.disabled = hot_position(p) is None
        if getattr(p, "autoplay", False):  # ⚙️ holds the autoplay switch: blue while it is on
            self.settings_btn.style = discord.ButtonStyle.primary
        if hasattr(p, "stop_armed") and p.stop_armed():
            self.stop_btn.label = "กดอีกครั้งเพื่อหยุด"
        if p.volume <= 0:
            self.mute_btn.emoji = "🔊"
            self.mute_btn.style = discord.ButtonStyle.primary
        _seek_state(self, p)
        self.add_btn.disabled = len(p.queue) >= config.MAX_QUEUE
        _counts(self, p)
        _lyrics_state(self, p)
        if not p.current:
            for item in (self.pause, self.skip, self.mute_btn, self.volume_select):
                item.disabled = True
        elif p.loading:
            self.pause.disabled = True
        current = int(round(p.volume * 100))
        self.volume_select.placeholder = f"🔊 ระดับเสียง: {current}%"
        for opt in self.volume_select.options:
            opt.default = int(opt.value) == current
            if opt.default:  # the closed menu shows this label
                opt.label = f"ระดับเสียง: {current}% · {VOLUME_NAMES[current][0]}"
        prev = getattr(p, "prev_volume", None)
        if prev is not None and prev != current:  # undo a change in one pick
            self.volume_select.options = [discord.SelectOption(
                label=f"↩ กลับไป {prev}%", value=VOLUME_BACK, emoji="↩️",
                description="ความดังก่อนหน้านี้")] + list(self.volume_select.options)

    # row 0: transport, row 1: queue and extras, row 2: add / mute / live lyrics,
    # row 3: volume. Icons only: every button the same width, rows the same length.
    @discord.ui.button(emoji="⏮", style=discord.ButtonStyle.secondary, custom_id="mb:prev", row=0)
    async def prev(self, inter, _):
        await act_prev(inter)

    @discord.ui.button(emoji="⏪", style=discord.ButtonStyle.secondary, custom_id="mb:rewind", row=0)
    async def rewind(self, inter, _):
        await act_seek(inter, -1)

    @discord.ui.button(emoji="⏯", style=discord.ButtonStyle.secondary, custom_id="mb:pause", row=0)
    async def pause(self, inter, _):
        await act_pause(inter)

    @discord.ui.button(emoji="⏩", style=discord.ButtonStyle.secondary, custom_id="mb:forward", row=0)
    async def forward(self, inter, _):
        await act_seek(inter, 1)

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary, custom_id="mb:skip", row=0)
    async def skip(self, inter, _):
        await act_skip(inter)

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.danger, custom_id="mb:stop", row=1)
    async def stop_btn(self, inter, _):
        await act_stop(inter)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="mb:loop", row=1)
    async def loop(self, inter, _):
        await _act(inter, lambda p: f"วนซ้ำ: {LOOP_ICON[p.cycle_loop()]}", "loop",
                   note=lambda p, _: f"🔁 {{who}} ตั้งวนซ้ำ: {LOOP_ICON[p.loop_mode]}")

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="mb:shuffle", row=1)
    async def shuffle(self, inter, _):
        await _act(inter, lambda p: (p.shuffle(), SHUFFLED)[1], "shuffle",
                   note="🔀 {who} สลับคิว")

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary, custom_id="mb:queue", row=1)
    async def queue_btn(self, inter, _):
        await act_queue(inter)

    @discord.ui.button(emoji="🎤", style=discord.ButtonStyle.secondary, custom_id="mb:lyrics", row=1)
    async def lyrics_btn(self, inter, _):
        await act_lyrics(inter)

    @discord.ui.button(emoji="➕", style=discord.ButtonStyle.success,
                       custom_id="mb:add", row=2)
    async def add_btn(self, inter, _):
        await act_add(inter)

    @discord.ui.button(emoji="🔇", style=discord.ButtonStyle.secondary,
                       custom_id="mb:mute", row=2)
    async def mute_btn(self, inter, _):
        await act_mute(inter)

    @discord.ui.button(emoji="🔥", style=discord.ButtonStyle.secondary, custom_id="mb:hot", row=2)
    async def hot_btn(self, inter, _):
        await act_hot(inter)

    @discord.ui.button(emoji="⚙️", style=discord.ButtonStyle.secondary, custom_id="mb:settings",
                       row=2)
    async def settings_btn(self, inter, _):
        await act_settings(inter)

    @discord.ui.button(emoji="🎙", style=discord.ButtonStyle.secondary,
                       custom_id="mb:livelyrics", row=2)
    async def live_lyrics_btn(self, inter, _):
        await act_live_lyrics(inter)

    @discord.ui.select(placeholder="🔊 ระดับเสียง", custom_id="mb:volpreset", row=3,
                       options=[_volume_option(v) for v in VOLUME_PRESETS])
    async def volume_select(self, inter, select: discord.ui.Select):
        await act_volume(inter, select.values[0])



class LegacyVolumeView(discord.ui.View):
    """Controls of older panels keep working until the panel is refreshed: -10 / +10 (before
    🔇), 📻 (before ⚙️ took its place) and the quick settings menu that sat under it."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(emoji="📻", custom_id="mb:autoplay")
    async def autoplay_btn(self, inter, _):
        await act_autoplay(inter)

    @discord.ui.select(custom_id="mb:quick", options=[
        discord.SelectOption(label=label, value=key) for key, _, label in QUICK_SETTINGS])
    async def quick_select(self, inter, select: discord.ui.Select):
        await act_quick(inter, select.values[0])

    @discord.ui.button(emoji="🔉", label="-10", custom_id="mb:voldown")
    async def vol_down(self, inter, _):
        await _act(inter, lambda p: (p.set_volume(int(round(p.volume * 100)) - 10), None)[1],
                   "volume -10")

    @discord.ui.button(emoji="🔊", label="+10", custom_id="mb:volup")
    async def vol_up(self, inter, _):
        await _act(inter, lambda p: (p.set_volume(int(round(p.volume * 100)) + 10), None)[1],
                   "volume +10")


class CompactPanelView(discord.ui.View):
    """One row of controls for mobile (settings compact)."""

    def __init__(self, p: Optional["GuildPlayer"] = None):
        super().__init__(timeout=None)
        if p is None:
            return
        self.prev.disabled = not p.history
        self.queue_btn.disabled = not p.queue and not p.current
        if p.is_paused:
            self.pause.emoji = "▶️"
            self.pause.style = discord.ButtonStyle.primary
        if not p.current:
            self.pause.disabled = self.skip.disabled = True
        elif p.loading:
            self.pause.disabled = True
        _counts(self, p)
        if p.queue:  # one row and a slim card without the queue chip: keep the number here
            self.queue_btn.label = f"{len(p.queue)}" if len(p.queue) < 1000 else "999+"
        self.effect.options = _effect_options(p)
        if getattr(p, "effect", "off") != "off":  # the menu says what is on, at a glance
            from core.player import EFFECTS
            name, emoji, *_ = EFFECTS[p.effect]
            self.effect.placeholder = f"🎛️ เอฟเฟกต์: {name}"

    @discord.ui.button(emoji="⏮", style=discord.ButtonStyle.secondary, custom_id="mbc:prev")
    async def prev(self, inter, _):
        await act_prev(inter)

    @discord.ui.button(emoji="⏯", style=discord.ButtonStyle.secondary, custom_id="mbc:pause")
    async def pause(self, inter, _):
        await act_pause(inter)

    @discord.ui.button(emoji="⏭", label="ข้าม", style=discord.ButtonStyle.secondary,
                       custom_id="mbc:skip")  # the most used button on a phone: a bigger target
    async def skip(self, inter, _):
        await act_skip(inter)

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.danger, custom_id="mbc:stop")
    async def stop_btn(self, inter, _):
        await act_stop(inter)

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary, custom_id="mbc:queue")
    async def queue_btn(self, inter, _):
        await act_queue(inter)

    @discord.ui.select(placeholder="🎛️ เอฟเฟกต์เสียง", custom_id="mbc:effect", row=1,
                       options=[discord.SelectOption(label=v, value=k)
                                for k, v in (("off", "ปกติ"), ("bass", "Bass boost"),
                                             ("nightcore", "Nightcore"),
                                             ("slowed", "Slowed + Reverb"), ("8d", "8D"))])
    async def effect(self, inter, select: discord.ui.Select):
        await act_effect(inter, select.values[0])


def wait_text(p: "GuildPlayer", index: int) -> str:
    """For the panel's "➕ added" note: when queue[index] (just added) will play.
    ' · ถัดไป <t:…:R>' counts down by itself on every screen."""
    if not p.current:
        return ""  # nothing playing: it starts now
    eta = p.eta(index)
    if eta is None:
        return ""
    return f" · {'ถัดไป' if index == 0 else 'ถึงคิว'} {relative_ts(eta)}"


def relative_ts(seconds_from_now: Optional[int]) -> str:
    if seconds_from_now is None:
        return "ไม่ทราบ"
    if seconds_from_now <= 0:
        return "ตอนนี้"
    return f"<t:{int(clock.now() + seconds_from_now)}:R>"
