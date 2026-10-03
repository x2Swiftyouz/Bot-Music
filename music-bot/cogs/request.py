"""Request channel: every message in it becomes a song request, and its header
message is the now-playing panel. Set with /settings request."""

import logging

import discord
from discord.ext import commands

import config
from core.checks import UserError

log = logging.getLogger("musicbot.request")


class RequestChannel(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.channels: dict[int, int] = {}  # guild_id -> channel_id

    async def cog_load(self):
        self.channels = await self.bot.db.request_channels()

    def is_prefixed(self, msg: discord.Message) -> bool:
        text = msg.content.strip()
        me = self.bot.user
        return text.startswith(config.PREFIX) or bool(
            me and text.startswith((f"<@{me.id}>", f"<@!{me.id}>")))

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if msg.author.bot or not msg.guild or self.channels.get(msg.guild.id) != msg.channel.id:
            return
        if self.is_prefixed(msg):
            return  # prefix commands still work here
        query = msg.content.strip()
        if not query and msg.attachments:
            query = msg.attachments[0].url  # audio file upload
        # Not at once: a message deleted within a moment of being sent can stay on the
        # sender's screen (the Discord app sees the delete before its own send finished).
        # delete(delay=…) runs in the background and ignores a missing Manage Messages.
        await msg.delete(delay=config.REQUEST_DELETE_DELAY)
        if not query:
            return
        music = self.bot.get_cog("Music")
        try:
            music.check_cooldown(msg.author.id)
            async with msg.channel.typing():
                embed = await music.enqueue(msg.author, msg.channel, query[:300])
            await msg.channel.send(embed=embed, delete_after=15)
        except UserError as exc:
            await msg.channel.send(f"⚠️ {msg.author.mention} {exc}", delete_after=10,
                                   allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            pass
        except Exception:
            log.exception("request channel failed")
            await msg.channel.send("❌ เกิดข้อผิดพลาด ลองใหม่", delete_after=10)

    async def setup_channel(self, guild: discord.Guild, channel: discord.TextChannel) -> list[str]:
        """Post the header and remember the channel. Returns warnings for the admin."""
        from core.ui import RequestIdleView, build_request_idle_embed
        warnings = []
        perms = channel.permissions_for(guild.me)
        if not (perms.send_messages and perms.embed_links):
            raise UserError(f"บอทส่งข้อความหรือ embed ใน {channel.mention} ไม่ได้")
        if not config.MESSAGE_CONTENT:
            raise UserError("ต้องเปิด Message Content Intent (MESSAGE_CONTENT=true) "
                            "บอทถึงจะอ่านชื่อเพลงในห้องได้")
        if not perms.manage_messages:
            warnings.append("บอทไม่มีสิทธิ์ Manage Messages ข้อความขอเพลงจะไม่ถูกลบ")
        await self.remove_channel(guild.id)
        header = await channel.send(embed=build_request_idle_embed(), view=RequestIdleView())
        db = self.bot.db
        await db.set_setting(guild.id, "request_channel", channel.id)
        await db.set_setting(guild.id, "request_message", header.id)
        self.channels[guild.id] = channel.id
        if p := self.bot.players.get(guild.id):
            p.request_channel_id, p.request_message_id = channel.id, header.id
        return warnings

    async def remove_channel(self, guild_id: int):
        s = await self.bot.db.get_settings(guild_id)
        if s["request_channel"] and s["request_message"]:
            channel = self.bot.get_channel(s["request_channel"])
            if channel:
                try:
                    await channel.get_partial_message(s["request_message"]).delete()
                except discord.HTTPException:
                    pass
        await self.bot.db.set_setting(guild_id, "request_channel", 0)
        await self.bot.db.set_setting(guild_id, "request_message", 0)
        self.channels.pop(guild_id, None)
        if p := self.bot.players.get(guild_id):
            if p._is_request_panel():
                p.panel_message = None
            p.request_channel_id = p.request_message_id = 0


async def setup(bot):
    await bot.add_cog(RequestChannel(bot))
