"""/help (category menu), /ping, /about."""

import logging
import platform
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
from core.helpdata import CATEGORIES

log = logging.getLogger("musicbot.info")


def start_embed(bot) -> discord.Embed:
    """Getting started: three steps, nothing more."""
    p = config.PREFIX
    e = discord.Embed(
        title="🚀 เริ่มต้นใช้งาน ใน 3 ขั้น",
        description="ขอบคุณที่เชิญบอทเพลงเข้ามา ใช้แค่นี้ก็ฟังเพลงได้แล้ว",
        color=0x57F287)
    e.add_field(name="1️⃣  เข้าห้องเสียง",
                value="เข้าห้องเสียงห้องไหนก็ได้ บอทจะตามเข้าไปเอง", inline=False)
    e.add_field(name="2️⃣  สั่งเล่นเพลง",
                value=f"พิมพ์ `/play ชื่อเพลง` หรือวางลิงก์ YouTube / Spotify\n"
                      f"-# แบบสั้น: `{p}p ชื่อเพลง`", inline=False)
    e.add_field(name="3️⃣  กดปุ่มบน panel",
                value="⏯ หยุด/เล่น · ⏭ ข้าม · ⏪ ⏩ กรอ 10 วิ · ➕ เพิ่มเพลง\n"
                      "📜 คิว · 🎤 เนื้อเพลง · 🎙 เนื้อเพลงสด", inline=False)
    e.set_footer(text="แอดมิน: /settings request ตั้งห้องขอเพลงที่พิมพ์ชื่อเพลงแล้วเล่นเลย")
    if bot.user:
        e.set_thumbnail(url=bot.user.display_avatar.url)
    return e


class WelcomeView(discord.ui.View):
    """Buttons under the getting-started message. Persistent (survives restarts)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(emoji="📖", label="คำสั่งทั้งหมด", style=discord.ButtonStyle.primary,
                       custom_id="mb:welcome:help")
    async def all_commands(self, inter: discord.Interaction, _):
        await inter.response.send_message(embed=help_embed(inter.client),
                                          view=HelpView(inter.client), ephemeral=True)


def _welcome_channel(guild: discord.Guild):
    """System channel if the bot can talk there, else the first channel it can."""
    me = guild.me
    candidates = [guild.system_channel] + sorted(guild.text_channels, key=lambda c: c.position)
    for ch in candidates:
        if ch:
            perms = ch.permissions_for(me)
            if perms.send_messages and perms.embed_links:
                return ch
    return None


def help_embed(bot, key: str | None = None) -> discord.Embed:
    p = config.PREFIX
    if key == "start":
        return start_embed(bot)
    if key is None:
        e = discord.Embed(
            title="🎶 คำสั่งทั้งหมด",
            description=(f"ใช้ได้ทั้ง `/คำสั่ง` และ `{p}คำสั่ง` (เช่น `{p}p ชื่อเพลง`)\n"
                         "เลือกหมวดจากเมนูด้านล่าง · ใช้ครั้งแรก? เลือก 🚀 เริ่มต้นใช้งาน"),
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
            discord.SelectOption(label="เริ่มต้นใช้งาน", value="start", emoji="🚀"),
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

    async def cog_load(self):
        self.bot.add_view(WelcomeView())

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        """Getting-started message, only the first time the bot joins a server."""
        try:
            if not await self.bot.db.mark_welcomed(guild.id):
                return
            channel = _welcome_channel(guild)
            if channel:
                await channel.send(embed=start_embed(self.bot), view=WelcomeView())
        except Exception:
            log.exception("welcome message failed in %s", guild.id)

    @app_commands.command(name="help", description="คำสั่งทั้งหมด")
    @app_commands.describe(start="แสดงวิธีเริ่มต้นใช้งาน 3 ขั้น")
    async def help_cmd(self, inter: discord.Interaction, start: bool = False):
        await inter.response.send_message(embed=help_embed(self.bot, "start" if start else None),
                                          view=HelpView(self.bot))

    @app_commands.command(description="ความเร็วบอท")
    async def ping(self, inter: discord.Interaction):
        await inter.response.send_message(embed=ping_embed(self.bot))

    @app_commands.command(description="ข้อมูลบอท")
    async def about(self, inter: discord.Interaction):
        await inter.response.send_message(embed=about_embed(self.bot))


async def setup(bot):
    await bot.add_cog(Info(bot))
