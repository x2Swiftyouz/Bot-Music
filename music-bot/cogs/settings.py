"""Server settings: 24/7, announcements, vote skip, compact panel, time format, card style."""

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

THEME_NAMES = {"blur": "เบลอจากปก", "solid": "สีพื้นจากปก", "minimal": "มินิมอล"}
LAYOUT_NAMES = {"wide": "แนวนอน (จอคอม)", "square": "จัตุรัส (มือถือ)"}


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
class Settings(commands.GroupCog, group_name="settings", group_description="ตั้งค่าบอทเพลง"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    def _player(self, guild_id: int):
        return self.bot.players.get(guild_id)

    @app_commands.command(name="247", description="อยู่ในห้องเสียงตลอด ไม่ออกเอง")
    async def mode_247(self, inter: discord.Interaction, enabled: bool):
        await self.bot.db.set_setting(inter.guild_id, "stay_247", int(enabled))
        if p := self._player(inter.guild_id):
            p.stay_247 = enabled
        await inter.response.send_message(f"🌙 24/7: {'เปิด' if enabled else 'ปิด'}",
                                          ephemeral=True)

    @app_commands.command(description="ประกาศเพลงใหม่ด้วย panel")
    async def announce(self, inter: discord.Interaction, enabled: bool):
        await self.bot.db.set_setting(inter.guild_id, "announce", int(enabled))
        if p := self._player(inter.guild_id):
            p.announce = enabled
        await inter.response.send_message(f"ประกาศเพลง: {'เปิด' if enabled else 'ปิด'}",
                                          ephemeral=True)

    @app_commands.command(description="ระบบโหวตข้ามเพลง")
    async def voteskip(self, inter: discord.Interaction, enabled: bool):
        await self.bot.db.set_setting(inter.guild_id, "vote_skip", int(enabled))
        if p := self._player(inter.guild_id):
            p.vote_skip_enabled = enabled
        await inter.response.send_message(f"โหวตข้าม: {'เปิด' if enabled else 'ปิด'}",
                                          ephemeral=True)

    @app_commands.command(description="Panel แบบย่อสำหรับมือถือ (ปุ่มแถวเดียว ไม่มีการ์ด)")
    async def compact(self, inter: discord.Interaction, enabled: bool):
        await self.bot.db.set_setting(inter.guild_id, "compact", int(enabled))
        if p := self._player(inter.guild_id):
            p.compact = enabled
            if p.current:
                await p.send_panel()
        await inter.response.send_message(f"📱 Compact: {'เปิด' if enabled else 'ปิด'}",
                                          ephemeral=True)

    @app_commands.command(description="แสดงเวลาที่เล่นไปแล้ว หรือ เวลาที่เหลือ")
    @app_commands.choices(mode=[app_commands.Choice(name="ความยาวเพลง (3:57)", value=0),
                                app_commands.Choice(name="เวลาที่เหลือ (-2:31)", value=1)])
    async def timeformat(self, inter: discord.Interaction, mode: app_commands.Choice[int]):
        await self.bot.db.set_setting(inter.guild_id, "time_remaining", mode.value)
        if p := self._player(inter.guild_id):
            p.time_remaining = bool(mode.value)
            await p.update_panel()
        await inter.response.send_message(f"⏱ เวลา: {mode.name}", ephemeral=True)

    @app_commands.command(description="รูปแบบการ์ด Now Playing")
    @app_commands.describe(theme="พื้นหลังการ์ด", layout="รูปทรงการ์ด")
    @app_commands.choices(
        theme=[app_commands.Choice(name=v, value=k) for k, v in THEME_NAMES.items()],
        layout=[app_commands.Choice(name=v, value=k) for k, v in LAYOUT_NAMES.items()])
    async def card(self, inter: discord.Interaction,
                   theme: Optional[app_commands.Choice[str]] = None,
                   layout: Optional[app_commands.Choice[str]] = None):
        p = self._player(inter.guild_id)
        if theme:
            await self.bot.db.set_setting(inter.guild_id, "card_theme", theme.value)
            if p:
                p.card_theme = theme.value
        if layout:
            await self.bot.db.set_setting(inter.guild_id, "card_layout", layout.value)
            if p:
                p.card_layout = layout.value
        s = await self.bot.db.get_settings(inter.guild_id)
        await inter.response.send_message(
            f"🖼 การ์ด: {THEME_NAMES.get(s['card_theme'], s['card_theme'])} · "
            f"{LAYOUT_NAMES.get(s['card_layout'], s['card_layout'])}", ephemeral=True)
        if p and (theme or layout):
            await p.update_panel()

    @app_commands.command(description="ปรับความดังทุกเพลงให้เท่ากัน (loudness normalization)")
    async def normalize(self, inter: discord.Interaction, enabled: bool):
        await self.bot.db.set_setting(inter.guild_id, "normalize", int(enabled))
        if p := self._player(inter.guild_id):
            p.set_normalize(enabled)
        await inter.response.send_message(
            f"🎚 ปรับความดังให้เท่ากัน: {'เปิด' if enabled else 'ปิด'}", ephemeral=True)

    @app_commands.command(description="ดูค่าทั้งหมด")
    async def show(self, inter: discord.Interaction):
        s = await self.bot.db.get_settings(inter.guild_id)
        on = lambda v: "เปิด" if v else "ปิด"
        e = discord.Embed(title="⚙️ ตั้งค่า", color=0x5865F2)
        e.add_field(name="24/7", value=on(s["stay_247"]))
        e.add_field(name="ประกาศเพลง", value=on(s["announce"]))
        e.add_field(name="โหวตข้าม", value=on(s["vote_skip"]))
        e.add_field(name="เสียงเริ่มต้น", value=f"{s['volume']}%")
        e.add_field(name="วนซ้ำ", value=s["loop_mode"])
        e.add_field(name="Compact", value=on(s["compact"]))
        e.add_field(name="เวลา", value="เวลาที่เหลือ" if s["time_remaining"] else "ความยาวเพลง")
        e.add_field(name="ความดังเท่ากัน", value=on(s["normalize"]))
        e.add_field(name="การ์ด", value=f"{THEME_NAMES.get(s['card_theme'], s['card_theme'])} · "
                                         f"{LAYOUT_NAMES.get(s['card_layout'], s['card_layout'])}")
        await inter.response.send_message(embed=e, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Settings(bot))
