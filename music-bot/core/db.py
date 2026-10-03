"""SQLite storage: guild settings, saved queue, playlists, audit log."""

import json
import os
import time
from typing import Any, Optional

import aiosqlite

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    volume INTEGER,
    loop_mode TEXT,
    stay_247 INTEGER DEFAULT 0,
    vote_skip INTEGER DEFAULT 1,
    announce INTEGER DEFAULT 1,
    compact INTEGER DEFAULT 0,
    time_remaining INTEGER DEFAULT 0,
    card_theme TEXT DEFAULT 'blur',
    card_layout TEXT DEFAULT 'wide',
    normalize INTEGER,
    request_channel INTEGER DEFAULT 0,
    request_message INTEGER DEFAULT 0,
    auto_clean INTEGER,
    autoplay INTEGER DEFAULT 0,
    fair_queue INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS queue_state (
    guild_id INTEGER PRIMARY KEY,
    voice_channel_id INTEGER,
    text_channel_id INTEGER,
    data TEXT,
    position REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS playlists (
    user_id INTEGER,
    name TEXT,
    data TEXT,
    PRIMARY KEY (user_id, name)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    user_id INTEGER,
    action TEXT,
    detail TEXT,
    at REAL
);
CREATE INDEX IF NOT EXISTS audit_guild ON audit (guild_id, at);
CREATE TABLE IF NOT EXISTS track_plays (
    guild_id INTEGER,
    url TEXT,
    plays INTEGER DEFAULT 0,
    PRIMARY KEY (guild_id, url)
);
CREATE TABLE IF NOT EXISTS welcomed (
    guild_id INTEGER PRIMARY KEY,
    at REAL
);
CREATE TABLE IF NOT EXISTS birthdays (
    user_id INTEGER PRIMARY KEY,
    month INTEGER,
    day INTEGER
);
"""

SETTING_KEYS = (
    "volume", "loop_mode", "stay_247",
    "vote_skip", "announce", "compact", "time_remaining", "card_theme", "card_layout",
    "normalize", "request_channel", "request_message", "auto_clean", "autoplay", "fair_queue",
)

# Columns added after the first release: (name, SQL type with default).
MIGRATIONS = (("compact", "INTEGER DEFAULT 0"), ("time_remaining", "INTEGER DEFAULT 0"),
              ("card_theme", "TEXT DEFAULT 'blur'"), ("card_layout", "TEXT DEFAULT 'wide'"),
              ("normalize", "INTEGER"), ("request_channel", "INTEGER DEFAULT 0"),
              ("request_message", "INTEGER DEFAULT 0"), ("auto_clean", "INTEGER"),
              ("autoplay", "INTEGER DEFAULT 0"), ("fair_queue", "INTEGER DEFAULT 0"))


class Database:
    def __init__(self, path: str = config.DB_PATH):
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript(SCHEMA)
        cur = await self.conn.execute("PRAGMA table_info(guild_settings)")
        have = {r["name"] for r in await cur.fetchall()}
        for name, sql in MIGRATIONS:
            if name not in have:
                await self.conn.execute(f"ALTER TABLE guild_settings ADD COLUMN {name} {sql}")
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    # ------------------------------------------------------------ settings
    async def get_settings(self, guild_id: int) -> dict[str, Any]:
        cur = await self.conn.execute(
            "SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,))
        row = await cur.fetchone()
        defaults = {
            "volume": config.DEFAULT_VOLUME, "loop_mode": "off",
            "stay_247": 0, "vote_skip": 1, "announce": 1,
            "compact": 0, "time_remaining": 0,
            "card_theme": "blur", "card_layout": "wide",
            "normalize": int(config.NORMALIZE),  # NULL in the table = follow .env
            "request_channel": 0, "request_message": 0,
            "auto_clean": int(config.AUTO_CLEAN),  # NULL in the table = follow .env
            "autoplay": 0, "fair_queue": 0,
        }
        if row:
            for k in SETTING_KEYS:
                if row[k] is not None:
                    defaults[k] = row[k]
        return defaults

    async def set_setting(self, guild_id: int, key: str, value: Any):
        if key not in SETTING_KEYS:
            raise ValueError(key)
        await self.conn.execute(
            f"INSERT INTO guild_settings (guild_id, {key}) VALUES (?, ?) "
            f"ON CONFLICT(guild_id) DO UPDATE SET {key} = excluded.{key}",
            (guild_id, value))
        await self.conn.commit()

    # --------------------------------------------------------- queue state
    async def save_queue(self, guild_id: int, voice_id: int, text_id: Optional[int],
                         tracks: list[dict], position: float):
        await self.conn.execute(
            "INSERT OR REPLACE INTO queue_state VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, voice_id, text_id, json.dumps(tracks), position, time.time()))
        await self.conn.commit()

    async def clear_queue(self, guild_id: int):
        await self.conn.execute("DELETE FROM queue_state WHERE guild_id = ?", (guild_id,))
        await self.conn.commit()

    async def all_saved_queues(self) -> list[dict]:
        cur = await self.conn.execute("SELECT * FROM queue_state")
        rows = await cur.fetchall()
        return [
            {**dict(r), "data": json.loads(r["data"] or "[]")} for r in rows
        ]

    # ----------------------------------------------------------- playlists
    async def save_playlist(self, user_id: int, name: str, tracks: list[dict]):
        await self.conn.execute(
            "INSERT OR REPLACE INTO playlists VALUES (?, ?, ?)",
            (user_id, name, json.dumps(tracks)))
        await self.conn.commit()

    async def get_playlist(self, user_id: int, name: str) -> Optional[list[dict]]:
        cur = await self.conn.execute(
            "SELECT data FROM playlists WHERE user_id = ? AND name = ?", (user_id, name))
        row = await cur.fetchone()
        return json.loads(row["data"]) if row else None

    async def list_playlists(self, user_id: int) -> list[tuple[str, int]]:
        cur = await self.conn.execute(
            "SELECT name, data FROM playlists WHERE user_id = ? ORDER BY name", (user_id,))
        return [(r["name"], len(json.loads(r["data"]))) for r in await cur.fetchall()]

    async def rename_playlist(self, user_id: int, old: str, new: str) -> bool:
        try:
            cur = await self.conn.execute(
                "UPDATE playlists SET name = ? WHERE user_id = ? AND name = ?",
                (new, user_id, old))
            await self.conn.commit()
            return cur.rowcount > 0
        except Exception:
            return False

    async def count_playlists(self, user_id: int) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) c FROM playlists WHERE user_id = ?", (user_id,))
        return (await cur.fetchone())["c"]

    async def delete_playlist(self, user_id: int, name: str) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM playlists WHERE user_id = ? AND name = ?", (user_id, name))
        await self.conn.commit()
        return cur.rowcount > 0

    # --------------------------------------------------------------- audit
    async def audit(self, guild_id: int, user_id: int, action: str, detail: str = ""):
        await self.conn.execute(
            "INSERT INTO audit (guild_id, user_id, action, detail, at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, action, detail[:200], time.time()))
        # keep the last 500 rows per guild
        await self.conn.execute(
            "DELETE FROM audit WHERE guild_id = ? AND id NOT IN "
            "(SELECT id FROM audit WHERE guild_id = ? ORDER BY id DESC LIMIT 500)",
            (guild_id, guild_id))
        await self.conn.commit()

    async def audit_list(self, guild_id: int, limit: int = 15) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM audit WHERE guild_id = ? ORDER BY id DESC LIMIT ?", (guild_id, limit))
        return [dict(r) for r in await cur.fetchall()]

    async def request_channels(self) -> dict[int, int]:
        """guild_id -> request channel id, for every server that set one."""
        cur = await self.conn.execute(
            "SELECT guild_id, request_channel FROM guild_settings WHERE request_channel > 0")
        return {r["guild_id"]: r["request_channel"] for r in await cur.fetchall()}

    async def mark_welcomed(self, guild_id: int) -> bool:
        """True the first time a server is seen (show the getting-started message once)."""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO welcomed (guild_id, at) VALUES (?, ?)", (guild_id, time.time()))
        await self.conn.commit()
        return cur.rowcount > 0

    # ---------------------------------------------------- card badges
    async def bump_play(self, guild_id: int, url: str) -> int:
        """Count one more play of a track in this server, return the new total."""
        await self.conn.execute(
            "INSERT INTO track_plays (guild_id, url, plays) VALUES (?, ?, 1) "
            "ON CONFLICT(guild_id, url) DO UPDATE SET plays = plays + 1", (guild_id, url))
        await self.conn.commit()
        return await self.get_plays(guild_id, url)

    async def get_plays(self, guild_id: int, url: str) -> int:
        cur = await self.conn.execute(
            "SELECT plays FROM track_plays WHERE guild_id = ? AND url = ?", (guild_id, url))
        row = await cur.fetchone()
        return row["plays"] if row else 0

    async def set_birthday(self, user_id: int, month: int, day: int):
        await self.conn.execute("INSERT OR REPLACE INTO birthdays VALUES (?, ?, ?)",
                                (user_id, month, day))
        await self.conn.commit()

    async def delete_birthday(self, user_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM birthdays WHERE user_id = ?", (user_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_birthday(self, user_id: int) -> Optional[tuple[int, int]]:
        cur = await self.conn.execute(
            "SELECT month, day FROM birthdays WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return (row["month"], row["day"]) if row else None
