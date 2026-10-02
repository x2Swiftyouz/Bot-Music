"""/help (category menu), /ping, /about."""

import platform
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
from core.helpdata import CATEGORIES


def help_embed(bot, key: str | None = None) -> discord.Embed:
    p = config.PREFIX
    if key is None:
        e = discord.Embed(
            title="🎶 คำสั่งทั้งหมด",
            description=(f"ใช้ได้ทั้ง `/คำสั่ง` และ `{p}คำสั่ง` (เช่น `{p}p ชื่อเพลง`)\n"
                         "เลือกหมวดจากเมนูด้านล่าง"),
            color=0x5865F2)
        for k, (title, cmds) in CATEGORIES.items():
            e.add_field(name=title, value=" ".join(f"`{c[0].split()[0]}`" for c in cmds),
                        inline=False)
    else:
        title, cmds = CATEGORIES[key]
        e = discord.Embed(title=title, color=0x5865F2)
        e.description = "\n".join(f"**`{c}`**\n-# {d}" for c, d in cmds)
    if bot.user:
        e.set_thumbnail(url=bot.user.display_avatar.url)
    return e


class HelpView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=180)
        self.bot = bot
        select = discord.ui.Select(placeholder="เลือกหมวดคำสั่ง", options=[
            discord.SelectOption(label="หน้าแรก", value="home", emoji="🏠"),
            *[discord.SelectOption(label=t.split(" ", 1)[1], value=k, emoji=t.split(" ", 1)[0])
              for k, (t, _) in CATEGORIES.items()],
        ])
        select.callback = self._pick
        self.add_item(select)

    async def _pick(self, inter: discord.Interaction):
        key = inter.data["values"][0]
        await inter.response.edit_message(
            embed=help_embed(self.bot, None if key == "home" else key), view=self)


def ping_embed(bot) -> discord.Embed:
    ws = round(bot.latency * 1000)
    e = discord.Embed(title="🏓 Pong", color=0x2ECC71 if ws < 200 else 0xE67E22)
    e.add_field(name="Gateway", value=f"{ws} ms")
    voice = [round(vc.latency * 1000) for vc in bot.voice_clients
             if getattr(vc, "latency", float("inf")) != float("inf")]
    if voice:
        e.add_field(name="Voice", value=f"{sum(voice) // len(voice)} ms")
    return e


def about_embed(bot) -> discord.Embed:
    up = int(time.time() - bot.started_at)
    h, rem = divmod(up, 3600)
    e = discord.Embed(title=f"🎶 {bot.user.name}", color=0x5865F2,
                      description="บอทเพลง Python · discord.py · yt-dlp · FFmpeg")
    e.add_field(name="เซิร์ฟเวอร์", value=str(len(bot.guilds)))
    e.add_field(name="กำลังเล่น", value=str(sum(1 for p in bot.players.values() if p.current)))
    e.add_field(name="Uptime", value=f"{h}h {rem // 60}m")
    e.add_field(name="Python", value=platform.python_version())
    e.add_field(name="discord.py", value=discord.__version__)
    e.set_thumbnail(url=bot.user.display_avatar.url)
    return e


class Info(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="คำสั่งทั้งหมด")
    async def help_cmd(self, inter: discord.Interaction):
        await inter.response.send_message(embed=help_embed(self.bot), view=HelpView(self.bot))

    @app_commands.command(description="ความเร็วบอท")
    async def ping(self, inter: discord.Interaction):
        await inter.response.send_message(embed=ping_embed(self.bot))

    @app_commands.command(description="ข้อมูลบอท")
    async def about(self, inter: discord.Interaction):
        await inter.response.send_message(embed=about_embed(self.bot))


async def setup(bot):
    await bot.add_cog(Info(bot))
