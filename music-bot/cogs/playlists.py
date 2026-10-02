"""Personal saved playlists."""

import random

import config
from core.sources import search_tracks

import discord
from discord import app_commands
from discord.ext import commands

from core.checks import UserError
from core.sources import Track, fmt_time
from core.ui import PagesView


def _pages(title: str, lines: list[str], per_page: int = 15) -> list[discord.Embed]:
    pages = []
    for i in range(0, max(len(lines), 1), per_page):
        e = discord.Embed(title=title, description="\n".join(lines[i:i + per_page]) or "ว่าง",
                          color=0xE91E63)
        pages.append(e)
    for n, e in enumerate(pages, 1):
        e.set_footer(text=f"หน้า {n}/{len(pages)}")
    return pages


class Playlists(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def music(self):
        return self.bot.get_cog("Music")

    playlist = app_commands.Group(name="playlist", description="เพลย์ลิสต์ส่วนตัว",
                                  guild_only=True)

    async def _name_ac(self, inter: discord.Interaction, current: str):
        names = await self.bot.db.list_playlists(inter.user.id)
        return [app_commands.Choice(name=f"{n} ({c})", value=n)
                for n, c in names if current.lower() in n.lower()][:25]

    async def _check_limit(self, user_id: int, name: str):
        if await self.bot.db.get_playlist(user_id, name) is not None:
            return  # overwrite is fine
        if await self.bot.db.count_playlists(user_id) >= config.PLAYLIST_LIMIT:
            raise UserError(f"มีเพลย์ลิสต์ครบ {config.PLAYLIST_LIMIT} แล้ว ลบอันเก่าก่อน")

    @playlist.command(description="บันทึกเพลงปัจจุบัน + คิว เป็นเพลย์ลิสต์")
    async def save(self, inter: discord.Interaction, name: app_commands.Range[str, 1, 50]):
        p = self.bot.players.get(inter.guild_id)
        if not p or (not p.current and not p.queue):
            raise UserError("คิวว่าง ไม่มีอะไรให้บันทึก")
        data = p.snapshot()
        await self._check_limit(inter.user.id, name)
        await self.bot.db.save_playlist(inter.user.id, name, data)
        await inter.response.send_message(f"💾 บันทึก **{name}** ({len(data)} เพลง)",
                                          ephemeral=True)

    @playlist.command(description="เล่นเพลย์ลิสต์")
    @app_commands.autocomplete(name=_name_ac)
    async def load(self, inter: discord.Interaction, name: str, shuffle: bool = False):
        data = await self.bot.db.get_playlist(inter.user.id, name)
        if not data:
            raise UserError("ไม่มีเพลย์ลิสต์นี้")
        await inter.response.defer(thinking=True)
        tracks = [Track.from_dict(d) for d in data]
        if shuffle:
            random.shuffle(tracks)
        msg = await self.music.enqueue(inter.user, inter.channel, name, tracks=tracks)
        await inter.followup.send(f"📂 **{name}**: {msg}")

    @playlist.command(description="รายการเพลย์ลิสต์ของฉัน")
    async def list(self, inter: discord.Interaction):
        names = await self.bot.db.list_playlists(inter.user.id)
        if not names:
            raise UserError("ยังไม่มีเพลย์ลิสต์ ใช้ `/playlist save`")
        lines = [f"• **{n}** · {c} เพลง" for n, c in names]
        await inter.response.send_message(embed=_pages("📂 เพลย์ลิสต์ของฉัน", lines)[0],
                                          ephemeral=True)

    @playlist.command(description="ดูเพลงในเพลย์ลิสต์")
    @app_commands.autocomplete(name=_name_ac)
    async def show(self, inter: discord.Interaction, name: str):
        data = await self.bot.db.get_playlist(inter.user.id, name)
        if not data:
            raise UserError("ไม่มีเพลย์ลิสต์นี้")
        lines = [f"`{i}.` {d['title'][:70]} `[{fmt_time(d.get('duration'))}]`"
                 for i, d in enumerate(data, 1)]
        pages = _pages(f"📂 {name}", lines)
        await inter.response.send_message(embed=pages[0],
                                          view=PagesView(pages, inter.user.id), ephemeral=True)

    @playlist.command(description="ลบเพลย์ลิสต์")
    @app_commands.autocomplete(name=_name_ac)
    async def delete(self, inter: discord.Interaction, name: str):
        ok = await self.bot.db.delete_playlist(inter.user.id, name)
        await inter.response.send_message(f"🗑 ลบ **{name}**" if ok else "ไม่มีเพลย์ลิสต์นี้",
                                          ephemeral=True)

    @playlist.command(description="เปลี่ยนชื่อเพลย์ลิสต์")
    @app_commands.autocomplete(name=_name_ac)
    async def rename(self, inter: discord.Interaction, name: str,
                     new_name: app_commands.Range[str, 1, 50]):
        if await self.bot.db.get_playlist(inter.user.id, new_name) is not None:
            raise UserError("มีชื่อนี้แล้ว")
        ok = await self.bot.db.rename_playlist(inter.user.id, name, new_name)
        await inter.response.send_message(
            f"✏️ **{name}** → **{new_name}**" if ok else "ไม่มีเพลย์ลิสต์นี้", ephemeral=True)

    @playlist.command(name="import", description="นำเข้าเพลย์ลิสต์จากลิงก์ YouTube / Spotify")
    async def import_(self, inter: discord.Interaction, url: str,
                      name: app_commands.Range[str, 1, 50]):
        if not url.startswith("http"):
            raise UserError("ต้องเป็นลิงก์")
        await self._check_limit(inter.user.id, name)
        await inter.response.defer(ephemeral=True, thinking=True)
        try:
            tracks = await search_tracks(url, inter.user.id, inter.user.display_name)
        except Exception:
            raise UserError("อ่านลิงก์ไม่ได้")
        if not tracks:
            raise UserError("ไม่พบเพลงในลิงก์")
        tracks = tracks[:config.MAX_QUEUE]
        await self.bot.db.save_playlist(inter.user.id, name, [t.to_dict() for t in tracks])
        await inter.followup.send(f"📥 นำเข้า **{name}** ({len(tracks)} เพลง)", ephemeral=True)

    @playlist.command(description="เพิ่มเพลงเข้าเพลย์ลิสต์ (เว้น query = เพลงที่เล่นอยู่)")
    @app_commands.autocomplete(name=_name_ac)
    async def add(self, inter: discord.Interaction, name: str, query: str | None = None):
        data = await self.bot.db.get_playlist(inter.user.id, name)
        if data is None:
            await self._check_limit(inter.user.id, name)
            data = []
        await inter.response.defer(ephemeral=True, thinking=True)
        if query:
            try:
                tracks = await search_tracks(query, inter.user.id, inter.user.display_name)
            except Exception:
                raise UserError("หาเพลงไม่เจอ")
        else:
            p = self.bot.players.get(inter.guild_id)
            if not p or not p.current:
                raise UserError("ไม่มีเพลงเล่นอยู่ ใส่ query แทน")
            tracks = [p.current]
        if not tracks:
            raise UserError("หาเพลงไม่เจอ")
        urls = {d["url"] for d in data}
        new = [t.to_dict() for t in tracks if t.url not in urls]
        data.extend(new)
        await self.bot.db.save_playlist(inter.user.id, name, data[:config.MAX_QUEUE])
        what = f"**{tracks[0].title}**" if len(new) == 1 else f"{len(new)} เพลง"
        await inter.followup.send(f"➕ เพิ่ม {what} เข้า **{name}**", ephemeral=True)

    @playlist.command(description="ลบเพลงออกจากเพลย์ลิสต์ตามลำดับ")
    @app_commands.autocomplete(name=_name_ac)
    async def removetrack(self, inter: discord.Interaction, name: str,
                          index: app_commands.Range[int, 1]):
        data = await self.bot.db.get_playlist(inter.user.id, name)
        if not data or index > len(data):
            raise UserError("ไม่มีลำดับนี้")
        t = data.pop(index - 1)
        await self.bot.db.save_playlist(inter.user.id, name, data)
        await inter.response.send_message(f"🗑 ลบ **{t['title']}** จาก **{name}**", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Playlists(bot))
