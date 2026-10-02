"""Prefix commands (default "!"), e.g. !p song, !s, !q. Needs MESSAGE_CONTENT."""

import logging
import time

import discord
from discord.ext import commands

import config
from cogs.info import HelpView, about_embed, help_embed, ping_embed
from core.checks import UserError, control_member
from core.player import LOOP_MODES
from core.sources import fmt_time, parse_time, search_choices
from core.ui import LOOP_ICON, QueueView, SearchView, build_now_playing

log = logging.getLogger("musicbot.prefix")

ALIASES = {
    "p": "play", "pn": "playnext", "s": "skip", "n": "skip", "next": "skip",
    "b": "previous", "back": "previous", "prev": "previous",
    "r": "resume", "dc": "stop", "leave": "stop", "q": "queue", "np": "nowplaying",
    "v": "volume", "vol": "volume", "l": "loop", "sh": "shuffle", "rm": "remove",
    "mv": "move", "j": "jump", "cl": "clear", "ff": "forward", "rw": "backward",
    "h": "help", "find": "search", "u": "undo", "share": "card", "ly": "lyrics",
}


def _int(text: str, name: str) -> int:
    try:
        return int(text)
    except (TypeError, ValueError):
        raise UserError(f"ใส่ {name} เป็นตัวเลข")


