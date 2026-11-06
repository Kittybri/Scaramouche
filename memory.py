"""
memory.py — Scaramouche Bot v6
Full persistence: mood, affection, trust, grudges, rivalry, milestones,
inside jokes, absence tracking, anniversaries, slow burn, personality drift,
memory summaries, muted users, contradiction tracking, name progression.
"""

import aiosqlite
import json
import os
import random
import time

# Use the Railway volume (or DATA_DIR) if available, otherwise the current directory.
_data_dir = os.getenv("DATA_DIR") or ("/data" if os.path.isdir("/data") else ".")
DB_PATH = os.path.join(_data_dir, "scaramouche.db")  # default only - every Memory instance has its own path
SHARED_DB_PATH = os.path.join(_data_dir, "shared_state.db")
SQLITE_TIMEOUT = 30  # seconds to wait on a locked database (two bot processes share one file)

# users.private_mask bits: this text came from a DM and must never surface in a server channel.
PRIVATE_CALLBACK = 1
PRIVATE_STATEMENT = 2
PRIVATE_CONFLICT = 4

_LOCAL_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_messages_user_chan_ts ON messages(user_id, channel_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_messages_chan_ts ON messages(channel_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_messages_user_role_ts ON messages(user_id, role, ts)",
    "CREATE INDEX IF NOT EXISTS idx_messages_role_ts ON messages(role, ts)",
    "CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(done, due_ts)",
    "CREATE INDEX IF NOT EXISTS idx_memory_bank_user ON memory_bank(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_inside_jokes_user ON inside_jokes(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_channels_guild ON channels(guild_id)",
)
_SHARED_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_banter_pair_ts ON bot_banter_log(pair_key, ts)",
    "CREATE INDEX IF NOT EXISTS idx_shared_jokes_user ON shared_inside_jokes(user_id)",
)


