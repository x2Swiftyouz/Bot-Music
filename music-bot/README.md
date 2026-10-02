# Discord Music Bot

บอทเพลง Discord เขียนด้วย Python (discord.py 2.7 + yt-dlp + FFmpeg) เน้นความเสถียร

## ความสามารถ

**การเล่นเพลง**: รองรับ YouTube, SoundCloud, Bandcamp, ลิงก์ Spotify (track/album/playlist) และคำค้น มี autocomplete ใน `/play`, prefetch และ gapless (เปิดเพลงถัดไปรอไว้ก่อน), fade-in ตอนเริ่มเพลง, smooth volume และ fade ตอน pause, skip, stop

**ความเสถียร**: yt-dlp รันแยก thread, FFmpeg reconnect อัตโนมัติ, voice watchdog ที่ต่อเสียงใหม่เองเมื่อค้าง, เพลงเสียจะข้ามเอง, player loop ที่ crash จะเริ่มใหม่เอง, คิวถูกบันทึกลง SQLite ทุก 20 วินาทีและตอนปิดบอท เมื่อบอท restart จะกลับเข้าห้องและเล่นต่อจากตำแหน่งเดิม, มี `/health` endpoint สำหรับ monitoring

**การ์ด**: การ์ด Now Playing (รองรับภาษาไทย ใช้ฟอนต์ Kanit) แสดงชื่อศิลปิน ป้ายแหล่งเพลง เพลงถัดไป และ waveform ที่ขยับตามเพลง เลือกธีม (เบลอ/สีพื้น/มินิมอล) และรูปทรง (แนวนอน/จัตุรัสสำหรับมือถือ) ได้ด้วย `/settings card` มีการ์ดตอนกำลังโหลด การ์ดตอนเล่นไม่ได้ และการ์ดสรุปเมื่อจบเซสชัน ป้ายพิเศษ "ฮิต" สำหรับเพลงที่เปิดบ่อยในเซิร์ฟเวอร์ และป้ายวันเกิด (`/birthday set`) แชร์การ์ดเพลงที่ฟังอยู่ด้วย `/card` การ์ดส่งเป็น WebP และจะอัปโหลดใหม่เฉพาะเมื่อมีอะไรเปลี่ยน (หรือทุก `CARD_REFRESH` วินาที)

**เสียงและเนื้อเพลง**: ปรับความดังทุกเพลงให้เท่ากันด้วย FFmpeg loudnorm (เปิดเป็นค่าเริ่มต้น ปิดได้ด้วย `/settings normalize` หรือ `NORMALIZE=false`) เนื้อเพลงจาก lrclib.net ผ่าน `/lyrics` หรือปุ่ม 🎤 ถ้าเป็นแบบ synced มีปุ่ม 📍 ไปท่อนที่กำลังร้อง

**ห้องขอเพลง**: `/settings request #ห้อง` ตั้งห้องที่พิมพ์ชื่อเพลงหรือวางลิงก์แล้วเพิ่มเข้าคิวเลย ข้อความจะถูกลบอัตโนมัติ (ต้องมีสิทธิ์ Manage Messages) และข้อความหัวห้องจะกลายเป็น panel ตอนเพลงเล่น

**เริ่มต้นใช้งาน**: เข้าเซิร์ฟเวอร์ครั้งแรกบอทจะส่งวิธีใช้ 3 ขั้น ดูอีกครั้งได้ด้วย `/help start:True`

**UI**: panel มีปุ่ม ➕ เพิ่มเพลงผ่านหน้าต่าง (ไม่ต้องพิมพ์ /play), 🎙 เนื้อเพลงสดบน panel (เปลี่ยนท่อนทุก `LYRICS_REFRESH` วินาที), ปุ่ม ⏪ ⏩ กรอ 10 วินาที, หน้าคิวเลือกได้หลายเพลง ค้นในคิว ลบเพลงของฉัน และลบเพลงซ้ำ, เพลงจาก Spotify เลือกคลิป YouTube ที่ความยาวและชื่อตรงที่สุดจาก 5 ผล, หน้าคิวเลือกเพลงแล้วกด เล่นเลย / ขึ้นถัดไป / ลบ ได้ทันที, `/play` ตอบเป็น embed มีปก ลำดับในคิว และเวลาที่จะได้เล่น, ปุ่มควบคุมที่เปลี่ยนตามสถานะ (ยังใช้ได้หลัง restart), dropdown ระดับเสียง, โหมด compact สำหรับมือถือ, แสดงเวลาที่เหลือได้, คิวแบบแบ่งหน้า, `/search` แบบ dropdown, `/help` แบบเลือกหมวด, สถานะห้องเสียงแสดงชื่อเพลง

**การจัดการ**: vote skip, จำกัดเพลงต่อคน, จำกัดความยาวเพลง, กันเพลงซ้ำ, cooldown, โหมด 24/7, undo คิว 5 ครั้งล่าสุด, audit log (`/log`), เพลย์ลิสต์ส่วนตัว (save/load/rename/import/add/removetrack) และจำกัดจำนวนเพลย์ลิสต์ต่อคน, คำสั่ง prefix (`!p`, `!s`, `!q` ฯลฯ)

