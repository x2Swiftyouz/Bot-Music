"""Embeds and interactive views (buttons, pagination, select menus)."""

import time
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

import discord

from core.card import FILENAME
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
    right = t.fmt_duration()
    if p.time_remaining and t.duration:
        right = "-" + fmt_time(t.duration - pos)
    return f"`{fmt_time(pos)}` {progress_bar(pos, t.duration)} `{right}`"


def build_now_playing(p: "GuildPlayer", card: Optional[bool] = None) -> discord.Embed:
    """card: whether a card image is attached (default: the panel's own state)."""
    t = p.current
    if not t:
        return build_idle_embed()
    has_card = p.has_card if card is None else card
    e = discord.Embed(
        title=t.title[:250],
        url=t.url if t.url.startswith("http") else None,
        color=track_color(t),
    )
    if p.loading:
        e.set_author(name="⏳ กำลังโหลด…")
        if has_card:
            e.set_image(url=f"attachment://{FILENAME}")
        elif t.thumbnail:
            e.set_thumbnail(url=t.thumbnail)
        e.description = f"-# ขอโดย {t.requester_name or '-'}"
        return e
    state = "⏸ หยุดชั่วคราว" if p.is_paused else "▶️ กำลังเล่น"
    artist = f"**{t.artist}**\n" if t.artist else ""
    if p.compact:
        e.description = (f"{artist}{_time_line(p)}\n"
                         f"-# {t.requester_name or '-'} · {int(p.volume * 100)}%")
        if p.queue:
            e.description += f" · ถัดไป: {p.queue[0].title[:50]}"
        if t.thumbnail:
            e.set_thumbnail(url=t.thumbnail)
        e.set_author(name=state)
        return e

    e.set_author(name=state)
    if has_card:  # the card already shows artist, progress and badges
        e.set_image(url=f"attachment://{FILENAME}")
        e.description = _time_line(p)
    else:
        if t.thumbnail:
            e.set_thumbnail(url=t.thumbnail)
        e.description = artist + _time_line(p)
    e.add_field(name="ขอโดย", value=t.requester_name or "-", inline=True)
    e.add_field(name="เสียง", value=f"{int(p.volume * 100)}%", inline=True)
    e.add_field(name="วนซ้ำ", value=LOOP_ICON[p.loop_mode], inline=True)
    if p.stay_247:
        e.add_field(name="โหมด", value="🌙 24/7", inline=False)
    if p.queue:
        nxt = p.queue[0]
        remain = p.total_remaining()
        tail = f" · รวม {fmt_time(remain)}" if remain else ""
        e.add_field(
            name=f"ถัดไป ({len(p.queue)} เพลงในคิว{tail})",
            value=f"{nxt.title[:90]} `[{nxt.fmt_duration()}]`",
            inline=False,
        )
    if p.skip_votes:
        e.set_footer(text=f"โหวตข้าม {len(p.skip_votes)}")
    return e


def build_idle_embed() -> discord.Embed:
    e = discord.Embed(color=SOURCE_COLORS["other"])
    e.title = "⏹ จบคิวแล้ว"
    e.description = "ใช้ `/play` เพื่อเล่นต่อ"
    return e


