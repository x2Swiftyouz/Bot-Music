"""/card (share the playing track as an image) and /birthday (birthday badge on cards)."""

import datetime
import io

import discord
from discord import app_commands
from discord.ext import commands

from core.card import EXT, make_card
from core.checks import UserError


class Cards(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def share_message(self, guild_id: int, user: discord.abc.User) -> dict:
        """Message kwargs for a share card. Shared with the prefix command."""
        p = self.bot.players.get(guild_id)
        if not p or not p.current:
            raise UserError("ไม่มีเพลงเล่นอยู่")
        t = p.current
        data = await make_card(t, p.card_state(mode="share"))
        if not data:
            raise UserError("สร้างการ์ดไม่ได้ ลองใหม่")
        view = discord.ui.View()
        if t.url.startswith("http"):
            view.add_item(discord.ui.Button(label="เปิดเพลง", url=t.url, emoji="🎧"))
        return {
            "content": f"🎧 {user.mention} กำลังฟังเพลงนี้",
            "file": discord.File(io.BytesIO(data), filename=f"share.{EXT}"),
            "view": view,
            "allowed_mentions": discord.AllowedMentions.none(),
        }

    @app_commands.command(name="card", description="แชร์การ์ดเพลงที่กำลังเล่น")
    @app_commands.guild_only()
    async def card(self, inter: discord.Interaction):
        if not (p := self.bot.players.get(inter.guild_id)) or not p.current:
            raise UserError("ไม่มีเพลงเล่นอยู่")
        await inter.response.defer(thinking=True)
        await inter.followup.send(**await self.share_message(inter.guild_id, inter.user))

    birthday = app_commands.Group(name="birthday",
                                  description="วันเกิด: การ์ดจะขึ้นป้ายวันเกิดตอนเปิดเพลงของคุณ")

    @birthday.command(name="set", description="ตั้งวันเกิด")
    @app_commands.describe(day="วันที่", month="เดือน")
    async def birthday_set(self, inter: discord.Interaction,
                           day: app_commands.Range[int, 1, 31],
                           month: app_commands.Range[int, 1, 12]):
        try:
            datetime.date(2000, month, day)  # leap year, so 29 Feb is allowed
        except ValueError:
            raise UserError("ไม่มีวันที่นี้")
        await self.bot.db.set_birthday(inter.user.id, month, day)
        await inter.response.send_message(f"🎂 บันทึกวันเกิด {day}/{month} แล้ว", ephemeral=True)

    @birthday.command(name="remove", description="ลบวันเกิด")
    async def birthday_remove(self, inter: discord.Interaction):
        if not await self.bot.db.delete_birthday(inter.user.id):
            raise UserError("ยังไม่ได้ตั้งวันเกิด")
        await inter.response.send_message("ลบวันเกิดแล้ว", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Cards(bot))