## ตั้งค่า Discord

1. สร้าง Application ที่ https://discord.com/developers/applications แล้วสร้าง Bot และคัดลอก token
2. ในหน้า Bot เปิด **Message Content Intent** (จำเป็นสำหรับคำสั่ง prefix ถ้าไม่ใช้ ให้ตั้ง `MESSAGE_CONTENT=false`)
3. เชิญบอทด้วย scope `bot` และ `applications.commands` และสิทธิ์ View Channels, Send Messages, Embed Links, Attach Files, Add Reactions, Connect, Speak, Set Voice Channel Status

## ติดตั้งแบบ Docker (แนะนำ)

```bash
cp .env.example .env     # ใส่ DISCORD_TOKEN
docker compose up -d --build
docker compose logs -f
```

ข้อมูลทั้งหมดอยู่ในโฟลเดอร์ `data/` และ yt-dlp จะอัปเดตเองทุกครั้งที่ container เริ่มใหม่ หาก YouTube มีปัญหาให้รัน `docker compose restart`

## ติดตั้งแบบปกติ

ต้องมี Python 3.10 ขึ้นไป, FFmpeg และ Deno (https://deno.land) อยู่ใน PATH

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

บน Linux server ใช้ไฟล์ `musicbot.service` กับ systemd ได้ ไฟล์นี้จะอัปเดต yt-dlp และ restart บอทให้อัตโนมัติ

## คำสั่ง

| หมวด | คำสั่ง |
|---|---|
| เล่น | `/play` `/search` `/skip` `/previous` `/pause` `/resume` `/stop` `/seek` `/forward` `/backward` `/replay` `/volume` |
| คิว | `/queue` `/nowplaying` `/remove` `/move` `/jump` `/shuffle` `/clear` `/undo` `/loop` |
| เพลย์ลิสต์ | `/playlist save/load/list/show/delete/rename/import/add/removetrack` |
| ข้อมูล | `/help` `/ping` `/about` `/log` `/lyrics` |
| การ์ด | `/card` `/birthday set/remove` |
| แอดมิน | `/settings request/card/normalize/247/announce/voteskip/compact/timeformat/show` |

**Prefix**: ค่าเริ่มต้นคือ `!` เปลี่ยนได้ด้วย `PREFIX` ใน `.env` หรือ mention บอทแทน prefix ก็ได้ คำย่อที่ใช้บ่อย: `!p` เล่น, `!s` ข้าม, `!q` คิว, `!np` เพลงที่เล่นอยู่, `!v 80` เสียง, `!l` วนซ้ำ, `!ff 30` กรอ, `!h` ช่วยเหลือ

ทุกคนที่อยู่ห้องเสียงเดียวกับบอทควบคุมเพลงได้ การข้ามเพลงจะใช้การโหวตเมื่อมีคนในห้องมากกว่า 2 คน ยกเว้นคนที่ขอเพลงนั้นหรือผู้ที่มีสิทธิ์ Manage Server

## แก้ปัญหา

- `Sign in to confirm you're not a bot`: export cookies.txt จาก browser แล้วตั้ง `YTDLP_COOKIES=data/cookies.txt`
- เพลงเล่นไม่ได้ทั้งหมด: อัปเดต yt-dlp ก่อน (`pip install -U "yt-dlp[default]"`) และตรวจว่ามี Deno
- `PrivilegedIntentsRequired`: เปิด Message Content Intent ใน Developer Portal หรือตั้ง `MESSAGE_CONTENT=false`
- slash command ไม่ขึ้น: global sync อาจใช้เวลาสักพัก ลองปิดเปิด Discord ใหม่

## โครงสร้าง

```
bot.py            entry point, health endpoint, error handler, graceful shutdown
config.py         อ่านค่าจาก .env
core/db.py        SQLite
core/sources.py   yt-dlp, Spotify, Track
core/stream.py    ตัวดาวน์โหลดเสียงสำหรับ pipe mode
core/player.py    คิว ระบบเล่นเพลง gapless watchdog
core/audio.py     smooth volume และ fade
core/card.py      การ์ด Now Playing, โหลด, error, แชร์ และสรุปเซสชัน
core/ui.py        embed, ปุ่ม, หน้า, modal
core/lyrics.py    เนื้อเพลงจาก lrclib.net
core/checks.py    ตรวจสิทธิ์
core/helpdata.py  รายการคำสั่งสำหรับ /help
cogs/music.py     คำสั่งหลัก, กู้คืนคิว
cogs/settings.py  ตั้งค่าเซิร์ฟเวอร์
cogs/cards.py     /card และ /birthday
cogs/request.py   ห้องขอเพลง
cogs/playlists.py เพลย์ลิสต์
cogs/info.py      /help /ping /about
cogs/prefix.py    คำสั่ง prefix
```

## เครดิต

ฟอนต์ Kanit โดย Cadson Demak ใช้สัญญาอนุญาต SIL Open Font License (ดู `assets/fonts/OFL.txt`) แนวคิด UI บางส่วนได้แรงบันดาลใจจาก Groove Music (MIT License)
