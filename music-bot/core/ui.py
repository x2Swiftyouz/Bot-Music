"""Embeds and interactive views (buttons, pagination, select menus)."""

import time
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

import discord

from core.checks import UserError, control
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


def build_now_playing(p: "GuildPlayer") -> discord.Embed:
    t = p.current
    if not t:
        return build_idle_embed()
    state = "⏸ หยุดชั่วคราว" if p.is_paused else "▶️ กำลังเล่น"
    e = discord.Embed(
        title=t.title[:250],
        url=t.url if t.url.startswith("http") else None,
        color=track_color(t),
    )
    if p.compact:
        e.description = f"{_time_line(p)}\n-# {t.requester_name or '-'} · {int(p.volume * 100)}%"
        if p.queue:
            e.description += f" · ถัดไป: {p.queue[0].title[:50]}"
        if t.thumbnail:
            e.set_thumbnail(url=t.thumbnail)
        e.set_author(name=state)
        return e

    e.set_author(name=state)
    if getattr(p, "has_card", False):
        e.set_image(url="attachment://nowplaying.jpg")
    elif t.thumbnail:
        e.set_thumbnail(url=t.thumbnail)
    e.description = _time_line(p)
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

    def __init__(self, pages: list[discord.Embed], author_id: int, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.index = 0
        self.author_id = author_id
        self.message: Optional[discord.Message] = None
        self._sync()

    def _sync(self):
        self.prev_btn.disabled = self.index == 0
        self.next_btn.disabled = self.index >= len(self.pages) - 1

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.author_id:
            await inter.response.send_message("ใช้ `/queue` เปิดของตัวเอง", ephemeral=True)
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
    pages = build_queue_pages(p)
    await inter.response.send_message(embed=pages[0], view=PagesView(pages, inter.user.id),
                                      ephemeral=True)


async def audit_inter(inter: discord.Interaction, action: str, detail: str = ""):
    try:
        await inter.client.db.audit(inter.guild_id, inter.user.id, action, detail)
    except Exception:
        pass


VOLUME_PRESETS = (10, 25, 50, 75, 100, 125)


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
        if not p.current:
            for item in (self.pause, self.skip, self.vol_down, self.vol_up, self.volume_select):
                item.disabled = True
        current = int(round(p.volume * 100))
        for opt in self.volume_select.options:
            opt.default = int(opt.value) == current

    @discord.ui.button(emoji="⏮", style=discord.ButtonStyle.secondary, custom_id="mb:prev", row=0)
    async def prev(self, inter, _):
        await act_prev(inter)

    @discord.ui.button(emoji="⏯", style=discord.ButtonStyle.primary, custom_id="mb:pause", row=0)
    async def pause(self, inter, _):
        await act_pause(inter)

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary, custom_id="mb:skip", row=0)
    async def skip(self, inter, _):
        await act_skip(inter)

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.danger, custom_id="mb:stop", row=0)
    async def stop_btn(self, inter, _):
        await act_stop(inter)

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary, custom_id="mb:queue", row=0)
    async def queue_btn(self, inter, _):
        await act_queue(inter)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="mb:loop", row=1)
    async def loop(self, inter, _):
        await _act(inter, lambda p: f"วนซ้ำ: {LOOP_ICON[p.cycle_loop()]}", "loop")

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="mb:shuffle", row=1)
    async def shuffle(self, inter, _):
        await _act(inter, lambda p: (p.shuffle(), "🔀 สลับคิวแล้ว (ใช้ /undo เพื่อย้อน)")[1],
                   "shuffle")

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, custom_id="mb:voldown", row=1)
    async def vol_down(self, inter, _):
        await _act(inter, lambda p: (p.set_volume(int(p.volume * 100) - 10), None)[1], "volume -10")

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, custom_id="mb:volup", row=1)
    async def vol_up(self, inter, _):
        await _act(inter, lambda p: (p.set_volume(int(p.volume * 100) + 10), None)[1], "volume +10")

    @discord.ui.select(placeholder="🔊 ระดับเสียง", custom_id="mb:volpreset", row=2,
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
