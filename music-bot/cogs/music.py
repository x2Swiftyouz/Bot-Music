"""Core music commands, voice events, queue restore."""

import asyncio
import logging
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from core.checks import UserError, control, get_player
from core.player import GuildPlayer
from core.sources import (Track, fmt_time, parse_time, search_busy, search_choices,
                          search_tracks)
from core.ui import (LOOP_ICON, PagesView, SearchView, build_now_playing,
                     build_queue_pages, relative_ts)

log = logging.getLogger("musicbot.music")


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._ac_cache: dict[str, tuple[float, list[app_commands.Choice[str]]]] = {}
        self._ac_latest: dict[int, object] = {}

    # ------------------------------------------------------------ helpers
    async def connect(self, member: discord.Member) -> discord.VoiceClient:
        voice = member.voice
        if not voice or not voice.channel:
            raise UserError("เข้าห้องเสียงก่อน")
        vc = member.guild.voice_client
        if vc is None:
            try:
                vc = await voice.channel.connect(timeout=20, reconnect=True, self_deaf=True)
            except Exception as exc:
                log.error("Voice connect failed: %s", exc)
                raise UserError("เชื่อมห้องเสียงไม่ได้ ลองใหม่")
        elif vc.channel != voice.channel:
            player = self.bot.players.get(member.guild.id)
            if player and player.current and player.humans_in_channel():
                raise UserError(f"บอทเล่นอยู่ที่ {vc.channel.mention}")
            await vc.move_to(voice.channel)
        return vc

    async def enqueue(self, member: discord.Member, channel: discord.abc.Messageable,
                      query: str, front: bool = False,
                      tracks: Optional[list[Track]] = None) -> str:
        """Shared by /play, /search, prefix commands and playlists."""
        await self.connect(member)
        player = await self.bot.get_player(member.guild)
        player.text_channel = channel

        if tracks is None:
            try:
                tracks = await search_tracks(query, member.id, member.display_name)
            except Exception as exc:
                log.warning("Search failed for %r: %s", query, exc)
                if "Spotify not configured" in str(exc):
                    raise UserError("ยังไม่ได้ตั้งค่า Spotify ใน .env")
                raise UserError("หาเพลงไม่เจอ หรือ ลิงก์ใช้ไม่ได้")
        else:
            for t in tracks:
                t.requester_id, t.requester_name = member.id, member.display_name
        if not tracks:
            raise UserError("หาเพลงไม่เจอ")

        # Limits and duplicate guard.
        tracks = [t for t in tracks if not player.contains(t.url)]
        if not tracks:
            raise UserError("เพลงนี้อยู่ในคิวแล้ว")
        if config.MAX_DURATION and not player.is_admin(member):
            tracks = [t for t in tracks if not t.duration or t.duration <= config.MAX_DURATION]
            if not tracks:
                raise UserError(f"เพลงยาวเกิน {fmt_time(config.MAX_DURATION)}")
        if config.MAX_PER_USER and not player.is_admin(member):
            room = config.MAX_PER_USER - player.user_track_count(member.id)
            if room <= 0:
                raise UserError(f"คุณมีเพลงในคิวครบ {config.MAX_PER_USER} แล้ว")
            tracks = tracks[:room]

        index = 0 if front else len(player.queue)
        added = player.add(tracks, front=front)
        if added == 0:
            raise UserError(f"คิวเต็ม (สูงสุด {config.MAX_QUEUE})")
        if added == 1:
            t = tracks[0]
            starts = "กำลังจะเล่น" if not player.current and index == 0 else \
                f"เล่น {relative_ts(player.eta(index))}"
            return f"➕ **{t.title}** `[{t.fmt_duration()}]` · {starts}"
        return f"➕ เพิ่ม {added} เพลงเข้าคิว"

    # ----------------------------------------------------------- commands
    async def play_autocomplete(self, inter: discord.Interaction, current: str):
        current = current.strip()
        if not config.AUTOCOMPLETE or len(current) < 4 or current.startswith("http"):
            return []
        key = current.lower()
        cached = self._ac_cache.get(key)
        if cached and time.time() - cached[0] < 600:
            return cached[1]
        # Debounce: only search the latest text the user typed.
        token = object()
        self._ac_latest[inter.user.id] = token
        await asyncio.sleep(0.7)
        if self._ac_latest.get(inter.user.id) is not token or search_busy():
            return []
        try:
            results = await asyncio.wait_for(
                search_choices(current, 5, autocomplete=True), timeout=2.0)
        except Exception:
            return []  # slow search runs on in its own small pool, playback unaffected
        choices = [
            app_commands.Choice(name=f"{t.title[:85]} ({t.fmt_duration()})", value=t.url[:100])
            for t in results
        ]
        self._ac_cache[key] = (time.time(), choices)
        if len(self._ac_cache) > 500:
            self._ac_cache.clear()
        return choices

    @app_commands.command(description="เล่นเพลง: ลิงก์ / เพลย์ลิสต์ / Spotify / คำค้น")
    @app_commands.describe(query="ลิงก์ หรือ ชื่อเพลง", next="แทรกเป็นเพลงถัดไป")
    @app_commands.autocomplete(query=play_autocomplete)
    @app_commands.checks.cooldown(1, config.PLAY_COOLDOWN, key=lambda i: i.user.id)
    @app_commands.guild_only()
    async def play(self, inter: discord.Interaction, query: str, next: bool = False):
        await inter.response.defer(thinking=True)
        msg = await self.enqueue(inter.user, inter.channel, query, front=next)
        await inter.followup.send(msg)

    @app_commands.command(description="ค้นหาแล้วเลือกเพลงจากรายการ")
    @app_commands.guild_only()
    async def search(self, inter: discord.Interaction, query: str):
        await inter.response.defer(thinking=True)
        results = await search_choices(query, 10)
        if not results:
            raise UserError("หาไม่เจอ")

        async def picked(i: discord.Interaction, track: Track):
            await i.response.defer()
            try:
                msg = await self.enqueue(i.user, i.channel, track.url, tracks=[track])
            except UserError as exc:
                msg = str(exc)
            await i.edit_original_response(content=msg, embed=None, view=None)

        lines = [f"`{n}.` {t.title[:80]} `[{t.fmt_duration()}]`"
                 for n, t in enumerate(results, 1)]
        embed = discord.Embed(title=f"🔎 {query}", description="\n".join(lines),
                              color=0x5865F2)
        await inter.followup.send(embed=embed, view=SearchView(results, inter.user.id, picked))

    @app_commands.command(description="ข้ามเพลง (โหวตเมื่อมีหลายคนในห้อง)")
    @app_commands.guild_only()
    async def skip(self, inter: discord.Interaction):
        p = control(inter)
        await inter.response.send_message(p.vote_skip(inter.user))

    @app_commands.command(description="กลับไปเพลงก่อนหน้า")
    @app_commands.guild_only()
    async def previous(self, inter: discord.Interaction):
        p = control(inter)
        if not p.previous():
            raise UserError("ไม่มีเพลงก่อนหน้า")
        await inter.response.send_message("⏮ กลับไปเพลงก่อนหน้า")

    @app_commands.command(description="หยุดชั่วคราว / เล่นต่อ")
    @app_commands.guild_only()
    async def pause(self, inter: discord.Interaction):
        p = control(inter)
        paused = p.toggle_pause()
        await inter.response.send_message("⏸ หยุดชั่วคราว" if paused else "▶️ เล่นต่อ")
        await p.update_panel()

    @app_commands.command(description="เล่นต่อ")
    @app_commands.guild_only()
    async def resume(self, inter: discord.Interaction):
        p = control(inter)
        if not p.is_paused:
            raise UserError("ไม่ได้หยุดอยู่")
        p.toggle_pause()
        await inter.response.send_message("▶️ เล่นต่อ")
        await p.update_panel()

    @app_commands.command(description="หยุด ล้างคิว และ ออกจากห้อง")
    @app_commands.guild_only()
    async def stop(self, inter: discord.Interaction):
        p = self.bot.players.get(inter.guild_id)
        if p:
            control(inter)
            await p.destroy()
        elif inter.guild.voice_client:
            await inter.guild.voice_client.disconnect(force=True)
        await inter.response.send_message("⏹ หยุดและออกแล้ว")

    @app_commands.command(description="ดูคิวเพลง")
    @app_commands.guild_only()
    async def queue(self, inter: discord.Interaction):
        p = get_player(inter)
        if not p.current and not p.queue:
            raise UserError("คิวว่าง")
        pages = build_queue_pages(p)
        view = PagesView(pages, inter.user.id)
        await inter.response.send_message(embed=pages[0], view=view)
        view.message = await inter.original_response()

    @app_commands.command(description="เพลงที่กำลังเล่น พร้อมปุ่มควบคุม")
    @app_commands.guild_only()
    async def nowplaying(self, inter: discord.Interaction):
        p = get_player(inter)
        if not p.current:
            raise UserError("ไม่มีเพลงเล่นอยู่")
        await inter.response.send_message(embed=build_now_playing(p), view=self.bot.panel_view)

    @app_commands.command(description="ปรับเสียง 0-150")
    @app_commands.guild_only()
    async def volume(self, inter: discord.Interaction,
                     percent: app_commands.Range[int, 0, 150]):
        p = control(inter)
        p.set_volume(percent)
        await self.bot.db.set_setting(inter.guild_id, "volume", percent)
        await inter.response.send_message(f"🔊 เสียง {percent}%")

    @app_commands.command(description="โหมดวนซ้ำ")
    @app_commands.choices(mode=[app_commands.Choice(name=v, value=k)
                                for k, v in LOOP_ICON.items()])
    @app_commands.guild_only()
    async def loop(self, inter: discord.Interaction, mode: app_commands.Choice[str]):
        p = control(inter)
        p.loop_mode = mode.value
        await self.bot.db.set_setting(inter.guild_id, "loop_mode", mode.value)
        await inter.response.send_message(f"วนซ้ำ: {mode.name}")
        await p.update_panel()

    @app_commands.command(description="สลับลำดับคิว")
    @app_commands.guild_only()
    async def shuffle(self, inter: discord.Interaction):
        p = control(inter)
        if len(p.queue) < 2:
            raise UserError("เพลงในคิวน้อยเกินไป")
        p.shuffle()
        await inter.response.send_message("🔀 สลับคิวแล้ว")

    @app_commands.command(description="ลบเพลงจากคิว")
    @app_commands.guild_only()
    async def remove(self, inter: discord.Interaction, index: app_commands.Range[int, 1]):
        p = control(inter)
        if index > len(p.queue):
            raise UserError("ไม่มีลำดับนี้")
        t = p.queue[index - 1]
        if t.requester_id != inter.user.id and not p.is_admin(inter.user):
            raise UserError("ลบได้เฉพาะเพลงของตัวเอง")
        p.remove_at(index)
        await inter.response.send_message(f"🗑 ลบ **{t.title}**")

    @app_commands.command(description="ย้ายเพลงในคิว")
    @app_commands.guild_only()
    async def move(self, inter: discord.Interaction, from_index: app_commands.Range[int, 1],
                   to_index: app_commands.Range[int, 1]):
        p = control(inter)
        if from_index > len(p.queue):
            raise UserError("ไม่มีลำดับนี้")
        t = p.move_track(from_index, to_index)
        await inter.response.send_message(f"↕️ ย้าย **{t.title}** ไปลำดับ {to_index}")

    @app_commands.command(description="ข้ามไปเพลงลำดับที่ระบุ")
    @app_commands.guild_only()
    async def jump(self, inter: discord.Interaction, index: app_commands.Range[int, 1]):
        p = control(inter)
        t = p.jump(index)
        if not t:
            raise UserError("ไม่มีลำดับนี้")
        await inter.response.send_message(f"⏩ ไปที่ **{t.title}**")

    @app_commands.command(description="ล้างคิว (ไม่หยุดเพลงปัจจุบัน)")
    @app_commands.guild_only()
    async def clear(self, inter: discord.Interaction):
        p = control(inter)
        n = p.clear_queue()
        await inter.response.send_message(f"🧹 ล้าง {n} เพลง (ใช้ /undo เพื่อย้อน)")

    @app_commands.command(description="ไปยังเวลา เช่น 1:30 หรือ 90")
    @app_commands.rename(time_="time")
    @app_commands.guild_only()
    async def seek(self, inter: discord.Interaction, time_: str):
        p = control(inter)
        sec = parse_time(time_)
        if sec is None or not p.current:
            raise UserError("รูปแบบเวลาไม่ถูกต้อง เช่น 1:30")
        if p.current.duration and sec >= p.current.duration:
            raise UserError("เกินความยาวเพลง")
        p.restart_at(sec)
        await inter.response.send_message(f"⏩ ไปที่ {fmt_time(sec)}")

    @app_commands.command(description="เล่นเพลงปัจจุบันใหม่ตั้งแต่ต้น")
    @app_commands.guild_only()
    async def replay(self, inter: discord.Interaction):
        p = control(inter)
        p.restart_at(0)
        await inter.response.send_message("🔄 เล่นใหม่")

    # ------------------------------------------------------- voice events
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        guild = member.guild
        p: Optional[GuildPlayer] = self.bot.players.get(guild.id)

        if member.id == self.bot.user.id:
            if before.channel and not after.channel and p and not self.bot.shutting_down:
                # Give discord.py a moment to auto-reconnect before giving up.
                await asyncio.sleep(5)
                if not guild.voice_client or not guild.voice_client.is_connected():
                    await p.destroy()
            return

        vc = guild.voice_client
        if not p or not vc or not vc.channel or p.stay_247:
            return
        if before.channel == vc.channel and after.channel != vc.channel:
            if not p.humans_in_channel():
                await asyncio.sleep(config.ALONE_TIMEOUT)
                if self.bot.players.get(guild.id) is p and not p.humans_in_channel():
                    await p.send("👋 ไม่มีใครอยู่ในห้อง ออกแล้ว")
                    await p.destroy()

    # ------------------------------------------------------------ restore
    async def restore_all(self):
        """Rejoin voice and continue queues saved before a restart."""
        await self.bot.wait_until_ready()
        for state in await self.bot.db.all_saved_queues():
            try:
                await self._restore(state)
            except Exception:
                log.exception("Restore failed for guild %s", state["guild_id"])
                await self.bot.db.clear_queue(state["guild_id"])

    async def _restore(self, state: dict):
        if time.time() - (state["updated_at"] or 0) > 6 * 3600:
            await self.bot.db.clear_queue(state["guild_id"])
            return
        guild = self.bot.get_guild(state["guild_id"])
        channel = guild and guild.get_channel(state["voice_channel_id"])
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            await self.bot.db.clear_queue(state["guild_id"])
            return
        settings = await self.bot.db.get_settings(guild.id)
        humans = [m for m in channel.members if not m.bot]
        if not humans and not settings["stay_247"]:
            await self.bot.db.clear_queue(guild.id)
            return
        tracks = [Track.from_dict(d) for d in state["data"]]
        if not tracks:
            return
        if not guild.voice_client:
            await channel.connect(timeout=20, reconnect=True, self_deaf=True)
        player = await self.bot.get_player(guild)
        text = guild.get_channel(state["text_channel_id"] or 0)
        if isinstance(text, discord.abc.Messageable):
            player.text_channel = text
        player._pending_start = max(float(state["position"] or 0) - 2, 0)
        player.add(tracks)
        await player.send(f"♻️ กู้คืนคิว {len(tracks)} เพลง เล่นต่อจากเดิม")
        log.info("Restored %d tracks in guild %s", len(tracks), guild.id)

    @app_commands.command(description="กรอไปข้างหน้า")
    @app_commands.guild_only()
    async def forward(self, inter: discord.Interaction,
                      seconds: app_commands.Range[int, 1, 600] = 10):
        p = control(inter)
        target = p.position + seconds
        if p.current and p.current.duration and target >= p.current.duration - 1:
            raise UserError("เกินความยาวเพลง ใช้ /skip แทน")
        p.restart_at(target)
        await inter.response.send_message(f"⏩ +{seconds}s → {fmt_time(target)}")

    @app_commands.command(description="ถอยหลัง")
    @app_commands.guild_only()
    async def backward(self, inter: discord.Interaction,
                       seconds: app_commands.Range[int, 1, 600] = 10):
        p = control(inter)
        target = max(p.position - seconds, 0)
        p.restart_at(target)
        await inter.response.send_message(f"⏪ -{seconds}s → {fmt_time(target)}")

    # ---------------------------------------------------------- navigation
    @app_commands.command(description="ย้อนการแก้คิวล่าสุด (shuffle, clear, remove, move, jump)")
    @app_commands.guild_only()
    async def undo(self, inter: discord.Interaction):
        p = control(inter)
        label = p.undo()
        if not label:
            raise UserError("ไม่มีอะไรให้ย้อน")
        await inter.response.send_message(f"↩️ ย้อน **{label}** แล้ว ({len(p.queue)} เพลงในคิว)")

    @app_commands.command(name="log", description="ดูว่าใครทำอะไรกับบอทล่าสุด")
    @app_commands.guild_only()
    async def log_cmd(self, inter: discord.Interaction,
                      limit: app_commands.Range[int, 5, 30] = 15):
        rows = await self.bot.db.audit_list(inter.guild_id, limit)
        if not rows:
            raise UserError("ยังไม่มีบันทึก")
        lines = [f"<t:{int(r['at'])}:R> <@{r['user_id']}> **{r['action']}**"
                 + (f" · {r['detail'][:60]}" if r["detail"] else "") for r in rows]
        e = discord.Embed(title="🗒 บันทึกการใช้งาน", description="\n".join(lines)[:4000],
                          color=0x95A5A6)
        await inter.response.send_message(embed=e, ephemeral=True,
                                          allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Music(bot))