class Prefix(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._cooldown: dict[int, float] = {}

    @property
    def music(self):
        return self.bot.get_cog("Music")

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if msg.author.bot or not msg.guild or not msg.content:
            return
        content = msg.content.strip()
        mention = f"<@{self.bot.user.id}>"
        if content.startswith(config.PREFIX):
            body = content[len(config.PREFIX):]
        elif content.startswith(mention):
            body = content[len(mention):].strip()
        else:
            return
        if not body:
            return
        name, _, args = body.partition(" ")
        name = ALIASES.get(name.lower(), name.lower())
        handler = getattr(self, f"cmd_{name}", None)
        if not handler:
            return
        try:
            await handler(msg, args.strip())
            try:
                await self.bot.db.audit(msg.guild.id, msg.author.id, name, args.strip()[:100])
            except Exception:
                pass
        except UserError as exc:
            await msg.reply(f"⚠️ {exc}", mention_author=False, delete_after=15)
        except discord.HTTPException:
            pass
        except Exception:
            log.exception("prefix command %s failed", name)
            await msg.reply("❌ เกิดข้อผิดพลาด ลองใหม่", mention_author=False)

    async def _say(self, msg: discord.Message, text: str = None, **kw):
        return await msg.reply(text, mention_author=False, **kw)

    def _ctl(self, msg):
        return control_member(self.bot, msg.author)

    # ---------------------------------------------------------------- play
    async def cmd_play(self, msg, args, front=False):
        if not args:
            raise UserError(f"ใช้ `{config.PREFIX}p ชื่อเพลง หรือ ลิงก์`")
        now = time.time()
        if now - self._cooldown.get(msg.author.id, 0) < config.PLAY_COOLDOWN:
            raise UserError("ใจเย็น รอสักครู่")
        self._cooldown[msg.author.id] = now
        async with msg.channel.typing():
            embed = await self.music.enqueue(msg.author, msg.channel, args, front=front)
        await self._say(msg, embed=embed)

    async def cmd_playnext(self, msg, args):
        await self.cmd_play(msg, args, front=True)

    async def cmd_search(self, msg, args):
        if not args:
            raise UserError(f"ใช้ `{config.PREFIX}search คำค้น`")
        async with msg.channel.typing():
            results = await search_choices(args, 10)
        if not results:
            raise UserError("หาไม่เจอ")

        async def picked(i: discord.Interaction, track):
            await i.response.defer()
            try:
                embed = await self.music.enqueue(i.user, i.channel, track.url, tracks=[track])
                await i.edit_original_response(content=None, embed=embed, view=None)
            except UserError as exc:
                await i.edit_original_response(content=str(exc), embed=None, view=None)

        lines = [f"`{n}.` {t.title[:80]} `[{t.fmt_duration()}]`" for n, t in enumerate(results, 1)]
        e = discord.Embed(title=f"🔎 {args}", description="\n".join(lines), color=0x5865F2)
        await self._say(msg, embed=e, view=SearchView(results, msg.author.id, picked))

    async def cmd_skip(self, msg, args):
        p = self._ctl(msg)
        await self._say(msg, p.vote_skip(msg.author))

    async def cmd_previous(self, msg, args):
        if not self._ctl(msg).previous():
            raise UserError("ไม่มีเพลงก่อนหน้า")
        await msg.add_reaction("⏮")

    async def cmd_pause(self, msg, args):
        p = self._ctl(msg)
        await self._say(msg, "⏸ หยุดชั่วคราว" if p.toggle_pause() else "▶️ เล่นต่อ")
        await p.update_panel()

    async def cmd_resume(self, msg, args):
        p = self._ctl(msg)
        if not p.is_paused:
            raise UserError("ไม่ได้หยุดอยู่")
        p.toggle_pause()
        await msg.add_reaction("▶️")
        await p.update_panel()

    async def cmd_stop(self, msg, args):
        p = self._ctl(msg)
        await p.destroy()
        await msg.add_reaction("⏹")

    # --------------------------------------------------------------- queue
    async def cmd_queue(self, msg, args):
        p = self.bot.players.get(msg.guild.id)
        if not p or (not p.current and not p.queue):
            raise UserError("คิวว่าง")
        view = QueueView(p, msg.author.id)
        view.message = await self._say(msg, embed=view.pages[0], view=view)

    async def cmd_nowplaying(self, msg, args):
        p = self.bot.players.get(msg.guild.id)
        if not p or not p.current:
            raise UserError("ไม่มีเพลงเล่นอยู่")
        card = await p.card_file()
        kw = {"file": card} if card else {}
        await self._say(msg, embed=build_now_playing(p, card=card.filename if card else ""),
                        view=self.bot.panel_view, **kw)

    async def cmd_lyrics(self, msg, args):
        async with msg.channel.typing():
            out = await self.music.lyrics_message(msg.guild.id, msg.author.id, args or None)
        out["view"].message = await self._say(msg, **out)

    async def cmd_card(self, msg, args):
        cards = self.bot.get_cog("Cards")
        await self._say(msg, **await cards.share_message(msg.guild.id, msg.author))

    async def cmd_remove(self, msg, args):
        p = self._ctl(msg)
        i = _int(args, "ลำดับ")
        if not 1 <= i <= len(p.queue):
            raise UserError("ไม่มีลำดับนี้")
        t = p.queue[i - 1]
        if t.requester_id != msg.author.id and not p.is_admin(msg.author):
            raise UserError("ลบได้เฉพาะเพลงของตัวเอง")
        p.remove_at(i)
        await self._say(msg, f"🗑 ลบ **{t.title}**")

    async def cmd_move(self, msg, args):
        p = self._ctl(msg)
        parts = args.split()
        if len(parts) != 2:
            raise UserError(f"ใช้ `{config.PREFIX}move จาก ไป`")
        a, b = _int(parts[0], "ลำดับ"), _int(parts[1], "ลำดับ")
        if not 1 <= a <= len(p.queue) or b < 1:
            raise UserError("ไม่มีลำดับนี้")
        t = p.move_track(a, b)
        await self._say(msg, f"↕️ ย้าย **{t.title}** ไปลำดับ {b}")

    async def cmd_jump(self, msg, args):
        t = self._ctl(msg).jump(_int(args, "ลำดับ"))
        if not t:
            raise UserError("ไม่มีลำดับนี้")
        await self._say(msg, f"⏩ ไปที่ **{t.title}**")

    async def cmd_shuffle(self, msg, args):
        p = self._ctl(msg)
        if len(p.queue) < 2:
            raise UserError("เพลงในคิวน้อยเกินไป")
        p.shuffle()
        await msg.add_reaction("🔀")

    async def cmd_clear(self, msg, args):
        p = self._ctl(msg)
        n = p.clear_queue()
        await self._say(msg, f"🧹 ล้าง {n} เพลง (`{config.PREFIX}undo` เพื่อย้อน)")

    async def cmd_loop(self, msg, args):
        p = self._ctl(msg)
        mode = args.lower()
        if mode in LOOP_MODES:
            p.loop_mode = mode
        elif not mode:
            p.cycle_loop()
        else:
            raise UserError("ใช้ off / track / queue")
        await self.bot.db.set_setting(msg.guild.id, "loop_mode", p.loop_mode)
        await self._say(msg, f"วนซ้ำ: {LOOP_ICON[p.loop_mode]}")
        await p.update_panel()

    # --------------------------------------------------------------- audio
    async def cmd_volume(self, msg, args):
        p = self._ctl(msg)
        if not args:
            return await self._say(msg, f"🔊 เสียง {int(p.volume * 100)}%")
        v = _int(args.rstrip("%"), "เสียง")
        if not 0 <= v <= 150:
            raise UserError("0-150 เท่านั้น")
        p.set_volume(v)
        await self.bot.db.set_setting(msg.guild.id, "volume", v)
        await self._say(msg, f"🔊 เสียง {v}%")

    async def cmd_seek(self, msg, args):
        p = self._ctl(msg)
        sec = parse_time(args or "")
        if sec is None or not p.current:
            raise UserError("รูปแบบเวลา เช่น 1:30")
        if p.current.duration and sec >= p.current.duration:
            raise UserError("เกินความยาวเพลง")
        p.restart_at(sec)
        await self._say(msg, f"⏩ ไปที่ {fmt_time(sec)}")

    async def cmd_forward(self, msg, args):
        p = self._ctl(msg)
        sec = _int(args or "10", "วินาที")
        target = p.position + sec
        if p.current and p.current.duration and target >= p.current.duration - 1:
            raise UserError("เกินความยาวเพลง")
        p.restart_at(target)
        await self._say(msg, f"⏩ +{sec}s → {fmt_time(target)}")

    async def cmd_backward(self, msg, args):
        p = self._ctl(msg)
        sec = _int(args or "10", "วินาที")
        target = max(p.position - sec, 0)
        p.restart_at(target)
        await self._say(msg, f"⏪ -{sec}s → {fmt_time(target)}")

    async def cmd_replay(self, msg, args):
        self._ctl(msg).restart_at(0)
        await msg.add_reaction("🔄")

    async def cmd_undo(self, msg, args):
        p = self._ctl(msg)
        label = p.undo()
        if not label:
            raise UserError("ไม่มีอะไรให้ย้อน")
        await self._say(msg, f"↩️ ย้อน **{label}** แล้ว")

    # ---------------------------------------------------------------- misc
    async def cmd_help(self, msg, args):
        await self._say(msg, embed=help_embed(self.bot), view=HelpView(self.bot))

    async def cmd_ping(self, msg, args):
        await self._say(msg, embed=ping_embed(self.bot))

    async def cmd_about(self, msg, args):
        await self._say(msg, embed=about_embed(self.bot))


async def setup(bot):
    if not bot.intents.message_content:
        log.warning("Prefix commands disabled: MESSAGE_CONTENT is off")
        return
    await bot.add_cog(Prefix(bot))
