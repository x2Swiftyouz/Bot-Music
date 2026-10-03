"""Embeds and interactive views (buttons, pagination, select menus)."""

import io
import time
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

import discord

import config
from core.checks import UserError, control
from core.lyrics import Lyrics
from core.sources import SOURCE_COLORS, Track, detect_source, fmt_time

if TYPE_CHECKING:
    from core.player import GuildPlayer

LOOP_ICON = {"off": "➡️ ปิด", "track": "🔂 เพลงเดียว", "queue": "🔁 ทั้งคิว"}


def progress_bar(pos: float, total: Optional[int], width: int = 18) -> str:
    if not total:
        return "🔴 LIVE"
    ratio = min(max(pos / total, 0), 1)
    filled = int(ratio * width)
    return "▬" * filled + "🔘" + "▬" * (width - filled)


def track_color(track: Track) -> int:
    key = track.origin if track.origin in SOURCE_COLORS else detect_source(track.url)
    return SOURCE_COLORS.get(key, SOURCE_COLORS["other"])


def _time_line(p: "GuildPlayer") -> str:
    t, pos = p.current, p.position
    right = f"`{t.fmt_duration()}`"
    if p.time_format == 1 and t.duration:
        right = f"`-{fmt_time(t.duration - pos)}`"
    elif p.time_format == 2 and t.duration:
        # Discord shows this timestamp in each viewer's own time zone
        right = f"จบ <t:{int(time.time() + t.duration - pos)}:t>"
    return f"`{fmt_time(pos)}` {progress_bar(pos, t.duration)} {right}"


def build_now_playing(p: "GuildPlayer", card: Optional[str] = None) -> discord.Embed:
    """card: filename of the attached card image ("" = none, default: the panel's own)."""
    t = p.current
    if not t:
        return build_idle_embed()
    card_name = (p.card_name if p.has_card else "") if card is None else card
    has_card = bool(card_name)
    e = discord.Embed(
        title=t.title[:250],
        url=t.url if t.url.startswith("http") else None,
        color=track_color(t),
    )
    if p.loading:
        e.set_author(name="⏳ กำลังโหลด…")
        if has_card:
            e.set_image(url=f"attachment://{card_name}")
        elif t.thumbnail:
            e.set_thumbnail(url=t.thumbnail)
        e.description = f"-# ขอโดย {t.requester_name or '-'}"
        return e
    state = "⏸ หยุดชั่วคราว" if p.is_paused else "▶️ กำลังเล่น"
    artist = f"**{t.artist}**\n" if t.artist else ""
    if p.compact:
        e.description = f"{artist}{_time_line(p)}\n-# ขอโดย {t.requester_name or '-'}"
        if p.queue:
            e.description += f" · ถัดไป: {p.queue[0].title[:50]}"
        e.set_footer(text=status_line(p))
        if karaoke := live_lyrics_text(p):
            e.description += "\n" + karaoke
        if has_card:  # slim mini card
            e.set_image(url=f"attachment://{card_name}")
        elif t.thumbnail:
            e.set_thumbnail(url=t.thumbnail)
        e.set_author(name=state)
        return e

    e.set_author(name=state)
    if has_card:  # the card already shows artist, progress and badges
        e.set_image(url=f"attachment://{card_name}")
        e.description = _time_line(p)
    else:
        if t.thumbnail:
            e.set_thumbnail(url=t.thumbnail)
        e.description = artist + _time_line(p)
    e.add_field(name="ขอโดย", value=t.requester_name or "-", inline=True)
    if p.queue:
        nxt = p.queue[0]
        remain = p.total_remaining()
        tail = f" · รวม {fmt_time(remain)}" if remain else ""
        e.add_field(
            name=f"ถัดไป ({len(p.queue)} เพลงในคิว{tail})",
            value=f"{nxt.title[:90]} `[{nxt.fmt_duration()}]`",
            inline=False,
        )
    if karaoke := live_lyrics_text(p):
        e.add_field(name="🎙 เนื้อเพลงสด", value=karaoke, inline=False)
    e.set_footer(text=status_line(p))
    return e