def _like(text: str) -> str:
    """Escape LIKE wildcards in user input so '%' and '_' match literally (use with ESCAPE '\\')."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class Memory:
    # In-memory only (resets on restart — intentional for mute)
    _muted: dict[int, float] = {}
    _muted_by: dict[int, int | None] = {}

    def __init__(self, bot_name: str = "scaramouche", data_dir: str | None = None):
        self.bot_name = (bot_name or "scaramouche").strip().lower()
        base = data_dir or _data_dir
        self.db_path = os.path.join(base, f"{self.bot_name}.db")
        self.shared_db_path = os.path.join(base, "shared_state.db")

    def _connect(self, path: str | None = None):
        """Open a connection (use with ``async with``). The generous timeout matters: two bots share one file."""
        return aiosqlite.connect(path or self.db_path, timeout=SQLITE_TIMEOUT)

    def _scope_db_path(self, scope: str) -> str:
        return self.shared_db_path if "::" in (scope or "") else self.db_path

    @staticmethod
    async def _ensure_columns(db, table: str, columns: dict[str, str]) -> None:
        """Add any missing column (table names are constants, never user input)."""
        async with db.execute(f"PRAGMA table_info({table})") as cur:
            existing = {row[1] for row in await cur.fetchall()}
        for name, ddl in columns.items():
            if name not in existing:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    async def init(self):
        async with self._connect() as db:
            await db.execute("PRAGMA journal_mode=WAL")
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
                    memory_summary_dm TEXT   DEFAULT NULL,
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
                    public_count INTEGER DEFAULT 0,
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
                CREATE TABLE IF NOT EXISTS hostages (
                    user_id  INTEGER PRIMARY KEY,
                    demand   TEXT,
                    ts       REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS channel_settings (
                    channel_id        INTEGER PRIMARY KEY,
                    proactive_enabled INTEGER DEFAULT 1,
                    updated_ts        REAL DEFAULT 0
                );
            """)
            async with db.execute("PRAGMA table_info(users)") as cur:
                had_private_summary = any(row[1] == "memory_summary_dm" for row in await cur.fetchall())
            await self._ensure_columns(db, "users", {
                "username":           "TEXT",
                "display_name":       "TEXT",
                "romance_mode":       "INTEGER DEFAULT 0",
                "nsfw_mode":          "INTEGER DEFAULT 0",
                "proactive":          "INTEGER DEFAULT 1",
                "last_seen":          "REAL DEFAULT 0",
                "allow_dms":          "INTEGER DEFAULT 1",
                "timezone_name":      "TEXT DEFAULT 'America/Los_Angeles'",
                "quiet_hours_start":  "INTEGER DEFAULT 23",
                "quiet_hours_end":    "INTEGER DEFAULT 8",
                "dm_frequency_hours": "INTEGER DEFAULT 8",
                "recent_activity_grace_minutes": "INTEGER DEFAULT 45",
                "mood":               "INTEGER DEFAULT 0",
                "affection":          "INTEGER DEFAULT 0",
                "trust":              "INTEGER DEFAULT 0",
                "rival_id":           "INTEGER DEFAULT NULL",
                "grudge_nick":        "TEXT DEFAULT NULL",
                "affection_nick":     "TEXT DEFAULT NULL",
                "message_count":      "INTEGER DEFAULT 0",
                "milestone_last":     "INTEGER DEFAULT 0",
                "first_seen":         "REAL DEFAULT 0",
                "last_active":        "REAL DEFAULT 0",
                "greeted_today":      "INTEGER DEFAULT 0",
                "anniversary_last":   "INTEGER DEFAULT 0",
                "slow_burn":          "INTEGER DEFAULT 0",
                "slow_burn_day":      "INTEGER DEFAULT 0",
                "slow_burn_fired":    "INTEGER DEFAULT 0",
                "drift_score":        "INTEGER DEFAULT 0",
                "memory_summary":     "TEXT DEFAULT NULL",
                "memory_summary_dm":  "TEXT DEFAULT NULL",
                "summary_msg_count":  "INTEGER DEFAULT 0",
                "last_statement":     "TEXT DEFAULT NULL",
                "style_profile":      "TEXT DEFAULT NULL",
                "emotional_arc":      "TEXT DEFAULT 'guarded'",
                "conflict_open":      "INTEGER DEFAULT 0",
                "conflict_summary":   "TEXT DEFAULT NULL",
                "last_conflict_ts":   "REAL DEFAULT 0",
                "repair_progress":    "INTEGER DEFAULT 0",
                "callback_memory":    "TEXT DEFAULT NULL",
                "callback_ts":        "REAL DEFAULT 0",
                "repair_count":       "INTEGER DEFAULT 0",
                "adult_confirmed":    "INTEGER DEFAULT 0",
                "private_mask":       "INTEGER DEFAULT 0",
                "greeted_day":        "INTEGER DEFAULT 0",
            })
            if not had_private_summary:
                # An old summary may have been written from DMs and nobody can tell which: keep it for DMs only and
                # let a fresh one (server messages only) be built for server channels.
                await db.execute(
                    "UPDATE users SET memory_summary_dm=memory_summary, memory_summary=NULL, summary_msg_count=0 "
                    "WHERE memory_summary IS NOT NULL"
                )
            # Topics learned before this column existed have an unknown origin, so they count as private (public_count 0).
            await self._ensure_columns(db, "user_topics", {"public_count": "INTEGER DEFAULT 0"})
            await self._ensure_columns(db, "messages", {"bot_name": "TEXT DEFAULT NULL"})
            await self._ensure_columns(db, "scene_state", {"important_prop": "TEXT DEFAULT NULL"})
            await self._ensure_columns(db, "reminders", {"dm": "INTEGER DEFAULT 0", "attempts": "INTEGER DEFAULT 0"})
            await self._ensure_columns(db, "memory_bank", {"private": "INTEGER DEFAULT 0"})
            await self._ensure_columns(db, "inside_jokes", {"private": "INTEGER DEFAULT 0"})
            await db.execute("UPDATE messages SET bot_name=? WHERE bot_name IS NULL", (self.bot_name,))
            # One trivia row per user. Older versions could create several when answers arrived together; fold them
            # into the first one so no score is lost, then let the database enforce it.
            await db.execute(
                "UPDATE trivia SET correct=(SELECT SUM(t.correct) FROM trivia t WHERE t.user_id=trivia.user_id), "
                "wrong=(SELECT SUM(t.wrong) FROM trivia t WHERE t.user_id=trivia.user_id) "
                "WHERE id IN (SELECT MIN(id) FROM trivia GROUP BY user_id HAVING COUNT(*)>1)"
            )
            await db.execute("DELETE FROM trivia WHERE id NOT IN (SELECT MIN(id) FROM trivia GROUP BY user_id)")
            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trivia_user ON trivia(user_id)")
            for statement in _LOCAL_INDEXES:
                await db.execute(statement)
            await db.commit()

        async with self._connect(self.shared_db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
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
            await self._ensure_columns(db, "duo_sessions", {
                "initiator_user_id":  "INTEGER DEFAULT 0",
                "awaiting_bot":       "TEXT DEFAULT NULL",
                "autoplay_remaining": "INTEGER DEFAULT 0",
                "next_autoplay_ts":   "REAL DEFAULT 0",
            })
            await self._ensure_columns(db, "shared_inside_jokes", {"private": "INTEGER DEFAULT 0"})
            for statement in _SHARED_INDEXES:
                await db.execute(statement)
            await db.commit()

    # ── Users ────────────────────────────────────────────────────────────────
    async def upsert_user(self, user_id: int, username: str, display_name: str):
        """Create the user or refresh their name / activity. One atomic statement per database: a user's first two
        messages can arrive together, and "look first, then insert" lets both decide to insert."""
        now = time.time()
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO users (user_id,username,display_name,last_seen,last_active,first_seen) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,display_name=excluded.display_name,"
                "last_seen=excluded.last_seen,last_active=excluded.last_active",
                (user_id, username, display_name, now, now, now),
            )
            await db.commit()
        async with self._connect(self.shared_db_path) as db:
            await db.execute(
                "INSERT INTO shared_users (user_id,username,display_name,first_seen,last_seen,last_active) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,display_name=excluded.display_name,"
                "last_seen=excluded.last_seen,last_active=excluded.last_active",
                (user_id, username, display_name, now, now, now),
            )
            await db.commit()

    async def get_user(self, user_id: int) -> dict | None:
        """Read-only: never creates rows (the old version wrote a preferences row on every read)."""
        async with self._connect() as db:
            async with db.execute("""
                SELECT user_id,username,display_name,romance_mode,nsfw_mode,proactive,allow_dms,
                       timezone_name,quiet_hours_start,quiet_hours_end,dm_frequency_hours,recent_activity_grace_minutes,
                       mood,affection,trust,rival_id,grudge_nick,affection_nick,message_count,
                       milestone_last,first_seen,last_seen,last_active,greeted_today,anniversary_last,
                       slow_burn,slow_burn_fired,drift_score,memory_summary,last_statement,
                       style_profile,emotional_arc,conflict_open,conflict_summary,last_conflict_ts,repair_progress,
                       callback_memory,callback_ts,repair_count,adult_confirmed,private_mask,memory_summary_dm
                FROM users WHERE user_id=?
            """, (user_id,)) as cur:
                row = await cur.fetchone()
            if not row:
                return None
            async with db.execute(
                "SELECT voice_enabled,utility_mode,duo_autoplay,rp_depth FROM user_preferences WHERE user_id=?",
                (user_id,),
            ) as cur:
                prefs_row = await cur.fetchone()
        try:
            style_profile = json.loads(row[30]) if row[30] else {}
        except (TypeError, ValueError):
            style_profile = {}
        if not isinstance(style_profile, dict):
            style_profile = {}
        mask = row[40] or 0
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
            "drift_score": row[27] or 0, "memory_summary": row[28], "memory_summary_dm": row[41],
            "last_statement": row[29],
            "style_profile": style_profile,
            "emotional_arc": row[31] or "guarded",
            "conflict_open": bool(row[32]),
            "conflict_summary": row[33],
            "last_conflict_ts": row[34] or 0,
            "repair_progress": row[35] or 0,
            "callback_memory": row[36],
            "callback_ts": row[37] or 0,
            "repair_count": row[38] or 0,
            "adult_confirmed": bool(row[39]),
            "callback_private": bool(mask & PRIVATE_CALLBACK),
            "last_statement_private": bool(mask & PRIVATE_STATEMENT),
            "conflict_private": bool(mask & PRIVATE_CONFLICT),
        }
        user.update(self._prefs_from_row(prefs_row))
        return user

    @staticmethod
    def _prefs_from_row(row) -> dict:
        return {
            "voice_enabled": bool(row[0]) if row else True,
            "utility_mode": bool(row[1]) if row else True,
            "duo_autoplay": bool(row[2]) if row else True,
            "rp_depth": (row[3] or "medium") if row else "medium",
        }

    async def get_user_preferences(self, user_id: int) -> dict:
        async with self._connect() as db:
            async with db.execute(
                "SELECT voice_enabled,utility_mode,duo_autoplay,rp_depth FROM user_preferences WHERE user_id=?",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()
        return self._prefs_from_row(row)

    async def set_user_preference(self, user_id: int, field: str, value):
        allowed = {"voice_enabled", "utility_mode", "duo_autoplay", "rp_depth"}
        if field not in allowed:
            raise ValueError(f"Unknown preference: {field}")
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO user_preferences (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
                (user_id,),
            )
            await db.execute(f"UPDATE user_preferences SET {field}=? WHERE user_id=?", (value, user_id))
            await db.commit()

    async def record_topic(self, user_id: int, topic: str, private: bool = False):
        """Count a topic. ``private`` (it came from a DM) bumps the total but not the public count."""
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO user_topics (user_id,topic,count,public_count,last_seen) VALUES (?,?,1,?,?) "
                "ON CONFLICT(user_id,topic) DO UPDATE SET count=count+1,public_count=public_count+excluded.public_count,"
                "last_seen=excluded.last_seen",
                (user_id, topic[:40], 0 if private else 1, time.time()),
            )
            await db.commit()

    async def get_top_topics(self, user_id: int, limit: int = 3, *, public_only: bool = False) -> list[dict]:
        """Most talked-about topics. ``public_only`` counts (and lists) only what was said in server channels: use it
        whenever the result will be seen by, or shape a reply to, other people."""
        column = "public_count" if public_only else "count"
        async with self._connect() as db:
            async with db.execute(
                f"SELECT topic,{column},last_seen FROM user_topics WHERE user_id=? AND {column}>0 "
                f"ORDER BY {column} DESC,last_seen DESC LIMIT ?",
                (user_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [{"topic": row[0], "count": row[1] or 0, "last_seen": row[2] or 0} for row in rows]

    async def add_shared_inside_joke(self, user_id: int, joke: str, source: str = "shared", private: bool = False):
        async with self._connect(self.shared_db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM shared_inside_jokes WHERE user_id=?", (user_id,)) as cur:
                count = (await cur.fetchone())[0]
            if count >= 12:
                await db.execute(
                    "DELETE FROM shared_inside_jokes WHERE id=(SELECT id FROM shared_inside_jokes WHERE user_id=? ORDER BY ts ASC LIMIT 1)",
                    (user_id,),
                )
            await db.execute(
                "INSERT INTO shared_inside_jokes (user_id,joke,source,ts,private) VALUES (?,?,?,?,?)",
                (user_id, joke[:220], source[:40], time.time(), 1 if private else 0),
            )
            await db.commit()

    async def get_random_shared_inside_joke(self, user_id: int, include_private: bool = True) -> str | None:
        query = "SELECT joke FROM shared_inside_jokes WHERE user_id=?"
        if not include_private:
            query += " AND COALESCE(private,0)=0"
        async with self._connect(self.shared_db_path) as db:
            async with db.execute(query + " ORDER BY RANDOM() LIMIT 1", (user_id,)) as cur:
                row = await cur.fetchone()
        return row[0] if row else None

    async def has_milestone(self, scope: str, marker: str) -> bool:
        async with self._connect(self._scope_db_path(scope)) as db:
            async with db.execute(
                "SELECT 1 FROM relationship_milestones WHERE scope=? AND marker=?",
                (scope, marker),
            ) as cur:
                return bool(await cur.fetchone())

    async def add_milestone(self, scope: str, marker: str, note: str):
        async with self._connect(self._scope_db_path(scope)) as db:
            await db.execute(
                "INSERT INTO relationship_milestones (scope,marker,note,ts) VALUES (?,?,?,?) "
                "ON CONFLICT(scope,marker) DO UPDATE SET note=excluded.note,ts=excluded.ts",
                (scope[:80], marker[:80], note[:220], time.time()),
            )
            await db.commit()

    async def get_recent_milestones(self, scope: str, limit: int = 3) -> list[str]:
        async with self._connect(self._scope_db_path(scope)) as db:
            async with db.execute(
                "SELECT note FROM relationship_milestones WHERE scope=? ORDER BY ts DESC LIMIT ?",
                (scope, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [row[0] for row in rows if row and row[0]]

    async def get_scene_state(self, channel_id: int) -> dict | None:
        async with self._connect() as db:
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
        async with self._connect() as db:
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
        async with self._connect() as db:
            await db.execute("DELETE FROM scene_state WHERE channel_id=?", (channel_id,))
            await db.commit()

    async def add_memory_event(self, user_id: int, kind: str, memory: str, weight: int = 1, private: bool = False):
        flag = 1 if private else 0
        async with self._connect() as db:
            async with db.execute(
                "SELECT id,weight FROM memory_bank WHERE user_id=? AND kind=? AND memory=?",
                (user_id, kind[:40], memory[:240]),
            ) as cur:
                row = await cur.fetchone()
            if row:
                # seen again: stronger, and public as soon as it has been said in public once
                await db.execute(
                    "UPDATE memory_bank SET weight=MIN(weight+?,10), ts=?, private=MIN(COALESCE(private,0),?) WHERE id=?",
                    (max(1, int(weight)), time.time(), flag, row[0]),
                )
            else:
                await db.execute(
                    "INSERT INTO memory_bank (user_id,kind,memory,weight,last_used,ts,private) VALUES (?,?,?,?,?,?,?)",
                    (user_id, kind[:40], memory[:240], max(1, int(weight)), 0, time.time(), flag),
                )
            await db.execute(
                "DELETE FROM memory_bank WHERE user_id=? AND id NOT IN (SELECT id FROM memory_bank WHERE user_id=? ORDER BY weight DESC, ts DESC LIMIT 16)",
                (user_id, user_id),
            )
            await db.commit()

    async def get_weighted_memory_event(self, user_id: int, include_private: bool = True) -> dict | None:
        """A remembered event, chosen at random with probability proportional to its weight."""
        query = "SELECT id,kind,memory,weight FROM memory_bank WHERE user_id=?"
        if not include_private:
            query += " AND COALESCE(private,0)=0"
        async with self._connect() as db:
            async with db.execute(query + " ORDER BY weight DESC, ts DESC LIMIT 12", (user_id,)) as cur:
                rows = await cur.fetchall()
            if not rows:
                return None
            chosen = random.choices(rows, weights=[max(1, r[3] or 1) for r in rows], k=1)[0]
            await db.execute("UPDATE memory_bank SET last_used=? WHERE id=?", (time.time(), chosen[0]))
            await db.commit()
        return {"kind": chosen[1] or "", "memory": chosen[2] or "", "weight": chosen[3] or 1}

    async def get_memory_bank_entries(self, user_id: int, limit: int = 8, include_private: bool = True) -> list[dict]:
        query = "SELECT kind,memory,weight,last_used,ts FROM memory_bank WHERE user_id=?"
        if not include_private:
            query += " AND COALESCE(private,0)=0"
        async with self._connect() as db:
            async with db.execute(query + " ORDER BY weight DESC, ts DESC LIMIT ?", (user_id, limit)) as cur:
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
        needle = (query or "").strip().lower()
        if not needle:
            return {"topics": 0, "jokes": 0, "shared_jokes": 0, "memories": 0, "callback": 0}

        like = _like(needle[:80])  # '%' and '_' in the query match literally
        async with self._connect() as db:
            topic_cur = await db.execute(
                "DELETE FROM user_topics WHERE user_id=? AND LOWER(topic) LIKE ? ESCAPE '\\'",
                (user_id, like),
            )
            joke_cur = await db.execute(
                "DELETE FROM inside_jokes WHERE user_id=? AND LOWER(joke) LIKE ? ESCAPE '\\'",
                (user_id, like),
            )
            memory_cur = await db.execute(
                "DELETE FROM memory_bank WHERE user_id=? AND (LOWER(kind) LIKE ? ESCAPE '\\' OR LOWER(memory) LIKE ? ESCAPE '\\')",
                (user_id, like, like),
            )
            callback_cur = await db.execute(
                "UPDATE users SET callback_memory=NULL, callback_ts=0 "
                "WHERE user_id=? AND callback_memory IS NOT NULL AND LOWER(callback_memory) LIKE ? ESCAPE '\\'",
                (user_id, like),
            )
            await db.commit()
        async with self._connect(self.shared_db_path) as db:
            shared_joke_cur = await db.execute(
                "DELETE FROM shared_inside_jokes WHERE user_id=? AND LOWER(joke) LIKE ? ESCAPE '\\'",
                (user_id, like),
            )
            await db.commit()
        return {
            "topics": int(topic_cur.rowcount or 0),
            "jokes": int(joke_cur.rowcount or 0),
            "shared_jokes": int(shared_joke_cur.rowcount or 0),
            "memories": int(memory_cur.rowcount or 0),
            "callback": int(callback_cur.rowcount or 0),
        }

    async def set_mode(self, user_id: int, field: str, value: bool):
        allowed = {"nsfw_mode","romance_mode","proactive","allow_dms"}
        if field not in allowed: raise ValueError(f"Unknown: {field}")
        async with self._connect() as db:
            await db.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (int(value), user_id))
            await db.commit()

    async def set_adult_confirmed(self, user_id: int, value: bool = True):
        """Record that the user attested to being 18+ (required before unfiltered mode can take effect)."""
        async with self._connect() as db:
            await db.execute("UPDATE users SET adult_confirmed=? WHERE user_id=?", (1 if value else 0, user_id))
            await db.commit()

    async def apply_deltas(self, user_id: int, *, mood: int = 0, affection: int = 0, trust: int = 0, drift: int = 0):
        """Apply several relationship changes in ONE statement (each is clamped to its range)."""
        if not any((mood, affection, trust, drift)):
            return
        async with self._connect() as db:
            await db.execute(
                "UPDATE users SET mood=MAX(-10,MIN(10,mood+?)), affection=MAX(0,MIN(100,affection+?)), "
                "trust=MAX(0,MIN(100,trust+?)), drift_score=MAX(0,MIN(100,drift_score+?)) WHERE user_id=?",
                (mood, affection, trust, drift, user_id),
            )
            await db.commit()

    async def set_timezone(self, user_id: int, timezone_name: str):
        async with self._connect() as db:
            await db.execute("UPDATE users SET timezone_name=? WHERE user_id=?", (timezone_name[:64], user_id))
            await db.commit()

    async def set_quiet_hours(self, user_id: int, start_hour: int, end_hour: int):
        async with self._connect() as db:
            await db.execute(
                "UPDATE users SET quiet_hours_start=?, quiet_hours_end=? WHERE user_id=?",
                (int(start_hour) % 24, int(end_hour) % 24, user_id),
            )
            await db.commit()

    async def set_dm_frequency(self, user_id: int, hours: int):
        async with self._connect() as db:
            await db.execute("UPDATE users SET dm_frequency_hours=? WHERE user_id=?", (max(1, min(72, int(hours))), user_id))
            await db.commit()

    async def set_recent_activity_grace(self, user_id: int, minutes: int):
        async with self._connect() as db:
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
            async with self._connect() as db:
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
        async with self._connect() as db:
            await db.execute("UPDATE users SET mood=MAX(-10,MIN(10,mood+?)) WHERE user_id=?", (delta, user_id))
            await db.commit()

    async def get_mood(self, user_id: int) -> int:
        async with self._connect() as db:
            async with db.execute("SELECT mood FROM users WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone(); return row[0] if row else 0

    async def random_mood_swing(self, user_id: int):
        """Randomly shift mood slightly — unpredictable personality."""
        delta = random.choice([-3, -2, -1, -1, 0, 0, 1, 2])
        await self.update_mood(user_id, delta)

    # ── Affection ─────────────────────────────────────────────────────────────
    async def update_affection(self, user_id: int, delta: int):
        async with self._connect() as db:
            await db.execute("UPDATE users SET affection=MAX(0,MIN(100,affection+?)) WHERE user_id=?", (delta, user_id))
            await db.commit()

    async def set_affection_nick(self, user_id: int, nick: str):
        async with self._connect() as db:
            await db.execute("UPDATE users SET affection_nick=? WHERE user_id=?", (nick, user_id))
            await db.commit()

    # ── Trust ─────────────────────────────────────────────────────────────────
    async def update_trust(self, user_id: int, delta: int):
        async with self._connect() as db:
            await db.execute("UPDATE users SET trust=MAX(0,MIN(100,trust+?)) WHERE user_id=?", (delta, user_id))
            await db.commit()

    # ── Grudge ────────────────────────────────────────────────────────────────
    async def set_grudge_nick(self, user_id: int, nick: str | None):
        async with self._connect() as db:
            await db.execute("UPDATE users SET grudge_nick=? WHERE user_id=?", (nick, user_id))
            await db.commit()

    # ── Rivalry ───────────────────────────────────────────────────────────────
    async def set_rival(self, user_id: int, rival_id: int | None):
        async with self._connect() as db:
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
        async with self._connect() as db:
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
            async with self._connect() as db:
                await db.execute("UPDATE users SET slow_burn_fired=1 WHERE user_id=?", (user_id,))
                await db.commit()
            return new_score, True
        return new_score, False

    async def reset_slow_burn_fired(self, user_id: int, clear_streak: bool = True):
        async with self._connect() as db:
            if clear_streak:
                await db.execute("UPDATE users SET slow_burn=0, slow_burn_fired=0 WHERE user_id=?", (user_id,))
            else:
                await db.execute("UPDATE users SET slow_burn_fired=0 WHERE user_id=?", (user_id,))
            await db.commit()

    # ── Personality drift ─────────────────────────────────────────────────────
    async def update_drift(self, user_id: int, delta: int):
        """Drift: 0=unchanged, 100=maximally drifted. Changes subtly over weeks."""
        async with self._connect() as db:
            await db.execute("UPDATE users SET drift_score=MAX(0,MIN(100,drift_score+?)) WHERE user_id=?", (delta, user_id))
            await db.commit()

    async def set_style_profile(self, user_id: int, profile: dict):
        # Only the known numeric traits are stored, so the JSON is always small and never truncated mid-string.
        clean = {str(k)[:24]: int(v) for k, v in (profile or {}).items() if isinstance(v, (int, float))}
        async with self._connect() as db:
            await db.execute(
                "UPDATE users SET style_profile=? WHERE user_id=?",
                (json.dumps(clean, ensure_ascii=True), user_id),
            )
            await db.commit()

    async def set_emotional_arc(self, user_id: int, arc: str):
        async with self._connect() as db:
            await db.execute("UPDATE users SET emotional_arc=? WHERE user_id=?", (arc[:40], user_id))
            await db.commit()

    async def open_conflict(self, user_id: int, summary: str, private: bool = False):
        async with self._connect() as db:
            await db.execute(
                "UPDATE users SET conflict_open=1, conflict_summary=?, last_conflict_ts=?, repair_progress=0, "
                "private_mask=(COALESCE(private_mask,0) & ?) | ? WHERE user_id=?",
                (summary[:300], time.time(), ~PRIVATE_CONFLICT & 0xFF, PRIVATE_CONFLICT if private else 0, user_id),
            )
            await db.commit()

    async def resolve_conflict(self, user_id: int):
        async with self._connect() as db:
            await db.execute(
                "UPDATE users SET conflict_open=0, conflict_summary=NULL, repair_progress=0, repair_count=repair_count+1, "
                "private_mask=COALESCE(private_mask,0) & ? WHERE user_id=?",
                (~PRIVATE_CONFLICT & 0xFF, user_id),
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
        async with self._connect() as db:
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

    async def set_callback_memory(self, user_id: int, callback_memory: str, private: bool = False):
        async with self._connect() as db:
            await db.execute(
                "UPDATE users SET callback_memory=?, callback_ts=?, "
                "private_mask=(COALESCE(private_mask,0) & ?) | ? WHERE user_id=?",
                (callback_memory[:300], time.time(), ~PRIVATE_CALLBACK & 0xFF, PRIVATE_CALLBACK if private else 0, user_id),
            )
            await db.commit()

    async def consume_phrase(self, scope: str, phrase_key: str, cooldown_seconds: int) -> bool:
        now = time.time()
        async with self._connect() as db:
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
        async with self._connect() as db:
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

    async def phrase_status(self, scope: str, phrase_key: str, cooldown_seconds: int) -> tuple[bool, int]:
        """Read-only cooldown check: (ready, seconds_remaining). Does not start the cooldown."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT last_used FROM phrase_cooldowns WHERE scope=? AND phrase_key=?",
                (scope, phrase_key),
            ) as cur:
                row = await cur.fetchone()
        if row:
            elapsed = time.time() - (row[0] or 0)
            if elapsed < cooldown_seconds:
                return False, max(0, int(cooldown_seconds - elapsed))
        return True, 0

    async def get_bot_relationship(self, pair_key: str) -> dict:
        async with self._connect(self.shared_db_path) as db:
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
        async with self._connect(self.shared_db_path) as db:
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
        async with self._connect(self.shared_db_path) as db:
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
        async with self._connect(self.shared_db_path) as db:
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
        async with self._connect(self.shared_db_path) as db:
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
        async with self._connect(self.shared_db_path) as db:
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
        async with self._connect(self.shared_db_path) as db:
            await db.execute(
                "UPDATE duo_sessions SET last_speaker=?, awaiting_bot=?, autoplay_remaining=?, next_autoplay_ts=?, updated_ts=?, expires_ts=? WHERE channel_id=?",
                (speaker_bot[:40], awaiting_bot[:40], autoplay_remaining, next_autoplay_ts, now, now + ttl_seconds, channel_id),
            )
            await db.commit()

    async def get_due_duo_sessions(self, bot_name: str) -> list[dict]:
        now = time.time()
        async with self._connect(self.shared_db_path) as db:
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
        async with self._connect(self.shared_db_path) as db:
            await db.execute("DELETE FROM duo_sessions WHERE channel_id=?", (channel_id,))
            await db.commit()

    async def set_channel_speaker_mode(self, channel_id: int, speaker_mode: str):
        async with self._connect(self.shared_db_path) as db:
            await db.execute(
                "INSERT INTO channel_speaker_modes (channel_id,speaker_mode,updated_ts) VALUES (?,?,?) "
                "ON CONFLICT(channel_id) DO UPDATE SET speaker_mode=excluded.speaker_mode, updated_ts=excluded.updated_ts",
                (channel_id, speaker_mode[:20], time.time()),
            )
            await db.commit()

    async def get_channel_speaker_mode(self, channel_id: int) -> str:
        async with self._connect(self.shared_db_path) as db:
            async with db.execute("SELECT speaker_mode FROM channel_speaker_modes WHERE channel_id=?", (channel_id,)) as cur:
                row = await cur.fetchone()
        return (row[0] if row else "auto") or "auto"

    async def start_duo_story(self, channel_id: int, story_type: str, topic: str, enemy: str = "") -> int:
        now = time.time()
        async with self._connect(self.shared_db_path) as db:
            cur = await db.execute(
                "INSERT INTO duo_story_log (channel_id,story_type,topic,status,outcome,enemy,ts,updated_ts) VALUES (?,?,?,?,?,?,?,?)",
                (channel_id, story_type[:40], topic[:300], "open", "", enemy[:120], now, now),
            )
            await db.commit()
            return cur.lastrowid

    async def resolve_duo_story(self, channel_id: int, story_type: str, outcome: str):
        async with self._connect(self.shared_db_path) as db:
            await db.execute(
                "UPDATE duo_story_log SET status='resolved', outcome=?, updated_ts=? "
                "WHERE id=(SELECT id FROM duo_story_log WHERE channel_id=? AND story_type=? ORDER BY ts DESC LIMIT 1)",
                (outcome[:300], time.time(), channel_id, story_type[:40]),
            )
            await db.commit()

    async def get_recent_duo_stories(self, channel_id: int, limit: int = 5) -> list[dict]:
        async with self._connect(self.shared_db_path) as db:
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
        async with self._connect() as db:
            async with db.execute(
                "SELECT message_count, summary_msg_count FROM users WHERE user_id=?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
                if not row: return False
                count, last = row[0] or 0, row[1] or 0
                return count >= 500 and (count - last) >= 50

    async def save_summary(self, user_id: int, summary: str, private: bool = False):
        """Store a summary. The public one is written from server messages only; the private one (``private=True``)
        may draw on DMs and is only ever shown in DMs."""
        column = "memory_summary_dm" if private else "memory_summary"
        async with self._connect() as db:
            async with db.execute("SELECT message_count FROM users WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone(); count = row[0] if row else 0
            await db.execute(
                f"UPDATE users SET {column}=?, summary_msg_count=? WHERE user_id=?",
                (summary[:1000], count, user_id))
            await db.commit()

    # ── Contradiction tracking ────────────────────────────────────────────────
    async def update_last_statement(self, user_id: int, statement: str, private: bool = False):
        async with self._connect() as db:
            await db.execute(
                "UPDATE users SET last_statement=?, private_mask=(COALESCE(private_mask,0) & ?) | ? WHERE user_id=?",
                (statement[:300], ~PRIVATE_STATEMENT & 0xFF, PRIVATE_STATEMENT if private else 0, user_id),
            )
            await db.commit()

    # ── Message count & milestones ────────────────────────────────────────────
    async def increment_message_count(self, user_id: int) -> tuple[int, bool]:
        async with self._connect() as db:
            await db.execute("UPDATE users SET message_count=message_count+1 WHERE user_id=?", (user_id,))
            await db.commit()
            async with db.execute("SELECT message_count,milestone_last FROM users WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone()
                if not row: return 1, False
                count, last = row
        for m in [50,100,250,500,1000]:
            if count >= m and last < m:
                async with self._connect() as db:
                    await db.execute("UPDATE users SET milestone_last=? WHERE user_id=?", (m, user_id))
                    await db.commit()
                return count, True
        return count, False

    async def get_top_users(self, limit: int = 8) -> list[dict]:
        async with self._connect() as db:
            async with db.execute(
                "SELECT user_id,display_name,message_count FROM users ORDER BY message_count DESC LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
        return [{"user_id":r[0],"display_name":r[1],"message_count":r[2]} for r in rows]

    async def get_top_users_in_guild(self, guild_id: int, limit: int = 40) -> list[dict]:
        """Who talks to the bot most in the channels of one server, counted from that server's messages only.

        DMs and other servers never count, so the ranking cannot depend on (or reveal) activity elsewhere and does
        not run dry when a server's members are not among the bot's busiest users overall.
        """
        async with self._connect() as db:
            async with db.execute(
                "SELECT m.user_id, COUNT(*) AS n FROM messages m JOIN channels c ON c.channel_id=m.channel_id "
                "WHERE c.guild_id=? AND m.role='user' AND m.user_id>0 AND (m.bot_name=? OR m.bot_name IS NULL) "
                "GROUP BY m.user_id ORDER BY n DESC, m.user_id LIMIT ?",
                (guild_id, self.bot_name, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [{"user_id": r[0], "message_count": r[1]} for r in rows]

    async def get_recent_user_ids(self, days: float = 2.0, limit: int = 3) -> list[int]:
        """A few random users seen in the last ``days`` days (used for the occasional mood swing)."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT user_id FROM users WHERE last_seen>? ORDER BY RANDOM() LIMIT ?",
                (time.time() - days * 86400, limit),
            ) as cur:
                return [row[0] for row in await cur.fetchall()]

    # ── Greetings ─────────────────────────────────────────────────────────────
    async def should_greet(self, user_id: int, day_key: int | None = None) -> bool:
        """Greet once per *local* day. ``day_key`` is e.g. 20261105 in the user's own timezone."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT greeted_today,last_active,greeted_day FROM users WHERE user_id=?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return False
        recent = (time.time() - (row[1] or 0)) < 172800
        if day_key is None:
            return not row[0] and recent
        return (row[2] or 0) != day_key and recent

    async def mark_greeted(self, user_id: int, day_key: int | None = None):
        async with self._connect() as db:
            await db.execute(
                "UPDATE users SET greeted_today=1, greeted_day=COALESCE(?, greeted_day) WHERE user_id=?",
                (day_key, user_id),
            )
            await db.commit()

    async def reset_daily_greetings(self):
        async with self._connect() as db:
            await db.execute("UPDATE users SET greeted_today=0"); await db.commit()

    # ── Anniversary ───────────────────────────────────────────────────────────
    async def check_anniversary(self, user_id: int) -> bool:
        async with self._connect() as db:
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
        async with self._connect() as db:
            await db.execute("UPDATE users SET anniversary_last=? WHERE user_id=?", (time.time(), user_id))
            await db.commit()

    # ── Absence ───────────────────────────────────────────────────────────────
    async def get_absent_romance_users(self, days: float = 3.0) -> list[dict]:
        cutoff = time.time() - days*86400
        async with self._connect() as db:
            async with db.execute("""
                SELECT user_id,display_name,last_active,
                       (julianday('now')-julianday(datetime(last_active,'unixepoch'))) as days_gone
                FROM users WHERE romance_mode=1 AND allow_dms=1 AND proactive=1
                  AND last_active>0 AND last_active<? AND message_count>5
            """, (cutoff,)) as cur:
                rows = await cur.fetchall()
        return [{"user_id":r[0],"display_name":r[1],"last_active":r[2],"days_gone":round(r[3] or 0,1)} for r in rows]

    # ── Inside jokes ─────────────────────────────────────────────────────────
    async def add_inside_joke(self, user_id: int, joke: str, private: bool = False):
        async with self._connect() as db:
            async with db.execute("SELECT COUNT(*) FROM inside_jokes WHERE user_id=?", (user_id,)) as cur:
                count = (await cur.fetchone())[0]
            if count >= 10:
                await db.execute("DELETE FROM inside_jokes WHERE id=(SELECT id FROM inside_jokes WHERE user_id=? ORDER BY ts ASC LIMIT 1)", (user_id,))
            await db.execute(
                "INSERT INTO inside_jokes (user_id,joke,ts,private) VALUES (?,?,?,?)",
                (user_id, joke[:200], time.time(), 1 if private else 0),
            )
            await db.commit()

    async def get_random_inside_joke(self, user_id: int, include_private: bool = True) -> str | None:
        query = "SELECT joke FROM inside_jokes WHERE user_id=?"
        if not include_private:
            query += " AND COALESCE(private,0)=0"
        async with self._connect() as db:
            async with db.execute(query + " ORDER BY RANDOM() LIMIT 1", (user_id,)) as cur:
                row = await cur.fetchone()
        return row[0] if row else None

    async def get_joke_count(self, user_id: int) -> int:
        async with self._connect() as db:
            async with db.execute("SELECT COUNT(*) FROM inside_jokes WHERE user_id=?", (user_id,)) as cur:
                return (await cur.fetchone())[0]

    # ── Conversation history ──────────────────────────────────────────────────
    async def get_history(self, user_id: int, channel_id: int, limit: int = 200, max_chars: int | None = None) -> list[dict]:
        """Newest ``limit`` messages (oldest first). ``max_chars`` caps the total size so prompts stay small."""
        async with self._connect() as db:
            async with db.execute("""
                SELECT role,content FROM messages WHERE user_id=? AND channel_id=? AND (bot_name=? OR bot_name IS NULL)
                ORDER BY ts DESC LIMIT ?
            """, (user_id,channel_id,self.bot_name,limit)) as cur:
                rows = await cur.fetchall()
        if max_chars:
            kept, used = [], 0
            for role, content in rows:  # newest first
                used += len(content or "")
                if kept and used > max_chars:
                    break
                kept.append((role, content))
            rows = kept
        return [{"role":r[0],"content":r[1]} for r in reversed(rows)]

    async def get_random_old_message(self, user_id: int, public_only: bool = False) -> str | None:
        """A random older message by this user. ``public_only`` restricts it to server channels (never DMs)."""
        cutoff = time.time()-86400*2
        join = "JOIN channels c ON c.channel_id=m.channel_id" if public_only else ""
        async with self._connect() as db:
            async with db.execute(
                f"SELECT m.content FROM messages m {join} WHERE m.user_id=? AND m.role='user' "
                "AND (m.bot_name=? OR m.bot_name IS NULL) AND m.ts<? ORDER BY RANDOM() LIMIT 1",
                (user_id,self.bot_name,cutoff),
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else None

    async def get_recent_messages(
        self, user_id: int, limit: int = 10, *, guild_id: int | None = None, public_only: bool = False
    ) -> list[str]:
        """Recent messages by a user.

        ``guild_id`` limits them to that server's channels; ``public_only`` to any server channel. Either way
        DMs are excluded - use them whenever the result will be shown to someone else.
        """
        params: list = [user_id, self.bot_name]
        join = ""
        extra = ""
        if guild_id is not None or public_only:
            join = "JOIN channels c ON c.channel_id=m.channel_id"
            if guild_id is not None:
                extra = " AND c.guild_id=?"
                params.append(guild_id)
        params.append(limit)
        async with self._connect() as db:
            async with db.execute(
                f"SELECT m.content FROM messages m {join} WHERE m.user_id=? AND m.role='user' "
                f"AND (m.bot_name=? OR m.bot_name IS NULL){extra} ORDER BY m.ts DESC LIMIT ?",
                params,
            ) as cur:
                return [r[0] for r in await cur.fetchall()]

    async def get_channel_recent(self, channel_id: int, limit: int = 20) -> list[dict]:
        """Get recent messages from all users in a channel."""
        async with self._connect() as db:
            async with db.execute("""
                SELECT m.role, m.content, u.display_name FROM messages m
                LEFT JOIN users u ON m.user_id=u.user_id
                WHERE m.channel_id=? AND (m.bot_name=? OR m.bot_name IS NULL) ORDER BY m.ts DESC LIMIT ?
            """, (channel_id,self.bot_name,limit)) as cur:
                rows = await cur.fetchall()
        return [{"role":r[0],"content":r[1],"name":r[2] or "?"} for r in reversed(rows)]

    async def add_message(self, user_id: int, channel_id: int, role: str, content: str):
        async with self._connect() as db:
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

        async with self._connect() as db:
            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()
        return [row[0] for row in rows if row and row[0]]

    async def reset_user(self, user_id: int):
        """Forget everything remembered about a user (settings such as modes and DM preferences are kept)."""
        scope = f"{self.bot_name}:user:{user_id}"
        async with self._connect() as db:
            for statement in (
                "DELETE FROM messages WHERE user_id=?",
                "DELETE FROM inside_jokes WHERE user_id=?",
                "DELETE FROM user_topics WHERE user_id=?",
                "DELETE FROM memory_bank WHERE user_id=?",
                "DELETE FROM reminders WHERE user_id=?",
                "DELETE FROM trivia WHERE user_id=?",
                "DELETE FROM active_trivia WHERE asker_id=?",
                "DELETE FROM dm_cooldown WHERE user_id=?",
                "DELETE FROM hostages WHERE user_id=?",
                "DELETE FROM scene_state WHERE channel_id=?",  # a DM's scene is keyed by the user id
            ):
                await db.execute(statement, (user_id,))
            # exact scope match - the old LIKE 'prefix%' also wiped other users whose id starts with these digits
            await db.execute("DELETE FROM relationship_milestones WHERE scope=?", (scope,))
            await db.execute("DELETE FROM phrase_cooldowns WHERE scope=?", (scope,))
            await db.execute("""UPDATE users SET mood=0,affection=0,trust=0,rival_id=NULL,grudge_nick=NULL,
                affection_nick=NULL,message_count=0,milestone_last=0,slow_burn=0,slow_burn_fired=0,
                drift_score=0,memory_summary=NULL,memory_summary_dm=NULL,summary_msg_count=0,last_statement=NULL,style_profile=NULL,
                emotional_arc='guarded',conflict_open=0,conflict_summary=NULL,last_conflict_ts=0,repair_progress=0,
                callback_memory=NULL,callback_ts=0,repair_count=0,private_mask=0,anniversary_last=0
                WHERE user_id=?""", (user_id,))
            await db.commit()
        async with self._connect(self.shared_db_path) as db:
            await db.execute("DELETE FROM shared_inside_jokes WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM relationship_milestones WHERE scope=? OR scope LIKE ?", (f"{self.bot_name}::user:{user_id}", f"%user:{user_id}"))
            await db.commit()

    # ── Mute (in-memory) ──────────────────────────────────────────────────────
    def mute_user(self, user_id: int, seconds: int = 600, by: int | None = None):
        Memory._muted[user_id] = time.time() + seconds
        Memory._muted_by[user_id] = by

    def is_muted(self, user_id: int) -> bool:
        exp = Memory._muted.get(user_id, 0)
        if exp and time.time() > exp:
            Memory._muted.pop(user_id, None)
            Memory._muted_by.pop(user_id, None)
            return False
        return bool(exp)

    def muted_by(self, user_id: int) -> int | None:
        """Who muted this user (None if nobody is muting them, or the source is unknown)."""
        return Memory._muted_by.get(user_id) if self.is_muted(user_id) else None

    def unmute_user(self, user_id: int):
        Memory._muted.pop(user_id, None)
        Memory._muted_by.pop(user_id, None)

    # ── Trivia ────────────────────────────────────────────────────────────────
    async def update_trivia(self, user_id: int, correct: bool):
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO trivia (user_id,correct,wrong) VALUES (?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET correct=correct+excluded.correct,wrong=wrong+excluded.wrong",
                (user_id, 1 if correct else 0, 0 if correct else 1),
            )
            await db.commit()

    async def set_active_trivia(self, channel_id: int, asker_id: int, question: str, answer: str, source_note: str = ""):
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO active_trivia (channel_id,asker_id,question,answer,source_note,asked_ts) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(channel_id) DO UPDATE SET asker_id=excluded.asker_id, question=excluded.question, "
                "answer=excluded.answer, source_note=excluded.source_note, asked_ts=excluded.asked_ts",
                (channel_id, asker_id, question[:500], answer[:240], source_note[:240], time.time()),
            )
            await db.commit()

    async def get_active_trivia(self, channel_id: int) -> dict | None:
        async with self._connect() as db:
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
        async with self._connect() as db:
            await db.execute("DELETE FROM active_trivia WHERE channel_id=?", (channel_id,))
            await db.commit()

    async def get_trivia_stats(self, user_id: int) -> dict:
        async with self._connect() as db:
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

    # ── Hostages (persisted so a restart does not forgive them) ──────────────
    async def set_hostage(self, user_id: int, demand: str):
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO hostages (user_id,demand,ts) VALUES (?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET demand=excluded.demand, ts=excluded.ts",
                (user_id, demand[:400], time.time()),
            )
            await db.commit()

    async def get_hostage(self, user_id: int) -> str | None:
        async with self._connect() as db:
            async with db.execute("SELECT demand FROM hostages WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone()
        return row[0] if row else None

    async def clear_hostage(self, user_id: int):
        async with self._connect() as db:
            await db.execute("DELETE FROM hostages WHERE user_id=?", (user_id,))
            await db.commit()

    # ── Maintenance ──────────────────────────────────────────────────────────
    async def prune_old_data(self, message_retention_days: int = 0) -> dict:
        """Housekeeping. Messages are only deleted when ``message_retention_days`` > 0 (default: keep forever)."""
        now = time.time()
        result = {"messages": 0, "phrase_cooldowns": 0, "reminders": 0, "trivia": 0}
        async with self._connect() as db:
            if message_retention_days and message_retention_days > 0:
                cur = await db.execute("DELETE FROM messages WHERE ts<?", (now - message_retention_days * 86400,))
                result["messages"] = max(0, cur.rowcount or 0)
            cur = await db.execute("DELETE FROM phrase_cooldowns WHERE last_used<?", (now - 60 * 86400,))
            result["phrase_cooldowns"] = max(0, cur.rowcount or 0)
            cur = await db.execute("DELETE FROM reminders WHERE done=1 AND due_ts<?", (now - 30 * 86400,))
            result["reminders"] = max(0, cur.rowcount or 0)
            cur = await db.execute("DELETE FROM active_trivia WHERE asked_ts<?", (now - 86400,))
            result["trivia"] = max(0, cur.rowcount or 0)
            await db.commit()
        return result

    # ── Roast battles ─────────────────────────────────────────────────────────
    async def start_roast_battle(self, channel_id: int, u1: int, u2: int) -> int:
        async with self._connect() as db:
            cur = await db.execute(
                "INSERT INTO roast_battles (channel_id,user1_id,user2_id,round,scores,active,ts) VALUES (?,?,?,0,'{}',1,?)",
                (channel_id,u1,u2,time.time()))
            await db.commit()
            return cur.lastrowid

    async def get_active_roast(self, channel_id: int) -> dict | None:
        async with self._connect() as db:
            async with db.execute(
                "SELECT id,user1_id,user2_id,round,scores FROM roast_battles WHERE channel_id=? AND active=1", (channel_id,)
            ) as cur:
                row = await cur.fetchone()
                if not row: return None
                return {"id":row[0],"user1":row[1],"user2":row[2],"round":row[3],"scores":json.loads(row[4])}

    async def end_roast_battle(self, battle_id: int):
        async with self._connect() as db:
            await db.execute("UPDATE roast_battles SET active=0 WHERE id=?", (battle_id,))
            await db.commit()

    async def increment_roast_round(self, battle_id: int):
        async with self._connect() as db:
            await db.execute("UPDATE roast_battles SET round=round+1 WHERE id=?", (battle_id,))
            await db.commit()

    async def award_roast_round(self, battle_id: int, winner_user_id: int):
        async with self._connect() as db:
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

    async def list_inside_jokes(self, user_id: int, limit: int = 6, include_private: bool = True) -> list[str]:
        query = "SELECT joke FROM inside_jokes WHERE user_id=?"
        if not include_private:
            query += " AND COALESCE(private,0)=0"
        async with self._connect() as db:
            async with db.execute(query + " ORDER BY ts DESC LIMIT ?", (user_id, limit)) as cur:
                return [row[0] for row in await cur.fetchall() if row and row[0]]

    # ── Reminders ─────────────────────────────────────────────────────────────
    async def add_reminder(self, user_id: int, channel_id: int, reminder: str, due_ts: float, dm: bool = False):
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO reminders (user_id,channel_id,reminder,due_ts,dm) VALUES (?,?,?,?,?)",
                (user_id, channel_id, reminder, due_ts, 1 if dm else 0),
            )
            await db.commit()

    async def get_due_reminders(self, limit: int = 20) -> list[dict]:
        """Reminders that are due. They stay pending until ``complete_reminder`` - a failed send is never lost."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT id,user_id,channel_id,reminder,dm,attempts FROM reminders "
                "WHERE done=0 AND due_ts<=? ORDER BY due_ts LIMIT ?",
                (time.time(), limit),
            ) as cur:
                rows = await cur.fetchall()
        return [
            {"id": r[0], "user_id": r[1], "channel_id": r[2], "reminder": r[3], "dm": bool(r[4]), "attempts": r[5] or 0}
            for r in rows
        ]

    async def complete_reminder(self, reminder_id: int):
        async with self._connect() as db:
            await db.execute("UPDATE reminders SET done=1 WHERE id=?", (reminder_id,))
            await db.commit()

    async def retry_reminder_later(self, reminder_id: int, delay_seconds: float = 90, max_attempts: int = 5) -> bool:
        """Push a failed reminder back. Returns True when it has been given up on (too many attempts)."""
        async with self._connect() as db:
            await db.execute(
                "UPDATE reminders SET attempts=attempts+1, due_ts=? WHERE id=?",
                (time.time() + delay_seconds, reminder_id),
            )
            async with db.execute("SELECT attempts FROM reminders WHERE id=?", (reminder_id,)) as cur:
                row = await cur.fetchone()
            gave_up = bool(row and (row[0] or 0) >= max_attempts)
            if gave_up:
                await db.execute("UPDATE reminders SET done=1 WHERE id=?", (reminder_id,))
            await db.commit()
        return gave_up

    # ── Channels ──────────────────────────────────────────────────────────────
    async def track_channel(self, channel_id: int, guild_id: int):
        async with self._connect() as db:
            await db.execute("INSERT INTO channels (channel_id,guild_id,last_active) VALUES (?,?,?) ON CONFLICT(channel_id) DO UPDATE SET last_active=excluded.last_active", (channel_id,guild_id,time.time()))
            await db.commit()

    async def get_active_channels(self) -> list[tuple]:
        """Channels seen recently where unprompted posts are allowed (an admin can switch a channel off)."""
        cutoff = time.time()-86400*3
        async with self._connect() as db:
            async with db.execute(
                "SELECT channel_id,guild_id FROM channels WHERE last_active>? AND channel_id NOT IN "
                "(SELECT channel_id FROM channel_settings WHERE proactive_enabled=0)",
                (cutoff,),
            ) as cur:
                return await cur.fetchall()

    async def set_channel_proactive(self, channel_id: int, enabled: bool):
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO channel_settings (channel_id,proactive_enabled,updated_ts) VALUES (?,?,?) "
                "ON CONFLICT(channel_id) DO UPDATE SET proactive_enabled=excluded.proactive_enabled, updated_ts=excluded.updated_ts",
                (channel_id, 1 if enabled else 0, time.time()),
            )
            await db.commit()

    async def channel_proactive_allowed(self, channel_id: int) -> bool:
        async with self._connect() as db:
            async with db.execute("SELECT proactive_enabled FROM channel_settings WHERE channel_id=?", (channel_id,)) as cur:
                row = await cur.fetchone()
        return bool(row[0]) if row else True

    # ── Cooldowns ─────────────────────────────────────────────────────────────
    async def can_proactive(self, channel_id: int, cooldown: int = 3600) -> bool:
        async with self._connect() as db:
            async with db.execute("SELECT last_sent FROM proactive_cooldown WHERE channel_id=?", (channel_id,)) as cur:
                row = await cur.fetchone(); return not row or (time.time()-row[0])>=cooldown

    async def set_proactive_sent(self, channel_id: int):
        async with self._connect() as db:
            await db.execute("INSERT INTO proactive_cooldown (channel_id,last_sent) VALUES (?,?) ON CONFLICT(channel_id) DO UPDATE SET last_sent=excluded.last_sent", (channel_id,time.time()))
            await db.commit()

    async def can_lore_drop(self, channel_id: int, cooldown: int = 21600) -> bool:
        async with self._connect() as db:
            async with db.execute("SELECT last_sent FROM lore_cooldown WHERE channel_id=?", (channel_id,)) as cur:
                row = await cur.fetchone(); return not row or (time.time()-row[0])>=cooldown

    async def set_lore_sent(self, channel_id: int):
        async with self._connect() as db:
            await db.execute("INSERT INTO lore_cooldown (channel_id,last_sent) VALUES (?,?) ON CONFLICT(channel_id) DO UPDATE SET last_sent=excluded.last_sent", (channel_id,time.time()))
            await db.commit()

    async def can_starter(self, channel_id: int, cooldown: int = 10800) -> bool:
        async with self._connect() as db:
            async with db.execute("SELECT last_sent FROM starter_cooldown WHERE channel_id=?", (channel_id,)) as cur:
                row = await cur.fetchone(); return not row or (time.time()-row[0])>=cooldown

    async def set_starter_sent(self, channel_id: int):
        async with self._connect() as db:
            await db.execute("INSERT INTO starter_cooldown (channel_id,last_sent) VALUES (?,?) ON CONFLICT(channel_id) DO UPDATE SET last_sent=excluded.last_sent", (channel_id,time.time()))
            await db.commit()

    # ── Romance helpers ───────────────────────────────────────────────────────
    async def get_romance_users(self) -> list[int]:
        async with self._connect() as db:
            async with db.execute("SELECT user_id FROM users WHERE romance_mode=1 AND proactive=1") as cur:
                return [r[0] for r in await cur.fetchall()]

    async def get_user_last_channel(self, user_id: int) -> int | None:
        async with self._connect() as db:
            async with db.execute("SELECT channel_id FROM messages WHERE user_id=? AND (bot_name=? OR bot_name IS NULL) ORDER BY ts DESC LIMIT 1", (user_id,self.bot_name)) as cur:
                row = await cur.fetchone(); return row[0] if row else None

    # ── DM cooldown ───────────────────────────────────────────────────────────
    async def can_dm_user(self, user_id: int, cooldown: int = 7200) -> bool:
        async with self._connect() as db:
            async with db.execute("SELECT last_sent FROM dm_cooldown WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone(); return not row or (time.time()-row[0])>=cooldown

    async def set_dm_sent(self, user_id: int):
        async with self._connect() as db:
            await db.execute("INSERT INTO dm_cooldown (user_id,last_sent) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET last_sent=excluded.last_sent", (user_id,time.time()))
            await db.commit()

    async def get_dm_eligible_users(self) -> list[dict]:
        """Users who may receive an unprompted DM: DMs on, proactive messages on, active in the last 7 days.

        ``nsfw_mode`` is the *effective* value: it also requires the user's 18+ confirmation.
        """
        cutoff = time.time()-86400*7
        async with self._connect() as db:
            async with db.execute("""
                SELECT u.user_id,u.display_name,u.romance_mode,(u.nsfw_mode AND u.adult_confirmed)
                FROM users u WHERE u.allow_dms=1 AND u.proactive=1 AND u.last_seen>?
                  AND EXISTS (SELECT 1 FROM messages m WHERE m.user_id=u.user_id AND (m.bot_name=? OR m.bot_name IS NULL))
            """, (cutoff,self.bot_name)) as cur:
                rows = await cur.fetchall()
        return [{"user_id":r[0],"display_name":r[1],"romance_mode":bool(r[2]),"nsfw_mode":bool(r[3])} for r in rows]

    # ── Stats ─────────────────────────────────────────────────────────────────
    async def get_stats(self, user_id: int) -> dict:
        async with self._connect() as db:
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
