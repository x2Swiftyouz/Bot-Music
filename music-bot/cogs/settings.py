"""Server settings: 24/7, announcements, vote skip, compact panel, time format."""

import discord
from discord import app_commands
from discord.ext import commands


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
        await inter.response.send_message(embed=e, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Settings(bot))