def build_queue_pages(p: "GuildPlayer", per_page: int = 10) -> list[discord.Embed]:
    items = list(p.queue)
    pages = []
    total_pages = max(1, -(-len(items) // per_page))
    remain = p.total_remaining()
    for page in range(total_pages):
        e = discord.Embed(title="📜 คิวเพลง", color=SOURCE_COLORS["other"])
        if p.current:
            e.add_field(
                name="กำลังเล่น",
                value=f"[{p.current.title[:80]}]({p.current.url}) "
                      f"`{fmt_time(p.position)}/{p.current.fmt_duration()}`",
                inline=False,
            )
        lines = []
        for i, t in enumerate(items[page * per_page:(page + 1) * per_page],
                              start=page * per_page + 1):
            lines.append(f"`{i}.` {t.title[:70]} `[{t.fmt_duration()}]` · {t.requester_name}")
        e.description = "\n".join(lines) or "คิวว่าง"
        e.set_footer(text=(
            f"หน้า {page + 1}/{total_pages} · {len(items)} เพลง"
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
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


class QueueView(PagesView):
    """Queue pages plus a song picker: play now, move to the top, or remove."""

    PER_PAGE = 10

    def __init__(self, p: "GuildPlayer", author_id: int):
        self.p = p
        self.selected: Optional[Track] = None
        self.picker = discord.ui.Select(placeholder="เลือกเพลงเพื่อจัดการ", row=1,
                                        options=[discord.SelectOption(label="-")])
        self.picker.callback = self._picked
        super().__init__(build_queue_pages(p, self.PER_PAGE), author_id)
        self.add_item(self.picker)

    def _sync(self):
        super()._sync()
        if not hasattr(self, "picker"):
            return
        start = self.index * self.PER_PAGE
        items = list(self.p.queue)[start:start + self.PER_PAGE]
        if self.selected is not None and not any(t is self.selected for t in items):
            self.selected = None
        if items:
            self.picker.options = [
                discord.SelectOption(
                    label=f"{i}. {t.title}"[:100], value=str(i),
                    description=f"{t.fmt_duration()} · {t.requester_name or '-'}"[:100],
                    default=t is self.selected)
                for i, t in enumerate(items, start=start + 1)]
            self.picker.disabled = False
        else:
            self.picker.options = [discord.SelectOption(label="คิวว่าง", value="0")]
            self.picker.disabled = True
        for btn in (self.jump_btn, self.top_btn, self.remove_btn):
            btn.disabled = self.selected is None

    def _locate(self) -> Optional[int]:
        """1-based position of the selected track now (the queue may have changed)."""
        for i, t in enumerate(self.p.queue, 1):
            if t is self.selected:
                return i
        return None

    def _refresh(self):
        self.pages = build_queue_pages(self.p, self.PER_PAGE)
        self.index = min(self.index, len(self.pages) - 1)
        self._sync()

    async def _picked(self, inter: discord.Interaction):
        pos = int(self.picker.values[0])
        q = list(self.p.queue)
        self.selected = q[pos - 1] if 0 < pos <= len(q) else None
        self._refresh()
        await inter.response.edit_message(embed=self.pages[self.index], view=self)

    async def _run(self, inter: discord.Interaction, action: str):
        try:
            p = control(inter)
            if p is not self.p:
                raise UserError("คิวนี้หมดอายุแล้ว เปิด `/queue` ใหม่")
            pos = self._locate()
            if pos is None:
                raise UserError("เพลงนี้ไม่อยู่ในคิวแล้ว")
            t = self.selected
            if action == "remove":
                if t.requester_id != inter.user.id and not p.is_admin(inter.user):
                    raise UserError("ลบได้เฉพาะเพลงของตัวเอง")
                p.remove_at(pos)
                text = f"🗑 ลบ **{t.title}**"
            elif action == "top":
                p.move_track(pos, 1)
                text = f"⬆️ ย้าย **{t.title}** ขึ้นเป็นเพลงถัดไป"
            else:
                p.jump(pos)
                text = f"⏩ ไปที่ **{t.title}**"
        except UserError as exc:
            return await inter.response.send_message(str(exc), ephemeral=True)
        self.selected = None
        self._refresh()
        await inter.response.edit_message(content=text, embed=self.pages[self.index], view=self)
        await audit_inter(inter, action, t.title)
        await p.update_panel()

    @discord.ui.button(emoji="▶️", label="เล่นเลย", style=discord.ButtonStyle.success, row=0)
    async def jump_btn(self, inter: discord.Interaction, _):
        await self._run(inter, "jump")

    @discord.ui.button(emoji="⬆️", label="ถัดไป", style=discord.ButtonStyle.primary, row=0)
    async def top_btn(self, inter: discord.Interaction, _):
        await self._run(inter, "top")

    @discord.ui.button(emoji="🗑", style=discord.ButtonStyle.danger, row=0)
    async def remove_btn(self, inter: discord.Interaction, _):
        await self._run(inter, "remove")


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

    @discord.ui.button(emoji="📍", label="ท่อนปัจจุบัน", style=discord.ButtonStyle.primary)
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
            self.pause.style = discord.ButtonStyle.success
        if p.loop_mode != "off":
            self.loop.style = discord.ButtonStyle.success
            self.loop.emoji = "🔂" if p.loop_mode == "track" else "🔁"
        self.vol_down.disabled = p.volume <= 0
        self.vol_up.disabled = p.volume >= 1.5
        seekable = bool(p.current and p.current.duration)
        self.rewind.disabled = self.forward.disabled = not seekable
        self.lyrics_btn.disabled = not p.current
        if not p.current:
            for item in (self.pause, self.skip, self.vol_down, self.vol_up, self.volume_select):
                item.disabled = True
        current = int(round(p.volume * 100))
        for opt in self.volume_select.options:
            opt.default = int(opt.value) == current

    # row 0: transport, row 1: queue and extras, row 2: volume, row 3: volume presets
    @discord.ui.button(emoji="⏮", style=discord.ButtonStyle.secondary, custom_id="mb:prev", row=0)
    async def prev(self, inter, _):
        await act_prev(inter)

    @discord.ui.button(emoji="⏪", style=discord.ButtonStyle.secondary, custom_id="mb:rewind", row=0)
    async def rewind(self, inter, _):
        await act_seek(inter, -SEEK_STEP)

    @discord.ui.button(emoji="⏯", style=discord.ButtonStyle.primary, custom_id="mb:pause", row=0)
    async def pause(self, inter, _):
        await act_pause(inter)

    @discord.ui.button(emoji="⏩", style=discord.ButtonStyle.secondary, custom_id="mb:forward", row=0)
    async def forward(self, inter, _):
        await act_seek(inter, SEEK_STEP)

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary, custom_id="mb:skip", row=0)
    async def skip(self, inter, _):
        await act_skip(inter)

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.danger, custom_id="mb:stop", row=1)
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

    @discord.ui.button(emoji="🔉", label="-10", style=discord.ButtonStyle.secondary,
                       custom_id="mb:voldown", row=2)
    async def vol_down(self, inter, _):
        await _act(inter, lambda p: (p.set_volume(int(p.volume * 100) - 10), None)[1], "volume -10")

    @discord.ui.button(emoji="🔊", label="+10", style=discord.ButtonStyle.secondary,
                       custom_id="mb:volup", row=2)
    async def vol_up(self, inter, _):
        await _act(inter, lambda p: (p.set_volume(int(p.volume * 100) + 10), None)[1], "volume +10")

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
            self.pause.style = discord.ButtonStyle.success
        if not p.current:
            self.pause.disabled = self.skip.disabled = True

    @discord.ui.button(emoji="⏮", style=discord.ButtonStyle.secondary, custom_id="mbc:prev")
    async def prev(self, inter, _):
        await act_prev(inter)

    @discord.ui.button(emoji="⏯", style=discord.ButtonStyle.primary, custom_id="mbc:pause")
    async def pause(self, inter, _):
        await act_pause(inter)

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary, custom_id="mbc:skip")
    async def skip(self, inter, _):
        await act_skip(inter)

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.danger, custom_id="mbc:stop")
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
