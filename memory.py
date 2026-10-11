"""
memory.py — Scaramouche Bot v6
Full persistence: mood, affection, trust, grudges, rivalry, milestones,
inside jokes, absence tracking, anniversaries, slow burn, personality drift,
memory summaries, muted users, contradiction tracking, name progression.
"""

from __future__ import annotations

import aiosqlite
import time
import json
import logging
import os

# Use Railway volume if available, otherwise current directory
_data_dir = "/data" if os.path.isdir("/data") else "."
DB_PATH = os.path.join(_data_dir, "scaramouche.db")
SHARED_DB_PATH = os.path.join(_data_dir, "shared_state.db")
log = logging.getLogger(__name__)


class Memory:
    db_path: str = DB_PATH
    shared_db_path: str = SHARED_DB_PATH

    def __init__(self, bot_name: str = "scaramouche", db_path: str | None = None,
                 shared_db_path: str | None = None):
        self.bot_name = (bot_name or "scaramouche").strip().lower()
        self.db_path = os.fspath(db_path or os.path.join(_data_dir, f"{self.bot_name}.db"))
        self.shared_db_path = os.fspath(shared_db_path or SHARED_DB_PATH)
        self._muted: dict[int, float] = {}

    def _scope_db_path(self, scope: str) -> str:
        return self.shared_db_path if "::" in (scope or "") else self.db_path

    async def init(self):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await self._configure_database(db)
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id          INTEGER PRIMARY KEY,
                    username         TEXT,
                    display_name     TEXT,
                    romance_mode     INTEGER DEFAULT 0,
                    nsfw_mode        INTEGER DEFAULT 0,
                    proactive        INTEGER DEFAULT 1,
                    allow_dms        INTEGER DEFAULT 1,
                    timezone_name    TEXT    DEFAULT 'America/Los_Angeles',
                    quiet_hours_start INTEGER DEFAULT 23,
                    quiet_hours_end   INTEGER DEFAULT 8,
                    dm_frequency_hours INTEGER DEFAULT 8,
                    recent_activity_grace_minutes INTEGER DEFAULT 45,
                    mood             INTEGER DEFAULT 0,
                    affection        INTEGER DEFAULT 0,
                    trust            INTEGER DEFAULT 0,
                    rival_id         INTEGER DEFAULT NULL,
                    grudge_nick      TEXT    DEFAULT NULL,
                    affection_nick   TEXT    DEFAULT NULL,
                    message_count    INTEGER DEFAULT 0,
                    milestone_last   INTEGER DEFAULT 0,
                    first_seen       REAL    DEFAULT 0,
                    last_seen        REAL    DEFAULT 0,
                    last_active      REAL    DEFAULT 0,
                    greeted_today    INTEGER DEFAULT 0,
                    anniversary_last INTEGER DEFAULT 0,
                    slow_burn        INTEGER DEFAULT 0,
                    slow_burn_day    INTEGER DEFAULT 0,
                    slow_burn_fired  INTEGER DEFAULT 0,
                    drift_score      INTEGER DEFAULT 0,
                    memory_summary   TEXT    DEFAULT NULL,
                    summary_msg_count INTEGER DEFAULT 0,
                    last_statement   TEXT    DEFAULT NULL,
                    style_profile    TEXT    DEFAULT NULL,
                    emotional_arc    TEXT    DEFAULT 'guarded',
                    conflict_open    INTEGER DEFAULT 0,
                    conflict_summary TEXT    DEFAULT NULL,
                    last_conflict_ts REAL    DEFAULT 0,
                    repair_progress  INTEGER DEFAULT 0,
                    callback_memory  TEXT    DEFAULT NULL,
                    callback_ts      REAL    DEFAULT 0,
                    repair_count     INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER,
                    channel_id  INTEGER,
                    role        TEXT,
                    content     TEXT,
                    ts          REAL
                );
                CREATE TABLE IF NOT EXISTS channels (
                    channel_id  INTEGER PRIMARY KEY,
                    guild_id    INTEGER,
                    last_active REAL
                );
                CREATE TABLE IF NOT EXISTS proactive_cooldown (
                    channel_id  INTEGER PRIMARY KEY,
                    last_sent   REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS dm_cooldown (
                    user_id   INTEGER PRIMARY KEY,
                    last_sent REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS bot_mutes (
                    user_id    INTEGER PRIMARY KEY,
                    expires_ts REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reminders (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER,
                    channel_id  INTEGER,
                    reminder    TEXT,
                    due_ts      REAL,
                    done        INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS inside_jokes (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    joke    TEXT,
                    ts      REAL
                );
                CREATE TABLE IF NOT EXISTS lore_cooldown (
                    channel_id  INTEGER PRIMARY KEY,
                    last_sent   REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS starter_cooldown (
                    channel_id  INTEGER PRIMARY KEY,
                    last_sent   REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS trivia (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER,
                    correct     INTEGER DEFAULT 0,
                    wrong       INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS active_trivia (
                    channel_id   INTEGER PRIMARY KEY,
                    asker_id     INTEGER,
                    question     TEXT,
                    answer       TEXT,
                    source_note  TEXT DEFAULT NULL,
                    asked_ts     REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS roast_battles (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id  INTEGER,
                    user1_id    INTEGER,
                    user2_id    INTEGER,
                    round       INTEGER DEFAULT 0,
                    scores      TEXT    DEFAULT '{}',
                    active      INTEGER DEFAULT 1,
                    ts          REAL
                );
                CREATE TABLE IF NOT EXISTS phrase_cooldowns (
                    scope       TEXT,
                    phrase_key  TEXT,
                    last_used   REAL DEFAULT 0,
                    PRIMARY KEY (scope, phrase_key)
                );
                CREATE TABLE IF NOT EXISTS bot_relationships (
                    pair_key       TEXT PRIMARY KEY,
                    stage          TEXT DEFAULT 'enemy',
                    respect        INTEGER DEFAULT 5,
                    tension        INTEGER DEFAULT 70,
                    shared_history TEXT DEFAULT NULL,
                    last_exchange  REAL DEFAULT 0,
                    last_theme     TEXT DEFAULT NULL
                );
                CREATE TABLE IF NOT EXISTS bot_banter_log (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair_key  TEXT,
                    speaker   TEXT,
                    content   TEXT,
                    theme     TEXT DEFAULT NULL,
                    ts        REAL
                );
                CREATE TABLE IF NOT EXISTS user_topics (
                    user_id    INTEGER,
                    topic      TEXT,
                    count      INTEGER DEFAULT 0,
                    last_seen  REAL DEFAULT 0,
                    PRIMARY KEY (user_id, topic)
                );
                CREATE TABLE IF NOT EXISTS shared_inside_jokes (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id   INTEGER,
                    joke      TEXT,
                    source    TEXT DEFAULT 'shared',
                    ts        REAL
                );
                CREATE TABLE IF NOT EXISTS relationship_milestones (
                    scope      TEXT,
                    marker     TEXT,
                    note       TEXT,
                    ts         REAL DEFAULT 0,
                    PRIMARY KEY (scope, marker)
                );
                CREATE TABLE IF NOT EXISTS scene_state (
                    channel_id    INTEGER PRIMARY KEY,
                    location      TEXT DEFAULT NULL,
                    situation     TEXT DEFAULT NULL,
                    last_beat     TEXT DEFAULT NULL,
                    emotional_temp TEXT DEFAULT NULL,
                    objective     TEXT DEFAULT NULL,
                    present       TEXT DEFAULT NULL,
                    important_prop TEXT DEFAULT NULL,
                    updated_ts    REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS memory_bank (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER,
                    kind       TEXT,
                    memory     TEXT,
                    weight     INTEGER DEFAULT 1,
                    last_used  REAL DEFAULT 0,
                    ts         REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id        INTEGER PRIMARY KEY,
                    voice_enabled  INTEGER DEFAULT 1,
                    utility_mode   INTEGER DEFAULT 1,
                    duo_autoplay   INTEGER DEFAULT 1,
                    rp_depth       TEXT    DEFAULT 'medium'
                );
            """)
            migrations = [
                ("username",           "TEXT"),
                ("display_name",       "TEXT"),
                ("romance_mode",       "INTEGER DEFAULT 0"),
                ("nsfw_mode",          "INTEGER DEFAULT 0"),
                ("proactive",          "INTEGER DEFAULT 1"),
                ("allow_dms",          "INTEGER DEFAULT 1"),
                ("timezone_name",      "TEXT DEFAULT 'America/Los_Angeles'"),
                ("quiet_hours_start",  "INTEGER DEFAULT 23"),
                ("quiet_hours_end",    "INTEGER DEFAULT 8"),
                ("dm_frequency_hours", "INTEGER DEFAULT 8"),
                ("recent_activity_grace_minutes", "INTEGER DEFAULT 45"),
                ("mood",               "INTEGER DEFAULT 0"),
                ("affection",          "INTEGER DEFAULT 0"),
                ("trust",              "INTEGER DEFAULT 0"),
                ("rival_id",           "INTEGER DEFAULT NULL"),
                ("grudge_nick",        "TEXT DEFAULT NULL"),
                ("affection_nick",     "TEXT DEFAULT NULL"),
                ("message_count",      "INTEGER DEFAULT 0"),
                ("milestone_last",     "INTEGER DEFAULT 0"),
                ("first_seen",         "REAL DEFAULT 0"),
                ("last_seen",          "REAL DEFAULT 0"),
                ("last_active",        "REAL DEFAULT 0"),
                ("greeted_today",      "INTEGER DEFAULT 0"),
                ("anniversary_last",   "INTEGER DEFAULT 0"),
                ("slow_burn",          "INTEGER DEFAULT 0"),
                ("slow_burn_day",      "INTEGER DEFAULT 0"),
                ("slow_burn_fired",    "INTEGER DEFAULT 0"),
                ("drift_score",        "INTEGER DEFAULT 0"),
                ("memory_summary",     "TEXT DEFAULT NULL"),
                ("summary_msg_count",  "INTEGER DEFAULT 0"),
                ("last_statement",     "TEXT DEFAULT NULL"),
                ("style_profile",      "TEXT DEFAULT NULL"),
                ("emotional_arc",      "TEXT DEFAULT 'guarded'"),
                ("conflict_open",      "INTEGER DEFAULT 0"),
                ("conflict_summary",   "TEXT DEFAULT NULL"),
                ("last_conflict_ts",   "REAL DEFAULT 0"),
                ("repair_progress",    "INTEGER DEFAULT 0"),
                ("callback_memory",    "TEXT DEFAULT NULL"),
                ("callback_ts",        "REAL DEFAULT 0"),
                ("repair_count",       "INTEGER DEFAULT 0"),
            ]
            for col, default in migrations:
                try:
                    await db.execute(f"ALTER TABLE users ADD COLUMN {col} {default}")
                except aiosqlite.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        log.exception("users migration failed", extra={"column": col})
                        raise
            now = time.time()
            await db.execute("DELETE FROM bot_mutes WHERE expires_ts<=?", (now,))
            async with db.execute("SELECT user_id,expires_ts FROM bot_mutes") as cur:
                self._muted = {int(row[0]): float(row[1]) for row in await cur.fetchall()}
            await db.commit()

        async with aiosqlite.connect(self.shared_db_path, timeout=15.0) as db:
            await self._configure_database(db)
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS shared_users (
                    user_id      INTEGER PRIMARY KEY,
                    username     TEXT,
                    display_name TEXT,
                    first_seen   REAL DEFAULT 0,
                    last_seen    REAL DEFAULT 0,
                    last_active  REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS bot_relationships (
                    pair_key       TEXT PRIMARY KEY,
                    stage          TEXT DEFAULT 'enemy',
                    respect        INTEGER DEFAULT 5,
                    tension        INTEGER DEFAULT 70,
                    shared_history TEXT DEFAULT NULL,
                    last_exchange  REAL DEFAULT 0,
                    last_theme     TEXT DEFAULT NULL
                );
                CREATE TABLE IF NOT EXISTS bot_banter_log (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair_key  TEXT,
                    speaker   TEXT,
                    content   TEXT,
                    theme     TEXT DEFAULT NULL,
                    ts        REAL
                );
                CREATE TABLE IF NOT EXISTS shared_inside_jokes (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id   INTEGER,
                    joke      TEXT,
                    source    TEXT DEFAULT 'shared',
                    ts        REAL
                );
                CREATE TABLE IF NOT EXISTS relationship_milestones (
                    scope      TEXT,
                    marker     TEXT,
                    note       TEXT,
                    ts         REAL DEFAULT 0,
                    PRIMARY KEY (scope, marker)
                );
                CREATE TABLE IF NOT EXISTS duo_sessions (
                    channel_id    INTEGER PRIMARY KEY,
                    mode          TEXT DEFAULT 'both',
                    topic         TEXT DEFAULT NULL,
                    initiator_bot TEXT DEFAULT NULL,
                    initiator_user_id INTEGER DEFAULT 0,
                    last_speaker  TEXT DEFAULT NULL,
                    awaiting_bot  TEXT DEFAULT NULL,
                    autoplay_remaining INTEGER DEFAULT 0,
                    next_autoplay_ts REAL DEFAULT 0,
                    expires_ts    REAL DEFAULT 0,
                    updated_ts    REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS channel_speaker_modes (
                    channel_id   INTEGER PRIMARY KEY,
                    speaker_mode TEXT DEFAULT 'auto',
                    updated_ts   REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS duo_story_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id  INTEGER,
                    story_type  TEXT,
                    topic       TEXT,
                    status      TEXT DEFAULT 'open',
                    outcome     TEXT DEFAULT NULL,
                    enemy       TEXT DEFAULT NULL,
                    ts          REAL DEFAULT 0,
                    updated_ts  REAL DEFAULT 0
                );
            """)
            for stmt in (
                "ALTER TABLE duo_sessions ADD COLUMN initiator_user_id INTEGER DEFAULT 0",
                "ALTER TABLE duo_sessions ADD COLUMN awaiting_bot TEXT DEFAULT NULL",
                "ALTER TABLE duo_sessions ADD COLUMN autoplay_remaining INTEGER DEFAULT 0",
                "ALTER TABLE duo_sessions ADD COLUMN next_autoplay_ts REAL DEFAULT 0",
            ):
                try:
                    await db.execute(stmt)
                except aiosqlite.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        log.exception("shared migration failed", extra={"statement": stmt})
                        raise
            await db.commit()

        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            try:
                await db.execute("ALTER TABLE messages ADD COLUMN bot_name TEXT DEFAULT NULL")
            except aiosqlite.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
            try:
                await db.execute("ALTER TABLE scene_state ADD COLUMN important_prop TEXT DEFAULT NULL")
            except aiosqlite.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
            await db.execute("UPDATE messages SET bot_name=? WHERE bot_name IS NULL", (self.bot_name,))
            await db.commit()

    @staticmethod
    async def _configure_database(db: aiosqlite.Connection) -> None:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=15000")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA foreign_keys=ON")

    async def healthcheck(self) -> bool:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("SELECT 1") as cur:
                row = await cur.fetchone()
        if not row or row[0] != 1:
            raise RuntimeError("database health probe failed")
        return True

    async def integrity_check(self) -> bool:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("PRAGMA quick_check") as cur:
                row = await cur.fetchone()
        if not row or row[0] != "ok":
            raise RuntimeError("database quick_check failed")
        return True

    async def backup(self, destination: str | None = None) -> str:
        destination = destination or f"{self.db_path}.backup"
        os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
        async with aiosqlite.connect(self.db_path, timeout=15.0) as source:
            async with aiosqlite.connect(destination, timeout=15.0) as target:
                await source.backup(target)
        return destination

    # ── Users ────────────────────────────────────────────────────────────────
    async def upsert_user(self, user_id: int, username: str, display_name: str):
        now = time.time()
        previous_last_active = 0.0
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("SELECT first_seen,last_active FROM users WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone()
            if row:
                previous_last_active = float(row[1] or 0)
                await db.execute(
                    "UPDATE users SET username=?,display_name=?,last_seen=?,last_active=? WHERE user_id=?",
                    (username, display_name, now, now, user_id))
            else:
                await db.execute(
                    "INSERT INTO users (user_id,username,display_name,last_seen,last_active,first_seen) VALUES (?,?,?,?,?,?)",
                    (user_id, username, display_name, now, now, now))
            await db.commit()
        async with aiosqlite.connect(self.shared_db_path, timeout=15.0) as db:
            async with db.execute("SELECT first_seen FROM shared_users WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone()
            first_seen = row[0] if row and row[0] else now
            await db.execute(
                "INSERT INTO shared_users (user_id,username,display_name,first_seen,last_seen,last_active) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,display_name=excluded.display_name,"
                "last_seen=excluded.last_seen,last_active=excluded.last_active",
                (user_id, username, display_name, first_seen, now, now),
            )
            await db.commit()
        return previous_last_active

    async def get_user(self, user_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("""
                SELECT user_id,username,display_name,romance_mode,nsfw_mode,proactive,allow_dms,
                       timezone_name,quiet_hours_start,quiet_hours_end,dm_frequency_hours,recent_activity_grace_minutes,
                       mood,affection,trust,rival_id,grudge_nick,affection_nick,message_count,
                       milestone_last,first_seen,last_seen,last_active,greeted_today,anniversary_last,
                       slow_burn,slow_burn_fired,drift_score,memory_summary,last_statement,
                       style_profile,emotional_arc,conflict_open,conflict_summary,last_conflict_ts,repair_progress,
                       callback_memory,callback_ts,repair_count
                FROM users WHERE user_id=?
            """, (user_id,)) as cur:
                row = await cur.fetchone()
                if not row: return None
                user = {
                    "user_id": row[0], "username": row[1], "display_name": row[2],
                    "romance_mode": bool(row[3]), "nsfw_mode": bool(row[4]),
                    "proactive": bool(row[5]), "allow_dms": bool(row[6]),
                    "timezone_name": row[7] or "America/Los_Angeles",
                    "quiet_hours_start": row[8] if row[8] is not None else 23,
                    "quiet_hours_end": row[9] if row[9] is not None else 8,
                    "dm_frequency_hours": row[10] if row[10] is not None else 8,
                    "recent_activity_grace_minutes": row[11] if row[11] is not None else 45,
                    "mood": row[12] or 0, "affection": row[13] or 0, "trust": row[14] or 0,
                    "rival_id": row[15], "grudge_nick": row[16], "affection_nick": row[17],
                    "message_count": row[18] or 0, "milestone_last": row[19] or 0,
                    "first_seen": row[20] or 0, "last_seen": row[21] or 0,
                    "last_active": row[22] or 0, "greeted_today": bool(row[23]),
                    "anniversary_last": row[24] or 0,
                    "slow_burn": row[25] or 0, "slow_burn_fired": bool(row[26]),
                    "drift_score": row[27] or 0, "memory_summary": row[28],
                    "last_statement": row[29],
                    "style_profile": json.loads(row[30]) if row[30] else {},
                    "emotional_arc": row[31] or "guarded",
                    "conflict_open": bool(row[32]),
                    "conflict_summary": row[33],
                    "last_conflict_ts": row[34] or 0,
                    "repair_progress": row[35] or 0,
                    "callback_memory": row[36],
                    "callback_ts": row[37] or 0,
                    "repair_count": row[38] or 0,
                }
        prefs = await self.get_user_preferences(user_id)
        user.update(prefs)
        return user

    async def get_user_preferences(self, user_id: int) -> dict:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute(
                "INSERT INTO user_preferences (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
                (user_id,),
            )
            await db.commit()
            async with db.execute(
                "SELECT voice_enabled,utility_mode,duo_autoplay,rp_depth FROM user_preferences WHERE user_id=?",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()
        return {
            "voice_enabled": bool(row[0]) if row else True,
            "utility_mode": bool(row[1]) if row else True,
            "duo_autoplay": bool(row[2]) if row else True,
            "rp_depth": (row[3] or "medium") if row else "medium",
        }

    async def set_user_preference(self, user_id: int, field: str, value):
        allowed = {"voice_enabled", "utility_mode", "duo_autoplay", "rp_depth"}
        if field not in allowed:
            raise ValueError(f"Unknown preference: {field}")
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute(
                "INSERT INTO user_preferences (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
                (user_id,),
            )
            await db.execute(f"UPDATE user_preferences SET {field}=? WHERE user_id=?", (value, user_id))
            await db.commit()

    async def record_topic(self, user_id: int, topic: str):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute(
                "INSERT INTO user_topics (user_id,topic,count,last_seen) VALUES (?,?,1,?) "
                "ON CONFLICT(user_id,topic) DO UPDATE SET count=count+1,last_seen=excluded.last_seen",
                (user_id, topic[:40], time.time()),
            )
            await db.commit()

    async def get_top_topics(self, user_id: int, limit: int = 3) -> list[dict]:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute(
                "SELECT topic,count,last_seen FROM user_topics WHERE user_id=? ORDER BY count DESC,last_seen DESC LIMIT ?",
                (user_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [{"topic": row[0], "count": row[1] or 0, "last_seen": row[2] or 0} for row in rows]

    async def add_shared_inside_joke(self, user_id: int, joke: str, source: str = "shared"):
        async with aiosqlite.connect(self.shared_db_path, timeout=15.0) as db:
            async with db.execute("SELECT COUNT(*) FROM shared_inside_jokes WHERE user_id=?", (user_id,)) as cur:
                count = (await cur.fetchone())[0]
            if count >= 12:
                await db.execute(
                    "DELETE FROM shared_inside_jokes WHERE id=(SELECT id FROM shared_inside_jokes WHERE user_id=? ORDER BY ts ASC LIMIT 1)",
                    (user_id,),
                )
            await db.execute(
                "INSERT INTO shared_inside_jokes (user_id,joke,source,ts) VALUES (?,?,?,?)",
                (user_id, joke[:220], source[:40], time.time()),
            )
            await db.commit()

    async def get_random_shared_inside_joke(self, user_id: int) -> str | None:
        async with aiosqlite.connect(self.shared_db_path, timeout=15.0) as db:
            async with db.execute(
                "SELECT joke FROM shared_inside_jokes WHERE user_id=? ORDER BY RANDOM() LIMIT 1",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else None

    async def has_milestone(self, scope: str, marker: str) -> bool:
        async with aiosqlite.connect(self._scope_db_path(scope), timeout=15.0) as db:
            async with db.execute(
                "SELECT 1 FROM relationship_milestones WHERE scope=? AND marker=?",
                (scope, marker),
            ) as cur:
                return bool(await cur.fetchone())

    async def add_milestone(self, scope: str, marker: str, note: str):
        async with aiosqlite.connect(self._scope_db_path(scope), timeout=15.0) as db:
            await db.execute(
                "INSERT INTO relationship_milestones (scope,marker,note,ts) VALUES (?,?,?,?) "
                "ON CONFLICT(scope,marker) DO UPDATE SET note=excluded.note,ts=excluded.ts",
                (scope[:80], marker[:80], note[:220], time.time()),
            )
            await db.commit()

    async def get_recent_milestones(self, scope: str, limit: int = 3) -> list[str]:
        async with aiosqlite.connect(self._scope_db_path(scope), timeout=15.0) as db:
            async with db.execute(
                "SELECT note FROM relationship_milestones WHERE scope=? ORDER BY ts DESC LIMIT ?",
                (scope, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [row[0] for row in rows if row and row[0]]

    async def get_scene_state(self, channel_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute(
                "SELECT location,situation,last_beat,emotional_temp,objective,present,important_prop,updated_ts FROM scene_state WHERE channel_id=?",
                (channel_id,),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        return {
            "location": row[0] or "",
            "situation": row[1] or "",
            "last_beat": row[2] or "",
            "emotional_temp": row[3] or "",
            "objective": row[4] or "",
            "present": row[5] or "",
            "important_prop": row[6] or "",
            "updated_ts": row[7] or 0,
        }

    async def update_scene_state(self, channel_id: int, **fields):
        current = await self.get_scene_state(channel_id) or {}
        payload = {
            "location": (fields.get("location") or current.get("location") or "")[:120],
            "situation": (fields.get("situation") or current.get("situation") or "")[:180],
            "last_beat": (fields.get("last_beat") or current.get("last_beat") or "")[:180],
            "emotional_temp": (fields.get("emotional_temp") or current.get("emotional_temp") or "")[:80],
            "objective": (fields.get("objective") or current.get("objective") or "")[:140],
            "present": (fields.get("present") or current.get("present") or "")[:180],
            "important_prop": (fields.get("important_prop") or current.get("important_prop") or "")[:120],
            "updated_ts": time.time(),
        }
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute(
                "INSERT INTO scene_state (channel_id,location,situation,last_beat,emotional_temp,objective,present,important_prop,updated_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(channel_id) DO UPDATE SET location=excluded.location,situation=excluded.situation,"
                "last_beat=excluded.last_beat,emotional_temp=excluded.emotional_temp,objective=excluded.objective,"
                "present=excluded.present,important_prop=excluded.important_prop,updated_ts=excluded.updated_ts",
                (
                    channel_id,
                    payload["location"],
                    payload["situation"],
                    payload["last_beat"],
                    payload["emotional_temp"],
                    payload["objective"],
                    payload["present"],
                    payload["important_prop"],
                    payload["updated_ts"],
                ),
            )
            await db.commit()

    async def clear_scene_state(self, channel_id: int):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("DELETE FROM scene_state WHERE channel_id=?", (channel_id,))
            await db.commit()

    async def add_memory_event(self, user_id: int, kind: str, memory: str, weight: int = 1):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute(
                "SELECT id,weight FROM memory_bank WHERE user_id=? AND kind=? AND memory=?",
                (user_id, kind[:40], memory[:240]),
            ) as cur:
                row = await cur.fetchone()
            if row:
                await db.execute(
                    "UPDATE memory_bank SET weight=MIN(weight+?,10), ts=? WHERE id=?",
                    (max(1, int(weight)), time.time(), row[0]),
                )
            else:
                await db.execute(
                    "INSERT INTO memory_bank (user_id,kind,memory,weight,last_used,ts) VALUES (?,?,?,?,?,?)",
                    (user_id, kind[:40], memory[:240], max(1, int(weight)), 0, time.time()),
                )
            await db.execute(
                "DELETE FROM memory_bank WHERE user_id=? AND id NOT IN (SELECT id FROM memory_bank WHERE user_id=? ORDER BY weight DESC, ts DESC LIMIT 16)",
                (user_id, user_id),
            )
            await db.commit()

    async def get_weighted_memory_event(self, user_id: int) -> dict | None:
        cutoff = time.time() - 3600
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute(
                "SELECT id,kind,memory,weight FROM memory_bank WHERE user_id=? ORDER BY weight DESC, ts DESC LIMIT 12",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()
            if not rows:
                return None
            chosen = None
            for row in rows:
                chosen = row
                if random.random() < min(0.85, max(0.15, (row[3] or 1) / 10)):
                    break
            if chosen:
                await db.execute("UPDATE memory_bank SET last_used=? WHERE id=?", (time.time(), chosen[0]))
                await db.commit()
                return {"kind": chosen[1] or "", "memory": chosen[2] or "", "weight": chosen[3] or 1}
        return None

    async def get_memory_bank_entries(self, user_id: int, limit: int = 8) -> list[dict]:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute(
                "SELECT kind,memory,weight,last_used,ts FROM memory_bank WHERE user_id=? "
                "ORDER BY weight DESC, ts DESC LIMIT ?",
                (user_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [
            {
                "kind": row[0] or "",
                "memory": row[1] or "",
                "weight": row[2] or 1,
                "last_used": row[3] or 0,
                "ts": row[4] or 0,
            }
            for row in rows
            if row and row[1]
        ]

    async def forget_memory_matches(self, user_id: int, query: str) -> dict:
        needle = (query or "").strip().lower()[:80]
        if not needle:
            return {}

        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("BEGIN IMMEDIATE")
            topic_cur = await db.execute(
                "DELETE FROM user_topics WHERE user_id=? AND INSTR(LOWER(topic),?)>0",
                (user_id, needle),
            )
            joke_cur = await db.execute(
                "DELETE FROM inside_jokes WHERE user_id=? AND INSTR(LOWER(joke),?)>0",
                (user_id, needle),
            )
            memory_cur = await db.execute(
                "DELETE FROM memory_bank WHERE user_id=? AND "
                "(INSTR(LOWER(kind),?)>0 OR INSTR(LOWER(memory),?)>0)",
                (user_id, needle, needle),
            )
            message_cur = await db.execute(
                "DELETE FROM messages WHERE user_id=? AND INSTR(LOWER(content),?)>0",
                (user_id, needle),
            )
            reminder_cur = await db.execute(
                "DELETE FROM reminders WHERE user_id=? AND INSTR(LOWER(reminder),?)>0",
                (user_id, needle),
            )
            trivia_cur = await db.execute(
                "DELETE FROM active_trivia WHERE asker_id=? AND "
                "(INSTR(LOWER(question),?)>0 OR INSTR(LOWER(answer),?)>0 OR INSTR(LOWER(COALESCE(source_note,'')),?)>0)",
                (user_id, needle, needle, needle),
            )
            milestone_cur = await db.execute(
                "DELETE FROM relationship_milestones WHERE scope LIKE ? AND INSTR(LOWER(note),?)>0",
                (f"{self.bot_name}:user:{user_id}%", needle),
            )
            user_cur = await db.execute(
                """UPDATE users SET
                       callback_memory=CASE WHEN INSTR(LOWER(COALESCE(callback_memory,'')),?)>0 THEN NULL ELSE callback_memory END,
                       callback_ts=CASE WHEN INSTR(LOWER(COALESCE(callback_memory,'')),?)>0 THEN 0 ELSE callback_ts END,
                       memory_summary=CASE WHEN INSTR(LOWER(COALESCE(memory_summary,'')),?)>0 THEN NULL ELSE memory_summary END,
                       summary_msg_count=CASE WHEN INSTR(LOWER(COALESCE(memory_summary,'')),?)>0 THEN 0 ELSE summary_msg_count END,
                       last_statement=CASE WHEN INSTR(LOWER(COALESCE(last_statement,'')),?)>0 THEN NULL ELSE last_statement END,
                       conflict_summary=CASE WHEN INSTR(LOWER(COALESCE(conflict_summary,'')),?)>0 THEN NULL ELSE conflict_summary END,
                       conflict_open=CASE WHEN INSTR(LOWER(COALESCE(conflict_summary,'')),?)>0 THEN 0 ELSE conflict_open END
                   WHERE user_id=? AND (
                       INSTR(LOWER(COALESCE(callback_memory,'')),?)>0 OR
                       INSTR(LOWER(COALESCE(memory_summary,'')),?)>0 OR
                       INSTR(LOWER(COALESCE(last_statement,'')),?)>0 OR
                       INSTR(LOWER(COALESCE(conflict_summary,'')),?)>0
                   )""",
                (needle, needle, needle, needle, needle, needle, needle, user_id,
                 needle, needle, needle, needle),
            )
            await db.commit()
        async with aiosqlite.connect(self.shared_db_path, timeout=15.0) as db:
            await db.execute("BEGIN IMMEDIATE")
            shared_joke_cur = await db.execute(
                "DELETE FROM shared_inside_jokes WHERE user_id=? AND INSTR(LOWER(joke),?)>0",
                (user_id, needle),
            )
            shared_milestone_cur = await db.execute(
                "DELETE FROM relationship_milestones WHERE scope LIKE ? AND INSTR(LOWER(note),?)>0",
                (f"%user:{user_id}", needle),
            )
            await db.commit()
        return {
            "topics": int(topic_cur.rowcount or 0),
            "jokes": int(joke_cur.rowcount or 0),
            "shared_jokes": int(shared_joke_cur.rowcount or 0),
            "memories": int(memory_cur.rowcount or 0),
            "messages": int(message_cur.rowcount or 0),
            "reminders": int(reminder_cur.rowcount or 0),
            "trivia": int(trivia_cur.rowcount or 0),
            "milestones": int(milestone_cur.rowcount or 0) + int(shared_milestone_cur.rowcount or 0),
            "profile_fields": int(user_cur.rowcount or 0),
        }

    async def set_mode(self, user_id: int, field: str, value: bool):
        allowed = {"nsfw_mode","romance_mode","proactive","allow_dms"}
        if field not in allowed: raise ValueError(f"Unknown: {field}")
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (int(value), user_id))
            await db.commit()

    async def set_timezone(self, user_id: int, timezone_name: str):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("UPDATE users SET timezone_name=? WHERE user_id=?", (timezone_name[:64], user_id))
            await db.commit()

    async def set_quiet_hours(self, user_id: int, start_hour: int, end_hour: int):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute(
                "UPDATE users SET quiet_hours_start=?, quiet_hours_end=? WHERE user_id=?",
                (int(start_hour) % 24, int(end_hour) % 24, user_id),
            )
            await db.commit()

    async def set_dm_frequency(self, user_id: int, hours: int):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("UPDATE users SET dm_frequency_hours=? WHERE user_id=?", (max(1, min(72, int(hours))), user_id))
            await db.commit()

    async def set_recent_activity_grace(self, user_id: int, minutes: int):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute(
                "UPDATE users SET recent_activity_grace_minutes=? WHERE user_id=?",
                (max(5, min(720, int(minutes))), user_id),
            )
            await db.commit()

    async def respects_dm_timing(self, user_id: int) -> bool:
        user = await self.get_user(user_id)
        if not user or not user.get("allow_dms", True):
            return False
        now = time.time()
        grace = max(5, int(user.get("recent_activity_grace_minutes", 45))) * 60
        if user.get("last_seen", 0) and (now - user.get("last_seen", 0)) < grace:
            return False
        try:
            async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
                async with db.execute("SELECT last_sent FROM dm_cooldown WHERE user_id=?", (user_id,)) as cur:
                    row = await cur.fetchone()
            if row and row[0]:
                min_gap = max(1, int(user.get("dm_frequency_hours", 8))) * 3600
                if (now - (row[0] or 0)) < min_gap:
                    return False
        except Exception:
            return False
        return True

    # ── Mood ─────────────────────────────────────────────────────────────────
    async def update_mood(self, user_id: int, delta: int):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("UPDATE users SET mood=MAX(-10,MIN(10,mood+?)) WHERE user_id=?", (delta, user_id))
            await db.commit()

    async def get_mood(self, user_id: int) -> int:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("SELECT mood FROM users WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone(); return row[0] if row else 0

    async def random_mood_swing(self, user_id: int):
        """Randomly shift mood slightly — unpredictable personality."""
        delta = random.choice([-3, -2, -1, -1, 0, 0, 1, 2])
        await self.update_mood(user_id, delta)

    # ── Affection ─────────────────────────────────────────────────────────────
    async def update_affection(self, user_id: int, delta: int):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("UPDATE users SET affection=MAX(0,MIN(100,affection+?)) WHERE user_id=?", (delta, user_id))
            await db.commit()

    async def set_affection_nick(self, user_id: int, nick: str):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("UPDATE users SET affection_nick=? WHERE user_id=?", (nick, user_id))
            await db.commit()

    # ── Trust ─────────────────────────────────────────────────────────────────
    async def update_trust(self, user_id: int, delta: int):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("UPDATE users SET trust=MAX(0,MIN(100,trust+?)) WHERE user_id=?", (delta, user_id))
            await db.commit()

    # ── Grudge ────────────────────────────────────────────────────────────────
    async def set_grudge_nick(self, user_id: int, nick: str | None):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("UPDATE users SET grudge_nick=? WHERE user_id=?", (nick, user_id))
            await db.commit()

    # ── Rivalry ───────────────────────────────────────────────────────────────
    async def set_rival(self, user_id: int, rival_id: int | None):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("UPDATE users SET rival_id=? WHERE user_id=?", (rival_id, user_id))
            await db.commit()

    # ── Slow burn ─────────────────────────────────────────────────────────────
    async def increment_slow_burn(self, user_id: int) -> tuple[int, bool]:
        """
        Increment slow burn if user was kind today and hasn't been incremented yet today.
        Returns (new_score, threshold_hit).
        Threshold is 7 consecutive kind days.
        """
        today = int(time.time() // 86400)
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute(
                "SELECT slow_burn, slow_burn_day, slow_burn_fired FROM users WHERE user_id=?",
                (user_id,)
            ) as cur:
                row = await cur.fetchone()
                if not row: return 0, False
                score, last_day, fired = row[0] or 0, row[1] or 0, bool(row[2])

            if last_day == today:
                return score, False  # Already counted today

            # If last kind day was yesterday, continue streak; else reset
            if today - last_day <= 1:
                new_score = score + 1
            else:
                new_score = 1  # Reset streak

            await db.execute(
                "UPDATE users SET slow_burn=?, slow_burn_day=? WHERE user_id=?",
                (new_score, today, user_id))
            await db.commit()

        # Threshold: 7 consecutive kind days, hasn't fired yet
        if new_score >= 7 and not fired:
            async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
                await db.execute("UPDATE users SET slow_burn_fired=1 WHERE user_id=?", (user_id,))
                await db.commit()
            return new_score, True
        return new_score, False

    async def reset_slow_burn_fired(self, user_id: int):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("UPDATE users SET slow_burn=0, slow_burn_fired=0 WHERE user_id=?", (user_id,))
            await db.commit()

    # ── Personality drift ─────────────────────────────────────────────────────
    async def update_drift(self, user_id: int, delta: int):
        """Drift: 0=unchanged, 100=maximally drifted. Changes subtly over weeks."""
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("UPDATE users SET drift_score=MAX(0,MIN(100,drift_score+?)) WHERE user_id=?", (delta, user_id))
            await db.commit()

    async def set_style_profile(self, user_id: int, profile: dict):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute(
                "UPDATE users SET style_profile=? WHERE user_id=?",
                (json.dumps(profile, ensure_ascii=True)[:1200], user_id),
            )
            await db.commit()

    async def set_emotional_arc(self, user_id: int, arc: str):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("UPDATE users SET emotional_arc=? WHERE user_id=?", (arc[:40], user_id))
            await db.commit()

    async def open_conflict(self, user_id: int, summary: str):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute(
                "UPDATE users SET conflict_open=1, conflict_summary=?, last_conflict_ts=?, repair_progress=0 WHERE user_id=?",
                (summary[:300], time.time(), user_id),
            )
            await db.commit()

    async def resolve_conflict(self, user_id: int):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute(
                "UPDATE users SET conflict_open=0, conflict_summary=NULL, repair_progress=0, repair_count=repair_count+1 WHERE user_id=?",
                (user_id,),
            )
            await db.commit()

    async def record_repair_attempt(self, user_id: int, repair_text: str = "") -> bool:
        user = await self.get_user(user_id)
        if not user or not user.get("conflict_open"):
            return False
        progress = int(user.get("repair_progress", 0)) + 1
        summary = user.get("conflict_summary") or ""
        updated_summary = summary
        if repair_text:
            updated_summary = f"{summary[:140]} | repair: {repair_text[:120]}".strip(" |")
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute(
                "UPDATE users SET conflict_summary=?, repair_progress=? WHERE user_id=?",
                (updated_summary[:300], progress, user_id),
            )
            await db.commit()
        days_open = 0
        if user.get("last_conflict_ts"):
            days_open = (time.time() - user.get("last_conflict_ts", 0)) / 86400
        should_resolve = progress >= 2 or (progress >= 1 and days_open >= 1.5)
        if should_resolve:
            await self.resolve_conflict(user_id)
            return True
        return False

    async def set_callback_memory(self, user_id: int, callback_memory: str):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute(
                "UPDATE users SET callback_memory=?, callback_ts=? WHERE user_id=?",
                (callback_memory[:300], time.time(), user_id),
            )
            await db.commit()

    async def consume_phrase(self, scope: str, phrase_key: str, cooldown_seconds: int) -> bool:
        now = time.time()
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute(
                "SELECT last_used FROM phrase_cooldowns WHERE scope=? AND phrase_key=?",
                (scope, phrase_key),
            ) as cur:
                row = await cur.fetchone()
            if row and (now - (row[0] or 0)) < cooldown_seconds:
                return False
            await db.execute(
                "INSERT INTO phrase_cooldowns (scope, phrase_key, last_used) VALUES (?,?,?) "
                "ON CONFLICT(scope, phrase_key) DO UPDATE SET last_used=excluded.last_used",
                (scope, phrase_key, now),
            )
            await db.commit()
        return True

    async def consume_phrase_with_status(self, scope: str, phrase_key: str, cooldown_seconds: int) -> tuple[bool, int]:
        now = time.time()
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute(
                "SELECT last_used FROM phrase_cooldowns WHERE scope=? AND phrase_key=?",
                (scope, phrase_key),
            ) as cur:
                row = await cur.fetchone()
            if row and (now - (row[0] or 0)) < cooldown_seconds:
                remaining = max(0, int(cooldown_seconds - (now - (row[0] or 0))))
                return False, remaining
            await db.execute(
                "INSERT INTO phrase_cooldowns (scope, phrase_key, last_used) VALUES (?,?,?) "
                "ON CONFLICT(scope, phrase_key) DO UPDATE SET last_used=excluded.last_used",
                (scope, phrase_key, now),
            )
            await db.commit()
        return True, 0

    async def get_bot_relationship(self, pair_key: str) -> dict:
        async with aiosqlite.connect(self.shared_db_path, timeout=15.0) as db:
            await db.execute(
                "INSERT INTO bot_relationships (pair_key) VALUES (?) ON CONFLICT(pair_key) DO NOTHING",
                (pair_key,),
            )
            await db.commit()
            async with db.execute(
                "SELECT stage,respect,tension,shared_history,last_exchange,last_theme FROM bot_relationships WHERE pair_key=?",
                (pair_key,),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return {
                "pair_key": pair_key,
                "stage": "enemy",
                "respect": 5,
                "tension": 70,
                "shared_history": "",
                "last_exchange": 0,
                "last_theme": "",
            }
        return {
            "pair_key": pair_key,
            "stage": row[0] or "enemy",
            "respect": row[1] or 0,
            "tension": row[2] or 0,
            "shared_history": row[3] or "",
            "last_exchange": row[4] or 0,
            "last_theme": row[5] or "",
        }

    async def update_bot_relationship(
        self,
        pair_key: str,
        stage: str,
        respect: int,
        tension: int,
        theme: str | None = None,
        history_note: str | None = None,
        touched_exchange: bool = False,
    ):
        relation = await self.get_bot_relationship(pair_key)
        history = relation.get("shared_history", "")
        notes = [note for note in history.split(" || ") if note]
        if history_note:
            cleaned = history_note[:180]
            if not notes or notes[-1] != cleaned:
                notes.append(cleaned)
                notes = notes[-6:]
        async with aiosqlite.connect(self.shared_db_path, timeout=15.0) as db:
            await db.execute(
                "UPDATE bot_relationships SET stage=?, respect=?, tension=?, shared_history=?, last_exchange=?, last_theme=? WHERE pair_key=?",
                (
                    stage[:40],
                    max(0, min(100, respect)),
                    max(0, min(100, tension)),
                    " || ".join(notes),
                    time.time() if touched_exchange else relation.get("last_exchange", 0),
                    (theme or relation.get("last_theme") or "")[:60],
                    pair_key,
                ),
            )
            await db.commit()

    async def record_bot_banter(self, pair_key: str, speaker: str, content: str, theme: str | None = None):
        async with aiosqlite.connect(self.shared_db_path, timeout=15.0) as db:
            await db.execute(
                "INSERT INTO bot_banter_log (pair_key,speaker,content,theme,ts) VALUES (?,?,?,?,?)",
                (pair_key, speaker[:40], content[:500], (theme or "")[:60], time.time()),
            )
            await db.execute(
                "DELETE FROM bot_banter_log WHERE pair_key=? AND id NOT IN (SELECT id FROM bot_banter_log WHERE pair_key=? ORDER BY ts DESC LIMIT 40)",
                (pair_key, pair_key),
            )
            await db.commit()

    async def get_recent_bot_banter(self, pair_key: str, limit: int = 8) -> list[dict]:
        async with aiosqlite.connect(self.shared_db_path, timeout=15.0) as db:
            async with db.execute(
                "SELECT speaker,content,theme,ts FROM bot_banter_log WHERE pair_key=? ORDER BY ts DESC LIMIT ?",
                (pair_key, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [
            {"speaker": row[0], "content": row[1], "theme": row[2] or "", "ts": row[3] or 0}
            for row in rows
        ]

    async def set_duo_session(
        self,
        channel_id: int,
        mode: str,
        topic: str,
        initiator_bot: str,
        initiator_user_id: int = 0,
        awaiting_bot: str = "",
        autoplay_turns: int = 0,
        autoplay_delay: int = 6,
        ttl_seconds: int = 900,
    ):
        now = time.time()
        async with aiosqlite.connect(self.shared_db_path, timeout=15.0) as db:
            await db.execute(
                "INSERT INTO duo_sessions (channel_id,mode,topic,initiator_bot,initiator_user_id,last_speaker,awaiting_bot,autoplay_remaining,next_autoplay_ts,expires_ts,updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(channel_id) DO UPDATE SET mode=excluded.mode, topic=excluded.topic, "
                "initiator_bot=excluded.initiator_bot, initiator_user_id=excluded.initiator_user_id, awaiting_bot=excluded.awaiting_bot, autoplay_remaining=excluded.autoplay_remaining, "
                "next_autoplay_ts=excluded.next_autoplay_ts, expires_ts=excluded.expires_ts, updated_ts=excluded.updated_ts",
                (
                    channel_id,
                    mode[:40],
                    topic[:300],
                    initiator_bot[:40],
                    initiator_user_id,
                    "",
                    awaiting_bot[:40],
                    max(0, int(autoplay_turns)),
                    now + max(2, int(autoplay_delay)),
                    now + ttl_seconds,
                    now,
                ),
            )
            await db.commit()

    async def get_duo_session(self, channel_id: int) -> dict | None:
        now = time.time()
        async with aiosqlite.connect(self.shared_db_path, timeout=15.0) as db:
            async with db.execute(
                "SELECT mode,topic,initiator_bot,initiator_user_id,last_speaker,awaiting_bot,autoplay_remaining,next_autoplay_ts,expires_ts,updated_ts "
                "FROM duo_sessions WHERE channel_id=?",
                (channel_id,),
            ) as cur:
                row = await cur.fetchone()
            if row and (row[8] or 0) < now:
                await db.execute("DELETE FROM duo_sessions WHERE channel_id=?", (channel_id,))
                await db.commit()
                return None
        if not row:
            return None
        return {
            "mode": row[0] or "both",
            "topic": row[1] or "",
            "initiator_bot": row[2] or "",
            "initiator_user_id": row[3] or 0,
            "last_speaker": row[4] or "",
            "awaiting_bot": row[5] or "",
            "autoplay_remaining": row[6] or 0,
            "next_autoplay_ts": row[7] or 0,
            "expires_ts": row[8] or 0,
            "updated_ts": row[9] or 0,
        }

    async def bump_duo_session(self, channel_id: int, speaker_bot: str, partner_bot: str = "", ttl_seconds: int = 900, autoplay_delay: int = 6):
        now = time.time()
        current = await self.get_duo_session(channel_id)
        awaiting_bot = current.get("awaiting_bot", "") if current else ""
        autoplay_remaining = current.get("autoplay_remaining", 0) if current else 0
        next_autoplay_ts = current.get("next_autoplay_ts", 0) if current else 0
        if awaiting_bot == speaker_bot and autoplay_remaining > 0:
            autoplay_remaining = max(0, autoplay_remaining - 1)
            if autoplay_remaining == 0:
                awaiting_bot = ""
                next_autoplay_ts = 0
            elif partner_bot:
                awaiting_bot = partner_bot[:40]
                next_autoplay_ts = now + max(2, int(autoplay_delay))
        async with aiosqlite.connect(self.shared_db_path, timeout=15.0) as db:
            await db.execute(
                "UPDATE duo_sessions SET last_speaker=?, awaiting_bot=?, autoplay_remaining=?, next_autoplay_ts=?, updated_ts=?, expires_ts=? WHERE channel_id=?",
                (speaker_bot[:40], awaiting_bot[:40], autoplay_remaining, next_autoplay_ts, now, now + ttl_seconds, channel_id),
            )
            await db.commit()

    async def get_due_duo_sessions(self, bot_name: str) -> list[dict]:
        now = time.time()
        async with aiosqlite.connect(self.shared_db_path, timeout=15.0) as db:
            async with db.execute(
                "SELECT channel_id,mode,topic,initiator_bot,initiator_user_id,last_speaker,awaiting_bot,autoplay_remaining,next_autoplay_ts,expires_ts,updated_ts "
                "FROM duo_sessions WHERE awaiting_bot=? AND autoplay_remaining>0 AND next_autoplay_ts<=? AND expires_ts>?",
                (bot_name, now, now),
            ) as cur:
                rows = await cur.fetchall()
        return [
            {
                "channel_id": row[0],
                "mode": row[1] or "both",
                "topic": row[2] or "",
                "initiator_bot": row[3] or "",
                "initiator_user_id": row[4] or 0,
                "last_speaker": row[5] or "",
                "awaiting_bot": row[6] or "",
                "autoplay_remaining": row[7] or 0,
                "next_autoplay_ts": row[8] or 0,
                "expires_ts": row[9] or 0,
                "updated_ts": row[10] or 0,
            }
            for row in rows
        ]

    async def clear_duo_session(self, channel_id: int):
        async with aiosqlite.connect(self.shared_db_path, timeout=15.0) as db:
            await db.execute("DELETE FROM duo_sessions WHERE channel_id=?", (channel_id,))
            await db.commit()

    async def set_channel_speaker_mode(self, channel_id: int, speaker_mode: str):
        async with aiosqlite.connect(self.shared_db_path, timeout=15.0) as db:
            await db.execute(
                "INSERT INTO channel_speaker_modes (channel_id,speaker_mode,updated_ts) VALUES (?,?,?) "
                "ON CONFLICT(channel_id) DO UPDATE SET speaker_mode=excluded.speaker_mode, updated_ts=excluded.updated_ts",
                (channel_id, speaker_mode[:20], time.time()),
            )
            await db.commit()

    async def get_channel_speaker_mode(self, channel_id: int) -> str:
        async with aiosqlite.connect(self.shared_db_path, timeout=15.0) as db:
            async with db.execute("SELECT speaker_mode FROM channel_speaker_modes WHERE channel_id=?", (channel_id,)) as cur:
                row = await cur.fetchone()
        return (row[0] if row else "auto") or "auto"

    async def start_duo_story(self, channel_id: int, story_type: str, topic: str, enemy: str = "") -> int:
        now = time.time()
        async with aiosqlite.connect(self.shared_db_path, timeout=15.0) as db:
            cur = await db.execute(
                "INSERT INTO duo_story_log (channel_id,story_type,topic,status,outcome,enemy,ts,updated_ts) VALUES (?,?,?,?,?,?,?,?)",
                (channel_id, story_type[:40], topic[:300], "open", "", enemy[:120], now, now),
            )
            await db.commit()
            return cur.lastrowid

    async def resolve_duo_story(self, channel_id: int, story_type: str, outcome: str):
        async with aiosqlite.connect(self.shared_db_path, timeout=15.0) as db:
            await db.execute(
                "UPDATE duo_story_log SET status='resolved', outcome=?, updated_ts=? "
                "WHERE id=(SELECT id FROM duo_story_log WHERE channel_id=? AND story_type=? ORDER BY ts DESC LIMIT 1)",
                (outcome[:300], time.time(), channel_id, story_type[:40]),
            )
            await db.commit()

    async def get_recent_duo_stories(self, channel_id: int, limit: int = 5) -> list[dict]:
        async with aiosqlite.connect(self.shared_db_path, timeout=15.0) as db:
            async with db.execute(
                "SELECT story_type,topic,status,outcome,enemy,ts,updated_ts FROM duo_story_log WHERE channel_id=? ORDER BY ts DESC LIMIT ?",
                (channel_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [
            {
                "story_type": row[0] or "",
                "topic": row[1] or "",
                "status": row[2] or "open",
                "outcome": row[3] or "",
                "enemy": row[4] or "",
                "ts": row[5] or 0,
                "updated_ts": row[6] or 0,
            }
            for row in rows
        ]

    # ── Memory summary ────────────────────────────────────────────────────────
    async def needs_summary(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute(
                "SELECT message_count, summary_msg_count FROM users WHERE user_id=?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
                if not row: return False
                count, last = row[0] or 0, row[1] or 0
                return count >= 500 and (count - last) >= 50

    async def save_summary(self, user_id: int, summary: str):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("SELECT message_count FROM users WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone(); count = row[0] if row else 0
            await db.execute(
                "UPDATE users SET memory_summary=?, summary_msg_count=? WHERE user_id=?",
                (summary[:1000], count, user_id))
            await db.commit()

    # ── Contradiction tracking ────────────────────────────────────────────────
    async def update_last_statement(self, user_id: int, statement: str):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("UPDATE users SET last_statement=? WHERE user_id=?", (statement[:300], user_id))
            await db.commit()

    # ── Message count & milestones ────────────────────────────────────────────
    async def increment_message_count(self, user_id: int) -> tuple[int, bool]:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("UPDATE users SET message_count=message_count+1 WHERE user_id=?", (user_id,))
            await db.commit()
            async with db.execute("SELECT message_count,milestone_last FROM users WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone()
                if not row: return 1, False
                count, last = row
        for m in [50,100,250,500,1000]:
            if count >= m and last < m:
                async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
                    await db.execute("UPDATE users SET milestone_last=? WHERE user_id=?", (m, user_id))
                    await db.commit()
                return count, True
        return count, False

    async def get_top_users(self, limit: int = 8) -> list[dict]:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute(
                "SELECT user_id,display_name,message_count FROM users ORDER BY message_count DESC LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
        return [{"user_id":r[0],"display_name":r[1],"message_count":r[2]} for r in rows]

    # ── Greetings ─────────────────────────────────────────────────────────────
    async def should_greet(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("SELECT greeted_today,last_active FROM users WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone()
                if not row: return False
                return not row[0] and (time.time() - (row[1] or 0)) < 172800

    async def mark_greeted(self, user_id: int):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("UPDATE users SET greeted_today=1 WHERE user_id=?", (user_id,))
            await db.commit()

    async def reset_daily_greetings(self):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("UPDATE users SET greeted_today=0"); await db.commit()

    # ── Anniversary ───────────────────────────────────────────────────────────
    async def check_anniversary(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute(
                "SELECT first_seen,anniversary_last,message_count FROM users WHERE user_id=?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
                if not row: return False
                first, ann_last, count = row
                if count < 10 or not first: return False
                days = (time.time()-first)/86400
                if days < 30: return False
                years = int(days/365.25)
                if years == 0: return False
                ann_ts = first + years*365.25*86400
                return abs(time.time()-ann_ts)<172800 and (time.time()-(ann_last or 0))>365.25*86400*0.9

    async def mark_anniversary(self, user_id: int):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("UPDATE users SET anniversary_last=? WHERE user_id=?", (time.time(), user_id))
            await db.commit()

    # ── Absence ───────────────────────────────────────────────────────────────
    async def get_absent_romance_users(self, days: float = 3.0) -> list[dict]:
        cutoff = time.time() - days*86400
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("""
                SELECT user_id,display_name,last_active,
                       (julianday('now')-julianday(datetime(last_active,'unixepoch'))) as days_gone
                FROM users WHERE romance_mode=1 AND allow_dms=1
                  AND last_active>0 AND last_active<? AND message_count>5
            """, (cutoff,)) as cur:
                rows = await cur.fetchall()
        return [{"user_id":r[0],"display_name":r[1],"last_active":r[2],"days_gone":round(r[3] or 0,1)} for r in rows]

    # ── Inside jokes ─────────────────────────────────────────────────────────
    async def add_inside_joke(self, user_id: int, joke: str):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("SELECT COUNT(*) FROM inside_jokes WHERE user_id=?", (user_id,)) as cur:
                count = (await cur.fetchone())[0]
            if count >= 10:
                await db.execute("DELETE FROM inside_jokes WHERE id=(SELECT id FROM inside_jokes WHERE user_id=? ORDER BY ts ASC LIMIT 1)", (user_id,))
            await db.execute("INSERT INTO inside_jokes (user_id,joke,ts) VALUES (?,?,?)", (user_id,joke[:200],time.time()))
            await db.commit()

    async def get_random_inside_joke(self, user_id: int) -> str | None:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("SELECT joke FROM inside_jokes WHERE user_id=? ORDER BY RANDOM() LIMIT 1", (user_id,)) as cur:
                row = await cur.fetchone(); return row[0] if row else None

    async def get_joke_count(self, user_id: int) -> int:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("SELECT COUNT(*) FROM inside_jokes WHERE user_id=?", (user_id,)) as cur:
                return (await cur.fetchone())[0]

    # ── Conversation history ──────────────────────────────────────────────────
    async def get_history(self, user_id: int, channel_id: int, limit: int = 200, *,
                          max_chars_per_message: int | None = None,
                          max_total_chars: int | None = None) -> list[dict]:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("""
                SELECT role,content FROM messages WHERE user_id=? AND channel_id=? AND (bot_name=? OR bot_name IS NULL)
                ORDER BY ts DESC LIMIT ?
            """, (user_id,channel_id,self.bot_name,limit)) as cur:
                rows = await cur.fetchall()
        bounded: list[dict] = []
        remaining = max_total_chars if max_total_chars and max_total_chars > 0 else None
        # Rows are newest-first. Apply the total budget from the most relevant
        # end, then restore chronological order for the provider.
        for role, raw_content in rows:
            content = str(raw_content or "")
            if max_chars_per_message and max_chars_per_message > 0:
                content = content[:max_chars_per_message]
            if remaining is not None:
                if remaining <= 0:
                    break
                content = content[:remaining]
                remaining -= len(content)
            if content:
                bounded.append({"role": role, "content": content})
        return list(reversed(bounded))

    async def get_random_old_message(self, user_id: int) -> str | None:
        cutoff = time.time()-86400*2
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("SELECT content FROM messages WHERE user_id=? AND role='user' AND (bot_name=? OR bot_name IS NULL) AND ts<? ORDER BY RANDOM() LIMIT 1", (user_id,self.bot_name,cutoff)) as cur:
                row = await cur.fetchone(); return row[0] if row else None

    async def get_recent_messages(self, user_id: int, limit: int = 10) -> list[str]:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("SELECT content FROM messages WHERE user_id=? AND role='user' AND (bot_name=? OR bot_name IS NULL) ORDER BY ts DESC LIMIT ?", (user_id,self.bot_name,limit)) as cur:
                return [r[0] for r in await cur.fetchall()]

    async def get_channel_recent(self, channel_id: int, limit: int = 20) -> list[dict]:
        """Get recent messages from all users in a channel."""
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("""
                SELECT m.role, m.content, u.display_name FROM messages m
                LEFT JOIN users u ON m.user_id=u.user_id
                WHERE m.channel_id=? AND (m.bot_name=? OR m.bot_name IS NULL) ORDER BY m.ts DESC LIMIT ?
            """, (channel_id,self.bot_name,limit)) as cur:
                rows = await cur.fetchall()
        return [{"role":r[0],"content":r[1],"name":r[2] or "?"} for r in reversed(rows)]

    async def add_message(self, user_id: int, channel_id: int, role: str, content: str):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("INSERT INTO messages (user_id,channel_id,role,content,ts,bot_name) VALUES (?,?,?,?,?,?)", (user_id,channel_id,role,content,time.time(),self.bot_name))
            await db.commit()

    async def get_recent_assistant_messages(
        self,
        limit: int = 25,
        channel_id: int | None = None,
        user_id: int | None = None,
    ) -> list[str]:
        query = "SELECT content FROM messages WHERE role='assistant' AND (bot_name=? OR bot_name IS NULL)"
        params: list[int | str] = [self.bot_name]
        if channel_id is not None:
            query += " AND channel_id=?"
            params.append(channel_id)
        if user_id is not None:
            query += " AND user_id=?"
            params.append(user_id)
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)

        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()
        return [row[0] for row in rows if row and row[0]]

    async def reset_user(self, user_id: int):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM inside_jokes WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM shared_inside_jokes WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM user_topics WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM memory_bank WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM reminders WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM trivia WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM active_trivia WHERE asker_id=?", (user_id,))
            await db.execute("DELETE FROM roast_battles WHERE user1_id=? OR user2_id=?", (user_id, user_id))
            await db.execute("DELETE FROM user_preferences WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM dm_cooldown WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM bot_mutes WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM relationship_milestones WHERE scope LIKE ?", (f"{self.bot_name}:user:{user_id}%",))
            await db.execute("DELETE FROM scene_state WHERE channel_id=?", (user_id,))
            await db.execute("DELETE FROM users WHERE user_id=?", (user_id,))
            await db.commit()
        async with aiosqlite.connect(self.shared_db_path, timeout=15.0) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("DELETE FROM shared_inside_jokes WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM relationship_milestones WHERE scope LIKE ?", (f"%user:{user_id}",))
            await db.execute("DELETE FROM duo_sessions WHERE initiator_user_id=?", (user_id,))
            await db.execute("DELETE FROM shared_users WHERE user_id=?", (user_id,))
            await db.commit()
        self._muted.pop(user_id, None)

    # ── Mute ──────────────────────────────────────────────────────────────────
    async def mute_user(self, user_id: int, seconds: int = 600):
        expires = time.time() + max(60, min(int(seconds), 86_400))
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute(
                "INSERT INTO bot_mutes(user_id,expires_ts) VALUES(?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET expires_ts=excluded.expires_ts",
                (user_id, expires),
            )
            await db.commit()
        self._muted[user_id] = expires

    async def is_muted(self, user_id: int) -> bool:
        exp = self._muted.get(user_id, 0)
        if exp and time.time() > exp:
            await self.unmute_user(user_id)
            return False
        return bool(exp)

    async def unmute_user(self, user_id: int):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("DELETE FROM bot_mutes WHERE user_id=?", (user_id,))
            await db.commit()
        self._muted.pop(user_id, None)

    # ── Trivia ────────────────────────────────────────────────────────────────
    async def update_trivia(self, user_id: int, correct: bool):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("SELECT id FROM trivia WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone()
            if row:
                col = "correct" if correct else "wrong"
                await db.execute(f"UPDATE trivia SET {col}={col}+1 WHERE user_id=?", (user_id,))
            else:
                await db.execute("INSERT INTO trivia (user_id,correct,wrong) VALUES (?,?,?)",
                                 (user_id, 1 if correct else 0, 0 if correct else 1))
            await db.commit()

    async def set_active_trivia(self, channel_id: int, asker_id: int, question: str, answer: str, source_note: str = ""):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute(
                "INSERT INTO active_trivia (channel_id,asker_id,question,answer,source_note,asked_ts) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(channel_id) DO UPDATE SET asker_id=excluded.asker_id, question=excluded.question, "
                "answer=excluded.answer, source_note=excluded.source_note, asked_ts=excluded.asked_ts",
                (channel_id, asker_id, question[:500], answer[:240], source_note[:240], time.time()),
            )
            await db.commit()

    async def get_active_trivia(self, channel_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute(
                "SELECT asker_id,question,answer,source_note,asked_ts FROM active_trivia WHERE channel_id=?",
                (channel_id,),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        return {
            "asker_id": row[0],
            "question": row[1] or "",
            "answer": row[2] or "",
            "source_note": row[3] or "",
            "asked_ts": row[4] or 0,
        }

    async def clear_active_trivia(self, channel_id: int):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("DELETE FROM active_trivia WHERE channel_id=?", (channel_id,))
            await db.commit()

    async def get_trivia_stats(self, user_id: int) -> dict:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("SELECT correct,wrong FROM trivia WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone()
        correct = row[0] if row else 0
        wrong = row[1] if row else 0
        total = correct + wrong
        return {
            "correct": correct,
            "wrong": wrong,
            "total": total,
            "accuracy": round((correct / total) * 100, 1) if total else 0.0,
        }

    # ── Roast battles ─────────────────────────────────────────────────────────
    async def start_roast_battle(self, channel_id: int, u1: int, u2: int) -> int:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            cur = await db.execute(
                "INSERT INTO roast_battles (channel_id,user1_id,user2_id,round,scores,active,ts) VALUES (?,?,?,0,'{}',1,?)",
                (channel_id,u1,u2,time.time()))
            await db.commit()
            return cur.lastrowid

    async def get_active_roast(self, channel_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute(
                "SELECT id,user1_id,user2_id,round,scores FROM roast_battles WHERE channel_id=? AND active=1", (channel_id,)
            ) as cur:
                row = await cur.fetchone()
                if not row: return None
                return {"id":row[0],"user1":row[1],"user2":row[2],"round":row[3],"scores":json.loads(row[4])}

    async def end_roast_battle(self, battle_id: int):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("UPDATE roast_battles SET active=0 WHERE id=?", (battle_id,))
            await db.commit()

    async def increment_roast_round(self, battle_id: int):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("UPDATE roast_battles SET round=round+1 WHERE id=?", (battle_id,))
            await db.commit()

    async def award_roast_round(self, battle_id: int, winner_user_id: int):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("SELECT scores FROM roast_battles WHERE id=?", (battle_id,)) as cur:
                row = await cur.fetchone()
            scores = json.loads(row[0] or "{}") if row else {}
            key = str(winner_user_id)
            scores[key] = int(scores.get(key, 0)) + 1
            await db.execute(
                "UPDATE roast_battles SET scores=? WHERE id=?",
                (json.dumps(scores, ensure_ascii=True), battle_id),
            )
            await db.commit()

    async def list_inside_jokes(self, user_id: int, limit: int = 6) -> list[str]:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute(
                "SELECT joke FROM inside_jokes WHERE user_id=? ORDER BY ts DESC LIMIT ?",
                (user_id, limit),
            ) as cur:
                return [row[0] for row in await cur.fetchall() if row and row[0]]

    # ── Reminders ─────────────────────────────────────────────────────────────
    async def add_reminder(self, user_id: int, channel_id: int, reminder: str, due_ts: float):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("INSERT INTO reminders (user_id,channel_id,reminder,due_ts) VALUES (?,?,?,?)", (user_id,channel_id,reminder,due_ts))
            await db.commit()

    async def get_due_reminders(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("SELECT id,user_id,channel_id,reminder FROM reminders WHERE due_ts<=? AND done=0", (time.time(),)) as cur:
                rows = await cur.fetchall()
            if rows:
                await db.execute(f"UPDATE reminders SET done=1 WHERE id IN ({','.join('?'*len(rows))})", [r[0] for r in rows])
                await db.commit()
        return [{"id":r[0],"user_id":r[1],"channel_id":r[2],"reminder":r[3]} for r in rows]

    # ── Channels ──────────────────────────────────────────────────────────────
    async def track_channel(self, channel_id: int, guild_id: int):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("INSERT INTO channels (channel_id,guild_id,last_active) VALUES (?,?,?) ON CONFLICT(channel_id) DO UPDATE SET last_active=excluded.last_active", (channel_id,guild_id,time.time()))
            await db.commit()

    async def get_active_channels(self) -> list[tuple]:
        cutoff = time.time()-86400*3
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("SELECT channel_id,guild_id FROM channels WHERE last_active>?", (cutoff,)) as cur:
                return await cur.fetchall()

    # ── Cooldowns ─────────────────────────────────────────────────────────────
    async def can_proactive(self, channel_id: int, cooldown: int = 3600) -> bool:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("SELECT last_sent FROM proactive_cooldown WHERE channel_id=?", (channel_id,)) as cur:
                row = await cur.fetchone(); return not row or (time.time()-row[0])>=cooldown

    async def set_proactive_sent(self, channel_id: int):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("INSERT INTO proactive_cooldown (channel_id,last_sent) VALUES (?,?) ON CONFLICT(channel_id) DO UPDATE SET last_sent=excluded.last_sent", (channel_id,time.time()))
            await db.commit()

    async def can_lore_drop(self, channel_id: int, cooldown: int = 21600) -> bool:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("SELECT last_sent FROM lore_cooldown WHERE channel_id=?", (channel_id,)) as cur:
                row = await cur.fetchone(); return not row or (time.time()-row[0])>=cooldown

    async def set_lore_sent(self, channel_id: int):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("INSERT INTO lore_cooldown (channel_id,last_sent) VALUES (?,?) ON CONFLICT(channel_id) DO UPDATE SET last_sent=excluded.last_sent", (channel_id,time.time()))
            await db.commit()

    async def can_starter(self, channel_id: int, cooldown: int = 10800) -> bool:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("SELECT last_sent FROM starter_cooldown WHERE channel_id=?", (channel_id,)) as cur:
                row = await cur.fetchone(); return not row or (time.time()-row[0])>=cooldown

    async def set_starter_sent(self, channel_id: int):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("INSERT INTO starter_cooldown (channel_id,last_sent) VALUES (?,?) ON CONFLICT(channel_id) DO UPDATE SET last_sent=excluded.last_sent", (channel_id,time.time()))
            await db.commit()

    # ── Romance helpers ───────────────────────────────────────────────────────
    async def get_romance_users(self) -> list[int]:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("SELECT user_id FROM users WHERE romance_mode=1 AND proactive=1") as cur:
                return [r[0] for r in await cur.fetchall()]

    async def get_user_last_channel(self, user_id: int) -> int | None:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("SELECT channel_id FROM messages WHERE user_id=? AND (bot_name=? OR bot_name IS NULL) ORDER BY ts DESC LIMIT 1", (user_id,self.bot_name)) as cur:
                row = await cur.fetchone(); return row[0] if row else None

    # ── DM cooldown ───────────────────────────────────────────────────────────
    async def can_dm_user(self, user_id: int, cooldown: int = 7200) -> bool:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("SELECT last_sent FROM dm_cooldown WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone(); return not row or (time.time()-row[0])>=cooldown

    async def set_dm_sent(self, user_id: int):
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            await db.execute("INSERT INTO dm_cooldown (user_id,last_sent) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET last_sent=excluded.last_sent", (user_id,time.time()))
            await db.commit()

    async def get_dm_eligible_users(self) -> list[dict]:
        cutoff = time.time()-86400*7
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("""
                SELECT u.user_id,u.display_name,u.romance_mode,u.nsfw_mode
                FROM users u WHERE u.allow_dms=1 AND u.last_seen>?
                  AND EXISTS (SELECT 1 FROM messages m WHERE m.user_id=u.user_id AND (m.bot_name=? OR m.bot_name IS NULL))
            """, (cutoff,self.bot_name)) as cur:
                rows = await cur.fetchall()
        return [{"user_id":r[0],"display_name":r[1],"romance_mode":bool(r[2]),"nsfw_mode":bool(r[3])} for r in rows]

    async def get_proactive_candidates(self, *, absent_before: float, limit: int = 20) -> list[dict]:
        """Return public-channel candidates without coupling them to DM consent."""
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute(
                """SELECT u.user_id,u.display_name,u.affection,u.trust,u.last_active,u.proactive,
                          latest.channel_id,
                          COALESCE((
                              SELECT SUBSTR(public_message.content,1,180)
                              FROM messages public_message
                              WHERE public_message.user_id=u.user_id
                                AND public_message.channel_id=latest.channel_id
                                AND public_message.role='user'
                                AND (public_message.bot_name=? OR public_message.bot_name IS NULL)
                              ORDER BY public_message.ts DESC,public_message.id DESC LIMIT 1
                          ), '') AS public_context
                   FROM users u
                   JOIN messages latest ON latest.id=(
                       SELECT candidate.id FROM messages candidate
                       JOIN channels tracked ON tracked.channel_id=candidate.channel_id
                       WHERE candidate.user_id=u.user_id
                         AND (candidate.bot_name=? OR candidate.bot_name IS NULL)
                       ORDER BY candidate.ts DESC,candidate.id DESC LIMIT 1
                   )
                   WHERE u.proactive=1 AND u.last_active>0 AND u.last_active<? AND u.message_count>5
                   ORDER BY (u.affection + u.trust * 0.5) DESC,u.last_active ASC
                   LIMIT ?""",
                (self.bot_name, self.bot_name, absent_before, max(1, min(int(limit), 100))),
            ) as cur:
                rows = await cur.fetchall()
        return [
            {
                "user_id": row[0], "display_name": row[1], "affection": row[2] or 0,
                "trust": row[3] or 0, "last_active": row[4] or 0,
                "proactive": bool(row[5]), "channel_id": row[6],
                "context": (row[7] or "")[:180],
            }
            for row in rows
        ]

    # ── Stats ─────────────────────────────────────────────────────────────────
    async def get_stats(self, user_id: int) -> dict:
        async with aiosqlite.connect(self.db_path, timeout=15.0) as db:
            async with db.execute("""
                SELECT message_count,mood,affection,trust,first_seen,grudge_nick,
                       affection_nick,romance_mode,nsfw_mode,drift_score,slow_burn
                FROM users WHERE user_id=?
            """, (user_id,)) as cur:
                row = await cur.fetchone()
                if not row: return {}
            async with db.execute("SELECT COUNT(*) FROM inside_jokes WHERE user_id=?", (user_id,)) as cur:
                jokes = (await cur.fetchone())[0]
        return {
            "message_count":row[0] or 0, "mood":row[1] or 0, "affection":row[2] or 0,
            "trust":row[3] or 0, "first_seen":row[4] or 0, "grudge_nick":row[5],
            "affection_nick":row[6], "romance_mode":bool(row[7]), "nsfw_mode":bool(row[8]),
            "drift_score":row[9] or 0, "slow_burn":row[10] or 0, "joke_count":jokes,
        }