def status_line(p: "GuildPlayer") -> str:
    """Small status strip under the panel: every mode that is on, in one line."""
    parts = []
    if p.is_paused:
        parts.append("⏸ หยุดอยู่")
    if p.loop_mode == "track":
        parts.append("🔂 วนเพลงนี้")
    elif p.loop_mode == "queue":
        parts.append("🔁 วนทั้งคิว")
    vol = int(round(p.volume * 100))
    parts.append(f"{'🔇' if vol == 0 else '🔊'} {vol}%")
    if p.live_lyrics:
        parts.append("🎙 เนื้อสด")
    if p.normalize:
        parts.append("🎚 ความดังเท่ากัน")
    if p.stay_247:
        parts.append("🌙 24/7")
    if p.skip_votes:
        parts.append(f"🗳 โหวตข้าม {len(p.skip_votes)}")
    return " · ".join(parts)


LYRICS_LEAD = 1.0  # seconds: the panel edit reaches people a little late


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
    lines.append(f"**{lyr.synced[idx][1] or '♪'}**" if idx >= 0 else "-# ♪ …")
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


def build_idle_embed() -> discord.Embed:
    e = discord.Embed(color=SOURCE_COLORS["other"])
    e.title = "⏹ จบคิวแล้ว"
    e.description = "ใช้ `/play` เพื่อเล่นต่อ"
    return e


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
    link = f"[{_plain(t.title)}]({t.url})" if t.url.startswith("http") else _plain(t.title)
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
        e = RowsEmbed(title=title[:256], color=SOURCE_COLORS["other"])
        if p.current:
            c = p.current
            e.groove_head = (f"**กำลังเล่น:** [{_plain(c.title)}]({c.url}) "
                             f"`{fmt_time(p.position)}/{c.fmt_duration()}`")
        for i, t in items[page * per_page:(page + 1) * per_page]:
            e.groove_rows.append((queue_row(p, i, t), t.thumbnail))
        if p.current:
            e.add_field(
                name="กำลังเล่น",
                value=f"[{p.current.title[:80]}]({p.current.url}) "
                      f"`{fmt_time(p.position)}/{p.current.fmt_duration()}`",
                inline=False,
            )
        lines = []
        for i, t in items[page * per_page:(page + 1) * per_page]:
            lines.append(f"`{i}.` {t.title[:70]} `[{t.fmt_duration()}]` · {t.requester_name}")
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
    search inside the queue, remove all of my songs, remove duplicates."""

    PER_PAGE = QUEUE_PER_PAGE

    def __init__(self, p: "GuildPlayer", author_id: int):
        self.p = p
        self.query = ""
        self.selected: list[Track] = []
        self.picker = discord.ui.Select(placeholder="เลือกเพลง (เลือกได้หลายเพลง)", row=1,
                                        options=[discord.SelectOption(label="-")])
        self.picker.callback = self._picked
        super().__init__(build_queue_pages(p, self.PER_PAGE), author_id)
        self.add_item(self.picker)
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
                    label=f"{i}. {t.title}"[:100], value=str(i),
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
        self.remove_btn.label = f"ลบ {n}" if n > 1 else None
        self.search_btn.label = "ล้างการค้นหา" if self.query else "ค้นหา"
        self.search_btn.emoji = "✖️" if self.query else "🔍"
        self.mine_btn.disabled = self.dedupe_btn.disabled = not self.p.queue
        self.image_btn.disabled = not queue_items(self.p, self.query)

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
            return f"⏩ ไปที่ **{t.title}**"
        await self._apply(inter, "jump", run)

    @discord.ui.button(emoji="⬆️", label="ถัดไป", style=discord.ButtonStyle.secondary, row=0)
    async def top_btn(self, inter: discord.Interaction, _):
        def run(p):
            alive = self._alive(p)
            p.move_to_front(alive)
            if len(alive) == 1:
                return f"⬆️ ย้าย **{alive[0].title}** ขึ้นเป็นเพลงถัดไป"
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
            text = f"🗑 ลบ **{mine[0].title}**" if len(mine) == 1 else f"🗑 ลบ {len(mine)} เพลง"
            return text + (f" (ข้าม {skipped} เพลงของคนอื่น)" if skipped else "")
        await self._apply(inter, "remove", run)

    @discord.ui.button(emoji="🔍", label="ค้นหา", style=discord.ButtonStyle.secondary, row=2)
    async def search_btn(self, inter: discord.Interaction, _):
        if self.query:
            self.query, self.index, self.selected = "", 0, []
            self._refresh()
            return await inter.response.edit_message(embed=self.pages[0], view=self)
        await inter.response.send_modal(QueueSearchModal(self))

    @discord.ui.button(emoji="🧹", label="ลบเพลงของฉัน", style=discord.ButtonStyle.secondary,
                       row=2)
    async def mine_btn(self, inter: discord.Interaction, _):
        def run(p):
            n = p.remove_tracks([t for t in p.queue if t.requester_id == inter.user.id])
            if not n:
                raise UserError("คุณไม่มีเพลงในคิว")
            return f"🧹 ลบเพลงของคุณ {n} เพลง (ใช้ /undo เพื่อย้อน)"
        await self._apply(inter, "remove mine", run)

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
        rows.append({"pos": pos, "title": t.title, "artist": t.artist,
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


class AddSongModal(discord.ui.Modal, title="เพิ่มเพลง"):
    query = discord.ui.TextInput(label="ชื่อเพลง หรือลิงก์",
                                 placeholder="เช่น ลมหายใจ bodyslam หรือลิงก์ YouTube / Spotify",
                                 max_length=300)
    next_up = discord.ui.TextInput(label="ให้เป็นเพลงถัดไป? (พิมพ์ y)", required=False,
                                   max_length=3, placeholder="เว้นว่าง = ต่อท้ายคิว")

    async def on_submit(self, inter: discord.Interaction):
        music = inter.client.get_cog("Music")
        front = str(self.next_up.value).strip().lower() in ("y", "yes", "ใช่", "1")
        try:
            music.check_cooldown(inter.user.id)
            await inter.response.defer(ephemeral=True, thinking=True)
            embed = await music.enqueue(inter.user, inter.channel, str(self.query.value),
                                        front=front)
        except UserError as exc:
            if inter.response.is_done():
                return await inter.followup.send(f"⚠️ {exc}", ephemeral=True)
            return await inter.response.send_message(f"⚠️ {exc}", ephemeral=True)
        await inter.followup.send(embed=embed, ephemeral=True)
        await audit_inter(inter, "play (panel)", str(self.query.value))


def build_lyrics_pages(lyr: Lyrics, max_chars: int = 1800) -> list[discord.Embed]:
    head = f"🎤 {lyr.title}" + (f" · {lyr.artist}" if lyr.artist else "")
    if lyr.instrumental and not lyr.plain:
        return [discord.Embed(title=head[:256], description="เพลงบรรเลง ไม่มีเนื้อร้อง",
                              color=0xEB459E)]
    chunks, cur = [], ""
    for line in lyr.plain.splitlines():
        if len(cur) + len(line) + 1 > max_chars and cur:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    chunks = chunks or ["(ไม่มีเนื้อเพลง)"]
    tag = " · synced" if lyr.synced else ""
    return [discord.Embed(title=head[:256], description=c, color=0xEB459E)
            .set_footer(text=f"lrclib.net{tag} · หน้า {i}/{len(chunks)}")
            for i, c in enumerate(chunks, 1)]


def build_lyrics_now(lyr: Lyrics, position: float) -> discord.Embed:
    """Lines around the one playing now (synced lyrics only)."""
    idx = lyr.line_at(position)
    lines = []
    for i in range(max(idx - 3, 0), min(idx + 7, len(lyr.synced))):
        text = lyr.synced[i][1] or "♪"
        lines.append(f"**▶ {text}**" if i == idx else (f"-# {text}" if i < idx else text))
    if idx < 0:
        lines.insert(0, "-# ♪ ยังไม่ถึงท่อนร้อง")
    e = discord.Embed(title=f"🎤 {lyr.title}"[:256], description="\n".join(lines)[:4000],
                      color=0xEB459E)
    e.set_footer(text=f"ตอนนี้ {fmt_time(position)} · กด 📍 อีกครั้งเพื่ออัปเดต")
    return e


class LyricsView(PagesView):
    """Lyrics pages, plus a 'now' button that jumps to the current line (synced only)."""

    def __init__(self, lyr: Lyrics, author_id: int, p: Optional["GuildPlayer"] = None,
                 url: str = ""):
        super().__init__(build_lyrics_pages(lyr), author_id, timeout=600,
                         deny_text="ใช้ `/lyrics` เปิดของตัวเอง")
        self.lyr, self.p, self.url = lyr, p, url
        if not (lyr.synced and p and url):
            self.remove_item(self.now_btn)

    @discord.ui.button(emoji="📍", label="ท่อนปัจจุบัน", style=discord.ButtonStyle.secondary)
    async def now_btn(self, inter: discord.Interaction, _):
        p = self.p
        if not p or not p.current or p.current.url != self.url:
            return await inter.response.send_message("เพลงเปลี่ยนแล้ว", ephemeral=True)
        await inter.response.edit_message(embed=build_lyrics_now(self.lyr, p.position), view=self)


def build_added_embed(p: "GuildPlayer", tracks: list[Track], index: int,
                      label: str = "") -> discord.Embed:
    """Reply for /play: cover, queue position and when it will play.
    index: 0-based queue position of the first added track."""
    first = tracks[0]
    starts_now = not p.current and index == 0
    eta = "ตอนนี้" if starts_now else relative_ts(p.eta(index))
    if len(tracks) == 1:
        e = discord.Embed(title=first.title[:250], color=track_color(first),
                          url=first.url if first.url.startswith("http") else None)
        e.set_author(name=label or ("▶️ กำลังจะเล่น" if starts_now else "➕ เพิ่มเข้าคิว"))
        if first.artist:
            e.description = f"**{first.artist}**"
        pos = "กำลังจะเล่น" if starts_now else ("ถัดไป" if index == 0 else f"#{index + 1}")
        e.add_field(name="ลำดับในคิว", value=pos)
        e.add_field(name="ความยาว", value=first.fmt_duration())
        e.add_field(name="จะได้เล่น", value=eta)
    else:
        e = discord.Embed(title=f"เพิ่ม {len(tracks)} เพลงเข้าคิว", color=track_color(first))
        e.set_author(name=label or "➕ เพิ่มเข้าคิว")
        lines = [f"`{index + i}.` {t.title[:60]} `[{t.fmt_duration()}]`"
                 for i, t in enumerate(tracks[:5], 1)]
        if len(tracks) > 5:
            lines.append(f"-# และอีก {len(tracks) - 5} เพลง")
        e.description = "\n".join(lines)
        e.add_field(name="ลำดับในคิว", value=f"#{index + 1} – #{index + len(tracks)}")
        if all(t.duration for t in tracks):
            e.add_field(name="ความยาวรวม", value=fmt_time(sum(t.duration for t in tracks)))
        e.add_field(name="เริ่มเล่น", value=eta)
    if first.thumbnail:
        e.set_thumbnail(url=first.thumbnail)
    e.set_footer(text=f"ขอโดย {first.requester_name or '-'}")
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
                discord.SelectOption(label=t.title[:100], description=t.fmt_duration(),
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

async def _act(inter: discord.Interaction, action: Callable[["GuildPlayer"], Optional[str]],
               label: str):
    try:
        p = control(inter)
    except UserError as exc:
        return await inter.response.send_message(str(exc), ephemeral=True)
    msg = action(p)
    if msg:
        await inter.response.send_message(msg, ephemeral=True)
    else:
        await inter.response.defer()
    await audit_inter(inter, label)
    await p.update_panel()


async def act_prev(inter):
    await _act(inter, lambda p: None if p.previous() else "ไม่มีเพลงก่อนหน้า", "previous")


async def act_pause(inter):
    await _act(inter, lambda p: (p.toggle_pause(), None)[1], "pause")


async def act_skip(inter):
    await _act(inter, lambda p: p.vote_skip(inter.user), "skip")


async def act_seek(inter, delta: int):
    def run(p: "GuildPlayer") -> Optional[str]:
        if not p.current or not p.current.duration:
            return "เพลงนี้กรอไม่ได้"
        target = max(p.position + delta, 0)
        if target >= p.current.duration - 1:
            return "เกินความยาวเพลง ใช้ ⏭ แทน"
        p.restart_at(target)
        return None
    await _act(inter, run, f"seek {delta:+d}s")


async def act_lyrics(inter):
    from core import lyrics
    p = inter.client.players.get(inter.guild_id)
    if not p or not p.current:
        return await inter.response.send_message("ไม่มีเพลงเล่นอยู่", ephemeral=True)
    track = p.current
    await inter.response.defer(ephemeral=True, thinking=True)
    lyr = await lyrics.find(track)
    if not lyr:
        return await inter.followup.send("หาเนื้อเพลงไม่เจอ ลอง `/lyrics ชื่อเพลง`", ephemeral=True)
    view = LyricsView(lyr, inter.user.id, p, track.url)
    await inter.followup.send(embed=view.pages[0], view=view, ephemeral=True)


async def act_add(inter):
    await inter.response.send_modal(AddSongModal())


async def act_live_lyrics(inter):
    def run(p: "GuildPlayer") -> str:
        on = p.toggle_live_lyrics()
        return "🎙 เปิดเนื้อเพลงสดบน panel" if on else "ปิดเนื้อเพลงสดแล้ว"
    await _act(inter, run, "live lyrics")


async def act_stop(inter):
    try:
        p = control(inter)
    except UserError as exc:
        return await inter.response.send_message(str(exc), ephemeral=True)
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
SEEK_STEP = 10  # seconds for the ⏪ ⏩ panel buttons


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
        self.vol_down.disabled = p.volume <= 0
        self.vol_up.disabled = p.volume >= 1.5
        seekable = bool(p.current and p.current.duration)
        self.rewind.disabled = self.forward.disabled = not seekable
        self.lyrics_btn.disabled = self.live_lyrics_btn.disabled = not p.current
        if p.live_lyrics:
            self.live_lyrics_btn.style = discord.ButtonStyle.primary
        if not p.current:
            for item in (self.pause, self.skip, self.vol_down, self.vol_up, self.volume_select):
                item.disabled = True
        current = int(round(p.volume * 100))
        for opt in self.volume_select.options:
            opt.default = int(opt.value) == current

    # row 0: transport, row 1: queue and extras, row 2: add / volume / live lyrics,
    # row 3: volume presets
    @discord.ui.button(emoji="⏮", style=discord.ButtonStyle.secondary, custom_id="mb:prev", row=0)
    async def prev(self, inter, _):
        await act_prev(inter)

    @discord.ui.button(emoji="⏪", style=discord.ButtonStyle.secondary, custom_id="mb:rewind", row=0)
    async def rewind(self, inter, _):
        await act_seek(inter, -SEEK_STEP)

    @discord.ui.button(emoji="⏯", style=discord.ButtonStyle.secondary, custom_id="mb:pause", row=0)
    async def pause(self, inter, _):
        await act_pause(inter)

    @discord.ui.button(emoji="⏩", style=discord.ButtonStyle.secondary, custom_id="mb:forward", row=0)
    async def forward(self, inter, _):
        await act_seek(inter, SEEK_STEP)

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary, custom_id="mb:skip", row=0)
    async def skip(self, inter, _):
        await act_skip(inter)

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.secondary, custom_id="mb:stop", row=1)
    async def stop_btn(self, inter, _):
        await act_stop(inter)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="mb:loop", row=1)
    async def loop(self, inter, _):
        await _act(inter, lambda p: f"วนซ้ำ: {LOOP_ICON[p.cycle_loop()]}", "loop")

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="mb:shuffle", row=1)
    async def shuffle(self, inter, _):
        await _act(inter, lambda p: (p.shuffle(), "🔀 สลับคิวแล้ว (ใช้ /undo เพื่อย้อน)")[1],
                   "shuffle")

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary, custom_id="mb:queue", row=1)
    async def queue_btn(self, inter, _):
        await act_queue(inter)

    @discord.ui.button(emoji="🎤", style=discord.ButtonStyle.secondary, custom_id="mb:lyrics", row=1)
    async def lyrics_btn(self, inter, _):
        await act_lyrics(inter)

    @discord.ui.button(emoji="➕", label="เพิ่มเพลง", style=discord.ButtonStyle.secondary,
                       custom_id="mb:add", row=2)
    async def add_btn(self, inter, _):
        await act_add(inter)

    @discord.ui.button(emoji="🔉", label="-10", style=discord.ButtonStyle.secondary,
                       custom_id="mb:voldown", row=2)
    async def vol_down(self, inter, _):
        await _act(inter, lambda p: (p.set_volume(int(p.volume * 100) - 10), None)[1], "volume -10")

    @discord.ui.button(emoji="🔊", label="+10", style=discord.ButtonStyle.secondary,
                       custom_id="mb:volup", row=2)
    async def vol_up(self, inter, _):
        await _act(inter, lambda p: (p.set_volume(int(p.volume * 100) + 10), None)[1], "volume +10")

    @discord.ui.button(emoji="🎙", label="เนื้อสด", style=discord.ButtonStyle.secondary,
                       custom_id="mb:livelyrics", row=2)
    async def live_lyrics_btn(self, inter, _):
        await act_live_lyrics(inter)

    @discord.ui.select(placeholder="🔊 ระดับเสียง", custom_id="mb:volpreset", row=3,
                       options=[discord.SelectOption(label=f"{v}%", value=str(v))
                                for v in VOLUME_PRESETS])
    async def volume_select(self, inter, select: discord.ui.Select):
        value = int(select.values[0])
        await _act(inter, lambda p: (p.set_volume(value), None)[1], f"volume {value}")


class CompactPanelView(discord.ui.View):
    """One row of controls for mobile (settings compact)."""

    def __init__(self, p: Optional["GuildPlayer"] = None):
        super().__init__(timeout=None)
        if p is None:
            return
        self.prev.disabled = not p.history
        if p.is_paused:
            self.pause.emoji = "▶️"
            self.pause.style = discord.ButtonStyle.primary
        if not p.current:
            self.pause.disabled = self.skip.disabled = True

    @discord.ui.button(emoji="⏮", style=discord.ButtonStyle.secondary, custom_id="mbc:prev")
    async def prev(self, inter, _):
        await act_prev(inter)

    @discord.ui.button(emoji="⏯", style=discord.ButtonStyle.secondary, custom_id="mbc:pause")
    async def pause(self, inter, _):
        await act_pause(inter)

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary, custom_id="mbc:skip")
    async def skip(self, inter, _):
        await act_skip(inter)

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.secondary, custom_id="mbc:stop")
    async def stop_btn(self, inter, _):
        await act_stop(inter)

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary, custom_id="mbc:queue")
    async def queue_btn(self, inter, _):
        await act_queue(inter)


def relative_ts(seconds_from_now: Optional[int]) -> str:
    if seconds_from_now is None:
        return "ไม่ทราบ"
    if seconds_from_now <= 0:
        return "ตอนนี้"
    return f"<t:{int(time.time() + seconds_from_now)}:R>"
