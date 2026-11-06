"""
memory.py — Scaramouche Bot v6
Full persistence: mood, affection, trust, grudges, rivalry, milestones,
inside jokes, absence tracking, anniversaries, slow burn, personality drift,
memory summaries, muted users, contradiction tracking, name progression.
"""

from __future__ import annotations

import aiosqlite
import asyncio
import random  # mood swings used to rely on bot.py patching this module (memory.random = random)
import time
import json
import os
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo


_ORIGINAL_AIOSQLITE_CONNECT = aiosqlite.connect
SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "10000") or "10000")
SQLITE_CONNECT_TIMEOUT_S = float(os.getenv("SQLITE_CONNECT_TIMEOUT_S", "30") or "30")


async def _configure_sqlite_connection(conn) -> None:
    try:
        await conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA temp_store=MEMORY")
    except Exception as exc:
        print(f"[DB] SQLite pragma setup skipped: {type(exc).__name__}: {exc}")


class _HardenedAioSqliteConnection:
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("timeout", SQLITE_CONNECT_TIMEOUT_S)
        self._raw = _ORIGINAL_AIOSQLITE_CONNECT(*args, **kwargs)
        self._conn = None

    async def _ready(self):
        if self._conn is None:
            self._conn = await self._raw
            await _configure_sqlite_connection(self._conn)
        return self._conn

    def __await__(self):
        return self._ready().__await__()

    async def __aenter__(self):
        return await self._ready()

    async def __aexit__(self, exc_type, exc, tb):
        if self._conn is not None:
            await self._conn.close()


def _connect_hardened(*args, **kwargs):
    return _HardenedAioSqliteConnection(*args, **kwargs)


aiosqlite.connect = _connect_hardened


def _message_milestone_for_count(count: int) -> int:
    count = max(0, int(count or 0))
    milestone = 0
    for marker in (50, 100, 250, 500, 1000):
        if count >= marker:
            milestone = marker
    return milestone

def _resolve_data_dir() -> str:
    preferred = (os.getenv("MEMORY_DATA_DIR") or os.getenv("BOT_DATA_DIR") or "").strip()
    if preferred:
        preferred = os.path.abspath(os.path.expanduser(preferred))
        try:
            os.makedirs(preferred, exist_ok=True)
            return preferred
        except OSError:
            pass
    if os.path.isdir("/data"):
        return "/data"
    return "."


_data_dir = _resolve_data_dir()
DB_PATH = os.path.join(_data_dir, "scaramouche.db")
SHARED_DB_PATH = os.path.join(_data_dir, "shared_state.db")


class Memory:
    # Cache mute state in memory, but persist it in sqlite so restarts keep working.
    _muted: dict[int, float] = {}
    db_path: str = DB_PATH
    shared_db_path: str = SHARED_DB_PATH

    def __init__(self, bot_name: str = "scaramouche"):
        global DB_PATH
        self.bot_name = (bot_name or "scaramouche").strip().lower()
        self.db_path = os.path.join(_data_dir, f"{self.bot_name}.db")
        self.shared_db_path = SHARED_DB_PATH
        DB_PATH = self.db_path

    def _scope_db_path(self, scope: str) -> str:
        return self.shared_db_path if "::" in (scope or "") else self.db_path

    async def backup_now(self, label: str | None = None) -> list[str]:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_label = re.sub(r"[^a-zA-Z0-9_.-]+", "-", (label or "auto").strip())[:32] or "auto"
        backup_dir = os.path.join(_data_dir, "backups")
        os.makedirs(backup_dir, exist_ok=True)
        copied: list[str] = []

        def _backup_one(src: str) -> str | None:
            if not src or not os.path.exists(src):
                return None
            base = os.path.splitext(os.path.basename(src))[0]
            dst = os.path.join(backup_dir, f"{base}-{safe_label}-{stamp}.db")
            source = sqlite3.connect(src, timeout=SQLITE_CONNECT_TIMEOUT_S)
            target = sqlite3.connect(dst, timeout=SQLITE_CONNECT_TIMEOUT_S)
            try:
                source.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
                with target:
                    source.backup(target)
                return dst
            finally:
                source.close()
                target.close()

        for path in dict.fromkeys([self.db_path, self.shared_db_path]):
            backed_up = await asyncio.to_thread(_backup_one, path)
            if backed_up:
                copied.append(backed_up)
        return copied

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
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
                    anger_level      INTEGER DEFAULT 0,
                    affection_ever_maxed INTEGER DEFAULT 0,
                    anger_repair_active INTEGER DEFAULT 0,
                    anger_repair_kind TEXT DEFAULT NULL,
                    anger_repair_instruction TEXT DEFAULT NULL,
                    anger_repair_requirement TEXT DEFAULT NULL,
                    anger_aftercare_messages INTEGER DEFAULT 0,
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
                    repair_count     INTEGER DEFAULT 0,
                    birth_month      INTEGER DEFAULT 0,
                    birth_day        INTEGER DEFAULT 0,
                    birth_year       INTEGER DEFAULT 0,
                    birthday_last_sent_year INTEGER DEFAULT 0
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
                CREATE TABLE IF NOT EXISTS doc_verifications (
                    doc_id      TEXT NOT NULL,
                    user_id     INTEGER NOT NULL,
                    verified_at REAL DEFAULT 0,
                    PRIMARY KEY (doc_id, user_id)
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
                CREATE TABLE IF NOT EXISTS muted_users (
                    user_id     INTEGER PRIMARY KEY,
                    expires_ts  REAL DEFAULT 0,
                    created_ts  REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS blocked_dm_users (
                    user_id     INTEGER PRIMARY KEY,
                    created_ts  REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS blocked_channels (
                    channel_id  INTEGER PRIMARY KEY,
                    created_ts  REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS blocked_users (
                    user_id     INTEGER PRIMARY KEY,
                    blocked_ts  REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS banned_channels (
                    channel_id  INTEGER PRIMARY KEY,
                    banned_ts   REAL DEFAULT 0
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
                CREATE TABLE IF NOT EXISTS game_scores (
                    user_id      INTEGER,
                    game_type    TEXT,
                    wins         INTEGER DEFAULT 0,
                    losses       INTEGER DEFAULT 0,
                    draws        INTEGER DEFAULT 0,
                    total_points INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, game_type)
                );
                CREATE TABLE IF NOT EXISTS rpg_medals (
                    user_id        INTEGER PRIMARY KEY,
                    completions    INTEGER DEFAULT 0,
                    best_points    INTEGER DEFAULT 0,
                    first_clear_ts REAL DEFAULT 0,
                    last_clear_ts  REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS rpg_state (
                    user_id        INTEGER PRIMARY KEY,
                    current_boss   INTEGER DEFAULT 0,
                    current_round  INTEGER DEFAULT 0,
                    boss_points    INTEGER DEFAULT 0,
                    total_points   INTEGER DEFAULT 0,
                    bosses_beaten  TEXT DEFAULT '[]',
                    scenario_data  TEXT DEFAULT '{}',
                    active         INTEGER DEFAULT 0,
                    last_updated   REAL DEFAULT 0,
                    world_type     TEXT DEFAULT '',
                    char_type      TEXT DEFAULT '',
                    element        TEXT DEFAULT ''
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
                CREATE TABLE IF NOT EXISTS consequence_marks (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER,
                    kind        TEXT,
                    summary     TEXT,
                    severity    INTEGER DEFAULT 1,
                    decay_days  REAL DEFAULT 5,
                    created_ts  REAL DEFAULT 0,
                    last_seen   REAL DEFAULT 0
                );
            """)
            migrations = [
                ("allow_dms",          "INTEGER DEFAULT 1"),
                ("timezone_name",      "TEXT DEFAULT 'America/Los_Angeles'"),
                ("quiet_hours_start",  "INTEGER DEFAULT 23"),
                ("quiet_hours_end",    "INTEGER DEFAULT 8"),
                ("dm_frequency_hours", "INTEGER DEFAULT 8"),
                ("recent_activity_grace_minutes", "INTEGER DEFAULT 45"),
                ("mood",               "INTEGER DEFAULT 0"),
                ("affection",          "INTEGER DEFAULT 0"),
                ("trust",              "INTEGER DEFAULT 0"),
                ("anger_level",        "INTEGER DEFAULT 0"),
                ("affection_ever_maxed", "INTEGER DEFAULT 0"),
                ("anger_repair_active", "INTEGER DEFAULT 0"),
                ("anger_repair_kind",  "TEXT DEFAULT NULL"),
                ("anger_repair_instruction", "TEXT DEFAULT NULL"),
                ("anger_repair_requirement", "TEXT DEFAULT NULL"),
                ("anger_aftercare_messages", "INTEGER DEFAULT 0"),
                ("rival_id",           "INTEGER DEFAULT NULL"),
                ("grudge_nick",        "TEXT DEFAULT NULL"),
                ("affection_nick",     "TEXT DEFAULT NULL"),
                ("message_count",      "INTEGER DEFAULT 0"),
                ("milestone_last",     "INTEGER DEFAULT 0"),
                ("first_seen",         "REAL DEFAULT 0"),
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
                ("birth_month",        "INTEGER DEFAULT 0"),
                ("birth_day",          "INTEGER DEFAULT 0"),
                ("birth_year",         "INTEGER DEFAULT 0"),
                ("birthday_last_sent_year", "INTEGER DEFAULT 0"),
                ("adult_confirmed",    "INTEGER DEFAULT 0"),  # 18+ confirmation required for !nsfw
            ]
            for col, default in migrations:
                try:
                    await db.execute(f"ALTER TABLE users ADD COLUMN {col} {default}")
                except Exception:
                    pass
            # Game and RPG migrations for databases created by older bot versions.
            for stmt in (
                "ALTER TABLE roast_battles ADD COLUMN turn_user INTEGER DEFAULT 0",
                "CREATE TABLE IF NOT EXISTS game_scores (user_id INTEGER, game_type TEXT, wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0, draws INTEGER DEFAULT 0, total_points INTEGER DEFAULT 0, PRIMARY KEY (user_id, game_type))",
                "ALTER TABLE rpg_state ADD COLUMN world_type TEXT DEFAULT ''",
                "ALTER TABLE rpg_state ADD COLUMN char_type TEXT DEFAULT ''",
                "ALTER TABLE rpg_state ADD COLUMN element TEXT DEFAULT ''",
            ):
                try:
                    await db.execute(stmt)
                except Exception:
                    pass
            await db.commit()

        async with aiosqlite.connect(self.shared_db_path) as db:
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
                CREATE TABLE IF NOT EXISTS user_bot_attention (
                    user_id          INTEGER,
                    bot_name         TEXT,
                    score            INTEGER DEFAULT 0,
                    direct_score     INTEGER DEFAULT 0,
                    mention_count    INTEGER DEFAULT 0,
                    last_topic       TEXT DEFAULT NULL,
                    last_interaction REAL DEFAULT 0,
                    PRIMARY KEY (user_id, bot_name)
                );
                CREATE TABLE IF NOT EXISTS hidden_achievements (
                    scope           TEXT,
                    achievement_key TEXT,
                    note            TEXT DEFAULT NULL,
                    unlocked_ts     REAL DEFAULT 0,
                    PRIMARY KEY (scope, achievement_key)
                );
                CREATE TABLE IF NOT EXISTS shared_cooldowns (
                    scope      TEXT PRIMARY KEY,
                    last_used  REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS shared_world_entities (
                    entity_key    TEXT PRIMARY KEY,
                    entity_type   TEXT,
                    name          TEXT,
                    summary       TEXT DEFAULT NULL,
                    status        TEXT DEFAULT NULL,
                    channel_id    INTEGER DEFAULT 0,
                    owner_user_id INTEGER DEFAULT 0,
                    updated_by    TEXT DEFAULT NULL,
                    ts            REAL DEFAULT 0,
                    updated_ts    REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS shared_world_cases (
                    case_key      TEXT PRIMARY KEY,
                    channel_id    INTEGER DEFAULT 0,
                    case_type     TEXT,
                    title         TEXT,
                    status        TEXT DEFAULT 'open',
                    summary       TEXT DEFAULT NULL,
                    enemy         TEXT DEFAULT NULL,
                    updated_by    TEXT DEFAULT NULL,
                    opened_ts     REAL DEFAULT 0,
                    updated_ts    REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS face_profiles (
                    profile_key   TEXT PRIMARY KEY,
                    owner_user_id INTEGER DEFAULT 0,
                    label         TEXT DEFAULT NULL,
                    profile_json  TEXT DEFAULT NULL,
                    updated_ts    REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS shared_event_memories (
                    event_key        TEXT PRIMARY KEY,
                    channel_id       INTEGER DEFAULT 0,
                    topic            TEXT DEFAULT NULL,
                    scaramouche_memory TEXT DEFAULT NULL,
                    wanderer_memory  TEXT DEFAULT NULL,
                    truth_hint       TEXT DEFAULT NULL,
                    distorted_bot    TEXT DEFAULT NULL,
                    last_noticed_by  TEXT DEFAULT NULL,
                    created_ts       REAL DEFAULT 0,
                    updated_ts       REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS shared_evidence_locker (
                    evidence_key    TEXT PRIMARY KEY,
                    channel_id      INTEGER DEFAULT 0,
                    evidence_type   TEXT DEFAULT 'evidence',
                    label           TEXT DEFAULT NULL,
                    summary         TEXT DEFAULT NULL,
                    source_excerpt  TEXT DEFAULT NULL,
                    owner_user_id   INTEGER DEFAULT 0,
                    linked_case     TEXT DEFAULT NULL,
                    updated_by      TEXT DEFAULT NULL,
                    created_ts      REAL DEFAULT 0,
                    updated_ts      REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS interbot_private_opinions (
                    scope         TEXT,
                    bot_name      TEXT,
                    subject_type  TEXT,
                    subject_key   TEXT,
                    opinion       TEXT DEFAULT NULL,
                    intensity     INTEGER DEFAULT 1,
                    leaked_ts     REAL DEFAULT 0,
                    updated_ts    REAL DEFAULT 0,
                    PRIMARY KEY (scope, bot_name, subject_type, subject_key)
                );
            """)
            for stmt in (
                "ALTER TABLE duo_sessions ADD COLUMN initiator_user_id INTEGER DEFAULT 0",
                "ALTER TABLE duo_sessions ADD COLUMN awaiting_bot TEXT DEFAULT NULL",
                "ALTER TABLE duo_sessions ADD COLUMN autoplay_remaining INTEGER DEFAULT 0",
                "ALTER TABLE duo_sessions ADD COLUMN next_autoplay_ts REAL DEFAULT 0",
                "ALTER TABLE duo_sessions ADD COLUMN silent_bot TEXT DEFAULT NULL",
                "ALTER TABLE duo_sessions ADD COLUMN silent_until REAL DEFAULT 0",
                "ALTER TABLE duo_sessions ADD COLUMN intervention_cooldown REAL DEFAULT 0",
            ):
                try:
                    await db.execute(stmt)
                except Exception:
                    pass
            await db.commit()

        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute("ALTER TABLE messages ADD COLUMN bot_name TEXT DEFAULT NULL")
            except Exception:
                pass
            try:
                await db.execute("ALTER TABLE scene_state ADD COLUMN important_prop TEXT DEFAULT NULL")
            except Exception:
                pass
            await db.execute("UPDATE messages SET bot_name=? WHERE bot_name IS NULL", (self.bot_name,))
            await db.commit()

    # ── Users ────────────────────────────────────────────────────────────────
    async def upsert_user(self, user_id: int, username: str, display_name: str):
        now = time.time()
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT first_seen FROM users WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone()
            if row:
                await db.execute(
                    "UPDATE users SET username=?,display_name=?,last_seen=?,last_active=? WHERE user_id=?",
                    (username, display_name, now, now, user_id))
            else:
                await db.execute(
                    "INSERT INTO users (user_id,username,display_name,last_seen,last_active,first_seen) VALUES (?,?,?,?,?,?)",
                    (user_id, username, display_name, now, now, now))
            await db.commit()
        async with aiosqlite.connect(self.shared_db_path) as db:
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

    async def get_user(self, user_id: int) -> dict | None:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT user_id,username,display_name,romance_mode,nsfw_mode,proactive,allow_dms,
                       timezone_name,quiet_hours_start,quiet_hours_end,dm_frequency_hours,recent_activity_grace_minutes,
                       mood,affection,trust,anger_level,affection_ever_maxed,anger_repair_active,anger_repair_kind,
                       anger_repair_instruction,anger_repair_requirement,anger_aftercare_messages,rival_id,grudge_nick,affection_nick,message_count,
                       milestone_last,first_seen,last_seen,last_active,greeted_today,anniversary_last,
                       slow_burn,slow_burn_fired,drift_score,memory_summary,last_statement,
                       style_profile,emotional_arc,conflict_open,conflict_summary,last_conflict_ts,repair_progress,
                       callback_memory,callback_ts,repair_count,birth_month,birth_day,birth_year,birthday_last_sent_year,
                       adult_confirmed
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
                    "anger_level": row[15] or 0,
                    "affection_ever_maxed": bool(row[16]),
                    "anger_repair_active": bool(row[17]),
                    "anger_repair_kind": row[18] or "",
                    "anger_repair_instruction": row[19] or "",
                    "anger_repair_requirement": row[20] or "",
                    "anger_aftercare_messages": row[21] or 0,
                    "rival_id": row[22], "grudge_nick": row[23], "affection_nick": row[24],
                    "message_count": row[25] or 0, "milestone_last": row[26] or 0,
                    "first_seen": row[27] or 0, "last_seen": row[28] or 0,
                    "last_active": row[29] or 0, "greeted_today": bool(row[30]),
                    "anniversary_last": row[31] or 0,
                    "slow_burn": row[32] or 0, "slow_burn_fired": bool(row[33]),
                    "drift_score": row[34] or 0, "memory_summary": row[35],
                    "last_statement": row[36],
                    "style_profile": json.loads(row[37]) if row[37] else {},
                    "emotional_arc": row[38] or "guarded",
                    "conflict_open": bool(row[39]),
                    "conflict_summary": row[40],
                    "last_conflict_ts": row[41] or 0,
                    "repair_progress": row[42] or 0,
                    "callback_memory": row[43],
                    "callback_ts": row[44] or 0,
                    "repair_count": row[45] or 0,
                    "birth_month": row[46] or 0,
                    "birth_day": row[47] or 0,
                    "birth_year": row[48] or 0,
                    "birthday_last_sent_year": row[49] or 0,
                    "adult_confirmed": bool(row[50]),
                }
        prefs = await self.get_user_preferences(user_id)
        user.update(prefs)
        return user

    async def get_user_preferences(self, user_id: int) -> dict:
        async with aiosqlite.connect(DB_PATH) as db:
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
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO user_preferences (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
                (user_id,),
            )
            await db.execute(f"UPDATE user_preferences SET {field}=? WHERE user_id=?", (value, user_id))
            await db.commit()

    async def record_topic(self, user_id: int, topic: str):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO user_topics (user_id,topic,count,last_seen) VALUES (?,?,1,?) "
                "ON CONFLICT(user_id,topic) DO UPDATE SET count=count+1,last_seen=excluded.last_seen",
                (user_id, topic[:40], time.time()),
            )
            await db.commit()

    async def get_top_topics(self, user_id: int, limit: int = 3) -> list[dict]:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT topic,count,last_seen FROM user_topics WHERE user_id=? ORDER BY count DESC,last_seen DESC LIMIT ?",
                (user_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [{"topic": row[0], "count": row[1] or 0, "last_seen": row[2] or 0} for row in rows]

    async def add_shared_inside_joke(self, user_id: int, joke: str, source: str = "shared"):
        async with aiosqlite.connect(self.shared_db_path) as db:
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
        async with aiosqlite.connect(self.shared_db_path) as db:
            async with db.execute(
                "SELECT joke FROM shared_inside_jokes WHERE user_id=? ORDER BY RANDOM() LIMIT 1",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else None

    async def has_milestone(self, scope: str, marker: str) -> bool:
        async with aiosqlite.connect(self._scope_db_path(scope)) as db:
            async with db.execute(
                "SELECT 1 FROM relationship_milestones WHERE scope=? AND marker=?",
                (scope, marker),
            ) as cur:
                return bool(await cur.fetchone())

    async def add_milestone(self, scope: str, marker: str, note: str):
        async with aiosqlite.connect(self._scope_db_path(scope)) as db:
            await db.execute(
                "INSERT INTO relationship_milestones (scope,marker,note,ts) VALUES (?,?,?,?) "
                "ON CONFLICT(scope,marker) DO UPDATE SET note=excluded.note,ts=excluded.ts",
                (scope[:80], marker[:80], note[:220], time.time()),
            )
            await db.commit()

    async def get_recent_milestones(self, scope: str, limit: int = 3) -> list[str]:
        async with aiosqlite.connect(self._scope_db_path(scope)) as db:
            async with db.execute(
                "SELECT note FROM relationship_milestones WHERE scope=? ORDER BY ts DESC LIMIT ?",
                (scope, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [row[0] for row in rows if row and row[0]]

    async def get_scene_state(self, channel_id: int) -> dict | None:
        async with aiosqlite.connect(DB_PATH) as db:
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
        async with aiosqlite.connect(DB_PATH) as db:
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
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM scene_state WHERE channel_id=?", (channel_id,))
            await db.commit()

    async def add_memory_event(self, user_id: int, kind: str, memory: str, weight: int = 1):
        async with aiosqlite.connect(DB_PATH) as db:
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
        async with aiosqlite.connect(DB_PATH) as db:
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
        async with aiosqlite.connect(DB_PATH) as db:
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
        needle = (query or "").strip().lower()
        if not needle:
            return {"topics": 0, "jokes": 0, "shared_jokes": 0, "memories": 0, "callback": 0}

        like = f"%{needle[:80]}%"
        async with aiosqlite.connect(DB_PATH) as db:
            topic_cur = await db.execute(
                "DELETE FROM user_topics WHERE user_id=? AND LOWER(topic) LIKE ?",
                (user_id, like),
            )
            joke_cur = await db.execute(
                "DELETE FROM inside_jokes WHERE user_id=? AND LOWER(joke) LIKE ?",
                (user_id, like),
            )
            memory_cur = await db.execute(
                "DELETE FROM memory_bank WHERE user_id=? AND (LOWER(kind) LIKE ? OR LOWER(memory) LIKE ?)",
                (user_id, like, like),
            )
            callback_cur = await db.execute(
                "UPDATE users SET callback_memory=NULL, callback_ts=0 "
                "WHERE user_id=? AND callback_memory IS NOT NULL AND LOWER(callback_memory) LIKE ?",
                (user_id, like),
            )
            await db.commit()
        async with aiosqlite.connect(self.shared_db_path) as db:
            shared_joke_cur = await db.execute(
                "DELETE FROM shared_inside_jokes WHERE user_id=? AND LOWER(joke) LIKE ?",
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
        allowed = {"nsfw_mode","romance_mode","proactive","allow_dms","adult_confirmed"}
        if field not in allowed: raise ValueError(f"Unknown: {field}")
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (int(value), user_id))
            await db.commit()

    async def set_timezone(self, user_id: int, timezone_name: str):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET timezone_name=? WHERE user_id=?", (timezone_name[:64], user_id))
            await db.commit()

    async def set_quiet_hours(self, user_id: int, start_hour: int, end_hour: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET quiet_hours_start=?, quiet_hours_end=? WHERE user_id=?",
                (int(start_hour) % 24, int(end_hour) % 24, user_id),
            )
            await db.commit()

    async def set_dm_frequency(self, user_id: int, hours: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET dm_frequency_hours=? WHERE user_id=?", (max(1, min(72, int(hours))), user_id))
            await db.commit()

    async def set_recent_activity_grace(self, user_id: int, minutes: int):
        async with aiosqlite.connect(DB_PATH) as db:
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
            async with aiosqlite.connect(DB_PATH) as db:
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
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET mood=MAX(-10,MIN(10,mood+?)) WHERE user_id=?", (delta, user_id))
            await db.commit()

    async def get_mood(self, user_id: int) -> int:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT mood FROM users WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone(); return row[0] if row else 0

    async def random_mood_swing(self, user_id: int):
        """Randomly shift mood slightly — unpredictable personality."""
        delta = random.choice([-3, -2, -1, -1, 0, 0, 1, 2])
        await self.update_mood(user_id, delta)

    # ── Affection ─────────────────────────────────────────────────────────────
    async def update_affection(self, user_id: int, delta: int):
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT affection, affection_ever_maxed FROM users WHERE user_id=?",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()
            if not row:
                await db.execute("UPDATE users SET affection=MAX(0,MIN(100,affection+?)) WHERE user_id=?", (delta, user_id))
                await db.commit()
                return
            affection = int(row[0] or 0)
            ever_maxed = bool(row[1]) or affection >= 100 or (affection + delta) >= 100
            floor = 50 if self.bot_name == "scaramouche" and ever_maxed and delta < 0 else 0
            new_affection = max(floor, min(100, affection + delta))
            await db.execute(
                "UPDATE users SET affection=?, affection_ever_maxed=? WHERE user_id=?",
                (new_affection, int(ever_maxed or new_affection >= 100), user_id),
            )
            await db.commit()

    async def update_anger(self, user_id: int, delta: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET anger_level=MAX(0,MIN(100,anger_level+?)) WHERE user_id=?",
                (delta, user_id),
            )
            await db.commit()

    async def set_anger_repair(
        self,
        user_id: int,
        kind: str,
        instruction: str,
        requirement: str,
    ):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET anger_repair_active=1, anger_repair_kind=?, anger_repair_instruction=?, anger_repair_requirement=? WHERE user_id=?",
                (kind[:24], instruction[:800], requirement[:800], user_id),
            )
            await db.commit()

    async def clear_anger_repair(self, user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET anger_repair_active=0, anger_repair_kind=NULL, anger_repair_instruction=NULL, anger_repair_requirement=NULL WHERE user_id=?",
                (user_id,),
            )
            await db.commit()

    async def resolve_anger_repair(self, user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET anger_level=0, affection=50, anger_repair_active=0, anger_repair_kind=NULL, anger_repair_instruction=NULL, anger_repair_requirement=NULL, anger_aftercare_messages=3 WHERE user_id=?",
                (user_id,),
            )
            await db.commit()

    async def consume_anger_aftercare(self, user_id: int) -> int:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT anger_aftercare_messages FROM users WHERE user_id=?",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()
            remaining = int((row[0] if row else 0) or 0)
            if remaining > 0:
                remaining -= 1
                await db.execute(
                    "UPDATE users SET anger_aftercare_messages=? WHERE user_id=?",
                    (remaining, user_id),
                )
                await db.commit()
            return remaining

    async def set_affection_nick(self, user_id: int, nick: str):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET affection_nick=? WHERE user_id=?", (nick, user_id))
            await db.commit()

    async def get_used_nicknames(self, exclude_user_id: int | None = None) -> dict[str, list[str]]:
        async with aiosqlite.connect(DB_PATH) as db:
            if exclude_user_id is None:
                query = "SELECT affection_nick, grudge_nick FROM users"
                params: tuple = ()
            else:
                query = "SELECT affection_nick, grudge_nick FROM users WHERE user_id != ?"
                params = (exclude_user_id,)
            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()

        def _norm(value: str | None) -> str:
            cleaned = re.sub(r"\s+", " ", (value or "").strip().lower())
            cleaned = re.sub(r"^[\"'`*]+|[\"'`*]+$", "", cleaned)
            return cleaned

        used_affection: list[str] = []
        used_grudge: list[str] = []
        for affection_nick, grudge_nick in rows:
            if affection_nick:
                key = _norm(affection_nick)
                if key:
                    used_affection.append(key)
            if grudge_nick:
                key = _norm(grudge_nick)
                if key:
                    used_grudge.append(key)
        return {
            "affection": used_affection,
            "grudge": used_grudge,
            "all": list(dict.fromkeys(used_affection + used_grudge)),
        }

    # ── Trust ─────────────────────────────────────────────────────────────────
    async def update_trust(self, user_id: int, delta: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET trust=MAX(0,MIN(100,trust+?)) WHERE user_id=?", (delta, user_id))
            await db.commit()

    # ── Grudge ────────────────────────────────────────────────────────────────
    async def set_grudge_nick(self, user_id: int, nick: str | None):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET grudge_nick=? WHERE user_id=?", (nick, user_id))
            await db.commit()

    # ── Rivalry ───────────────────────────────────────────────────────────────
    async def set_rival(self, user_id: int, rival_id: int | None):
        async with aiosqlite.connect(DB_PATH) as db:
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
        async with aiosqlite.connect(DB_PATH) as db:
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
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE users SET slow_burn_fired=1 WHERE user_id=?", (user_id,))
                await db.commit()
            return new_score, True
        return new_score, False

    async def reset_slow_burn_fired(self, user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET slow_burn=0, slow_burn_fired=0 WHERE user_id=?", (user_id,))
            await db.commit()

    # ── Personality drift ─────────────────────────────────────────────────────
    async def update_drift(self, user_id: int, delta: int):
        """Drift: 0=unchanged, 100=maximally drifted. Changes subtly over weeks."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET drift_score=MAX(0,MIN(100,drift_score+?)) WHERE user_id=?", (delta, user_id))
            await db.commit()

    async def set_style_profile(self, user_id: int, profile: dict):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET style_profile=? WHERE user_id=?",
                (json.dumps(profile, ensure_ascii=True)[:1200], user_id),
            )
            await db.commit()

    async def set_emotional_arc(self, user_id: int, arc: str):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET emotional_arc=? WHERE user_id=?", (arc[:40], user_id))
            await db.commit()

    async def open_conflict(self, user_id: int, summary: str):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET conflict_open=1, conflict_summary=?, last_conflict_ts=?, repair_progress=0 WHERE user_id=?",
                (summary[:300], time.time(), user_id),
            )
            await db.commit()

    async def resolve_conflict(self, user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
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
        async with aiosqlite.connect(DB_PATH) as db:
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
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET callback_memory=?, callback_ts=? WHERE user_id=?",
                (callback_memory[:300], time.time(), user_id),
            )
            await db.commit()

    async def consume_phrase(self, scope: str, phrase_key: str, cooldown_seconds: int) -> bool:
        now = time.time()
        async with aiosqlite.connect(DB_PATH) as db:
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
        async with aiosqlite.connect(DB_PATH) as db:
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
        async with aiosqlite.connect(self.shared_db_path) as db:
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
        async with aiosqlite.connect(self.shared_db_path) as db:
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
        async with aiosqlite.connect(self.shared_db_path) as db:
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
        async with aiosqlite.connect(self.shared_db_path) as db:
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
        async with aiosqlite.connect(self.shared_db_path) as db:
            await db.execute(
                "INSERT INTO duo_sessions (channel_id,mode,topic,initiator_bot,initiator_user_id,last_speaker,awaiting_bot,autoplay_remaining,next_autoplay_ts,expires_ts,updated_ts,silent_bot,silent_until,intervention_cooldown) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(channel_id) DO UPDATE SET mode=excluded.mode, topic=excluded.topic, "
                "initiator_bot=excluded.initiator_bot, initiator_user_id=excluded.initiator_user_id, awaiting_bot=excluded.awaiting_bot, autoplay_remaining=excluded.autoplay_remaining, "
                "next_autoplay_ts=excluded.next_autoplay_ts, expires_ts=excluded.expires_ts, updated_ts=excluded.updated_ts, "
                "silent_bot=excluded.silent_bot, silent_until=excluded.silent_until, intervention_cooldown=excluded.intervention_cooldown",
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
                    "",
                    0,
                    0,
                ),
            )
            await db.commit()

    async def get_duo_session(self, channel_id: int) -> dict | None:
        now = time.time()
        async with aiosqlite.connect(self.shared_db_path) as db:
            async with db.execute(
                "SELECT mode,topic,initiator_bot,initiator_user_id,last_speaker,awaiting_bot,autoplay_remaining,next_autoplay_ts,expires_ts,updated_ts,silent_bot,silent_until,intervention_cooldown "
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
            "silent_bot": row[10] or "",
            "silent_until": row[11] or 0,
            "intervention_cooldown": row[12] or 0,
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
        async with aiosqlite.connect(self.shared_db_path) as db:
            await db.execute(
                "UPDATE duo_sessions SET last_speaker=?, awaiting_bot=?, autoplay_remaining=?, next_autoplay_ts=?, updated_ts=?, expires_ts=? WHERE channel_id=?",
                (speaker_bot[:40], awaiting_bot[:40], autoplay_remaining, next_autoplay_ts, now, now + ttl_seconds, channel_id),
            )
            await db.commit()

    async def get_due_duo_sessions(self, bot_name: str) -> list[dict]:
        now = time.time()
        async with aiosqlite.connect(self.shared_db_path) as db:
            async with db.execute(
                "SELECT channel_id,mode,topic,initiator_bot,initiator_user_id,last_speaker,awaiting_bot,autoplay_remaining,next_autoplay_ts,expires_ts,updated_ts,silent_bot,silent_until,intervention_cooldown "
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
                "silent_bot": row[11] or "",
                "silent_until": row[12] or 0,
                "intervention_cooldown": row[13] or 0,
            }
            for row in rows
        ]

    async def set_duo_presence(
        self,
        channel_id: int,
        *,
        silent_bot: str = "",
        silent_until: float = 0,
        intervention_cooldown: float | None = None,
    ):
        current = await self.get_duo_session(channel_id)
        if not current:
            return
        async with aiosqlite.connect(self.shared_db_path) as db:
            await db.execute(
                "UPDATE duo_sessions SET silent_bot=?, silent_until=?, intervention_cooldown=COALESCE(?, intervention_cooldown), updated_ts=? WHERE channel_id=?",
                (
                    (silent_bot or "")[:40],
                    float(silent_until or 0),
                    intervention_cooldown,
                    time.time(),
                    channel_id,
                ),
            )
            await db.commit()

    async def clear_duo_session(self, channel_id: int):
        async with aiosqlite.connect(self.shared_db_path) as db:
            await db.execute("DELETE FROM duo_sessions WHERE channel_id=?", (channel_id,))
            await db.commit()

    async def set_channel_speaker_mode(self, channel_id: int, speaker_mode: str):
        async with aiosqlite.connect(self.shared_db_path) as db:
            await db.execute(
                "INSERT INTO channel_speaker_modes (channel_id,speaker_mode,updated_ts) VALUES (?,?,?) "
                "ON CONFLICT(channel_id) DO UPDATE SET speaker_mode=excluded.speaker_mode, updated_ts=excluded.updated_ts",
                (channel_id, speaker_mode[:20], time.time()),
            )
            await db.commit()

    async def get_channel_speaker_mode(self, channel_id: int) -> str:
        async with aiosqlite.connect(self.shared_db_path) as db:
            async with db.execute("SELECT speaker_mode FROM channel_speaker_modes WHERE channel_id=?", (channel_id,)) as cur:
                row = await cur.fetchone()
        return (row[0] if row else "auto") or "auto"

    async def start_duo_story(self, channel_id: int, story_type: str, topic: str, enemy: str = "") -> int:
        now = time.time()
        async with aiosqlite.connect(self.shared_db_path) as db:
            cur = await db.execute(
                "INSERT INTO duo_story_log (channel_id,story_type,topic,status,outcome,enemy,ts,updated_ts) VALUES (?,?,?,?,?,?,?,?)",
                (channel_id, story_type[:40], topic[:300], "open", "", enemy[:120], now, now),
            )
            await db.commit()
            row_id = cur.lastrowid
        await self.add_world_case(
            channel_id,
            story_type,
            topic,
            status="open",
            summary=f"{story_type[:40]} opened: {topic[:180]}",
            enemy=enemy,
            updated_by=self.bot_name,
        )
        if enemy:
            await self.upsert_world_entity(
                "enemy",
                enemy,
                summary=f"Tied to an active {story_type[:40]} in channel {channel_id}.",
                status="active",
                channel_id=channel_id,
                updated_by=self.bot_name,
            )
        return row_id

    async def resolve_duo_story(self, channel_id: int, story_type: str, outcome: str):
        raw_outcome = (outcome or "").strip()
        status = "resolved"
        summary = raw_outcome[:300]
        if raw_outcome.startswith("ENDING:"):
            tag_part, _, remainder = raw_outcome.partition("|")
            tag = tag_part.split(":", 1)[1].strip().lower()
            if tag:
                status = tag[:30]
            if remainder.strip():
                summary = remainder.strip()[:300]
        async with aiosqlite.connect(self.shared_db_path) as db:
            await db.execute(
                "UPDATE duo_story_log SET status=?, outcome=?, updated_ts=? "
                "WHERE id=(SELECT id FROM duo_story_log WHERE channel_id=? AND story_type=? ORDER BY ts DESC LIMIT 1)",
                (status[:30], summary[:300], time.time(), channel_id, story_type[:40]),
            )
            await db.commit()
        lowered = summary.lower()
        if status == "resolved" and any(token in lowered for token in ["failed", "lost", "unresolved", "escaped", "collapsed", "guilty"]):
            status = "scarred"
        await self.update_latest_world_case(
            channel_id,
            story_type,
            status=status,
            summary=summary[:240],
            updated_by=self.bot_name,
        )

    async def get_recent_duo_stories(self, channel_id: int, limit: int = 5) -> list[dict]:
        async with aiosqlite.connect(self.shared_db_path) as db:
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

    async def get_open_duo_story(self, channel_id: int, story_type: str | None = None) -> dict | None:
        query = (
            "SELECT story_type,topic,status,outcome,enemy,ts,updated_ts "
            "FROM duo_story_log WHERE channel_id=? AND status='open'"
        )
        params: list[object] = [channel_id]
        if story_type:
            query += " AND story_type=?"
            params.append((story_type or "")[:40])
        query += " ORDER BY updated_ts DESC LIMIT 1"
        async with aiosqlite.connect(self.shared_db_path) as db:
            async with db.execute(query, params) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        return {
            "story_type": row[0] or "",
            "topic": row[1] or "",
            "status": row[2] or "open",
            "outcome": row[3] or "",
            "enemy": row[4] or "",
            "ts": row[5] or 0,
            "updated_ts": row[6] or 0,
        }

    async def note_duo_story_progress(self, channel_id: int, story_type: str, summary: str):
        note = (summary or "").strip()[:260]
        if not note:
            return
        now = time.time()
        async with aiosqlite.connect(self.shared_db_path) as db:
            await db.execute(
                "UPDATE duo_story_log SET outcome=?, updated_ts=? "
                "WHERE id=(SELECT id FROM duo_story_log WHERE channel_id=? AND story_type=? AND status='open' ORDER BY ts DESC LIMIT 1)",
                (note, now, channel_id, (story_type or "")[:40]),
            )
            await db.commit()
        await self.update_latest_world_case(
            channel_id,
            story_type,
            summary=note,
            updated_by=self.bot_name,
        )

    def _world_entity_key(self, entity_type: str, name: str) -> str:
        cleaned = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
        return f"{(entity_type or 'entity').strip().lower()}:{cleaned[:80] or 'unknown'}"

    async def upsert_world_entity(
        self,
        entity_type: str,
        name: str,
        *,
        summary: str = "",
        status: str = "",
        channel_id: int = 0,
        owner_user_id: int = 0,
        updated_by: str = "",
    ) -> str:
        entity_type = (entity_type or "entity").strip().lower()[:40]
        name = (name or "").strip()[:120]
        if not name:
            return ""
        entity_key = self._world_entity_key(entity_type, name)
        now = time.time()
        async with aiosqlite.connect(self.shared_db_path) as db:
            await db.execute(
                "INSERT INTO shared_world_entities (entity_key,entity_type,name,summary,status,channel_id,owner_user_id,updated_by,ts,updated_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(entity_key) DO UPDATE SET "
                "summary=CASE WHEN excluded.summary IS NOT NULL AND excluded.summary!='' THEN excluded.summary ELSE shared_world_entities.summary END, "
                "status=CASE WHEN excluded.status IS NOT NULL AND excluded.status!='' THEN excluded.status ELSE shared_world_entities.status END, "
                "channel_id=CASE WHEN excluded.channel_id!=0 THEN excluded.channel_id ELSE shared_world_entities.channel_id END, "
                "owner_user_id=CASE WHEN excluded.owner_user_id!=0 THEN excluded.owner_user_id ELSE shared_world_entities.owner_user_id END, "
                "updated_by=CASE WHEN excluded.updated_by IS NOT NULL AND excluded.updated_by!='' THEN excluded.updated_by ELSE shared_world_entities.updated_by END, "
                "updated_ts=excluded.updated_ts",
                (
                    entity_key,
                    entity_type,
                    name,
                    (summary or "")[:240],
                    (status or "")[:120],
                    channel_id or 0,
                    owner_user_id or 0,
                    (updated_by or "")[:40],
                    now,
                    now,
                ),
            )
            await db.commit()
        return entity_key

    async def list_world_entities(
        self,
        entity_type: str | None = None,
        *,
        limit: int = 8,
        channel_id: int | None = None,
    ) -> list[dict]:
        query = "SELECT entity_key,entity_type,name,summary,status,channel_id,owner_user_id,updated_by,ts,updated_ts FROM shared_world_entities"
        clauses = []
        params: list[object] = []
        if entity_type:
            clauses.append("entity_type=?")
            params.append(entity_type[:40])
        if channel_id is not None:
            clauses.append("(channel_id=? OR channel_id=0)")
            params.append(channel_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_ts DESC LIMIT ?"
        params.append(limit)
        async with aiosqlite.connect(self.shared_db_path) as db:
            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()
        return [
            {
                "entity_key": row[0] or "",
                "entity_type": row[1] or "",
                "name": row[2] or "",
                "summary": row[3] or "",
                "status": row[4] or "",
                "channel_id": row[5] or 0,
                "owner_user_id": row[6] or 0,
                "updated_by": row[7] or "",
                "ts": row[8] or 0,
                "updated_ts": row[9] or 0,
            }
            for row in rows
        ]

    async def note_campaign_npc(
        self,
        name: str,
        *,
        role: str = "npc",
        summary: str = "",
        status: str = "active",
        channel_id: int = 0,
        owner_user_id: int = 0,
        updated_by: str = "",
    ) -> str:
        normalized_role = (role or "npc").strip().lower()
        if normalized_role not in {"ally", "enemy", "npc", "figure", "faction", "rival"}:
            normalized_role = "npc"
        return await self.upsert_world_entity(
            normalized_role,
            name,
            summary=summary,
            status=status,
            channel_id=channel_id,
            owner_user_id=owner_user_id,
            updated_by=updated_by or self.bot_name,
        )

    async def list_campaign_npcs(self, *, channel_id: int | None = None, limit: int = 8) -> list[dict]:
        roles = ("ally", "enemy", "npc", "figure", "faction", "rival")
        placeholders = ",".join("?" for _ in roles)
        query = (
            "SELECT entity_key,entity_type,name,summary,status,channel_id,owner_user_id,updated_by,ts,updated_ts "
            f"FROM shared_world_entities WHERE entity_type IN ({placeholders})"
        )
        params: list[object] = list(roles)
        if channel_id is not None:
            query += " AND (channel_id=? OR channel_id=0)"
            params.append(channel_id)
        query += " ORDER BY updated_ts DESC LIMIT ?"
        params.append(limit)
        async with aiosqlite.connect(self.shared_db_path) as db:
            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()
        return [
            {
                "entity_key": row[0] or "",
                "entity_type": row[1] or "",
                "name": row[2] or "",
                "summary": row[3] or "",
                "status": row[4] or "",
                "channel_id": row[5] or 0,
                "owner_user_id": row[6] or 0,
                "updated_by": row[7] or "",
                "ts": row[8] or 0,
                "updated_ts": row[9] or 0,
            }
            for row in rows
        ]

    async def add_world_case(
        self,
        channel_id: int,
        case_type: str,
        title: str,
        *,
        status: str = "open",
        summary: str = "",
        enemy: str = "",
        updated_by: str = "",
    ) -> str:
        now = time.time()
        case_key = f"{channel_id}:{(case_type or 'case')[:30]}:{int(now * 1000)}"
        async with aiosqlite.connect(self.shared_db_path) as db:
            await db.execute(
                "INSERT INTO shared_world_cases (case_key,channel_id,case_type,title,status,summary,enemy,updated_by,opened_ts,updated_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    case_key,
                    channel_id,
                    (case_type or "case")[:40],
                    (title or "")[:180],
                    (status or "open")[:30],
                    (summary or "")[:260],
                    (enemy or "")[:140],
                    (updated_by or "")[:40],
                    now,
                    now,
                ),
            )
            await db.commit()
        return case_key

    async def update_latest_world_case(
        self,
        channel_id: int,
        case_type: str,
        *,
        status: str = "",
        summary: str = "",
        enemy: str = "",
        updated_by: str = "",
    ) -> bool:
        now = time.time()
        async with aiosqlite.connect(self.shared_db_path) as db:
            cur = await db.execute(
                "SELECT case_key,status,summary,enemy,updated_by FROM shared_world_cases "
                "WHERE channel_id=? AND case_type=? ORDER BY opened_ts DESC LIMIT 1",
                (channel_id, (case_type or "case")[:40]),
            )
            row = await cur.fetchone()
            if not row:
                return False
            await db.execute(
                "UPDATE shared_world_cases SET status=?, summary=?, enemy=?, updated_by=?, updated_ts=? WHERE case_key=?",
                (
                    (status or row[1] or "open")[:30],
                    (summary or row[2] or "")[:260],
                    (enemy or row[3] or "")[:140],
                    (updated_by or row[4] or "")[:40],
                    now,
                    row[0],
                ),
            )
            await db.commit()
        return True

    async def list_world_cases(
        self,
        *,
        channel_id: int | None = None,
        status: str | None = None,
        limit: int = 8,
    ) -> list[dict]:
        query = "SELECT case_key,channel_id,case_type,title,status,summary,enemy,updated_by,opened_ts,updated_ts FROM shared_world_cases"
        clauses = []
        params: list[object] = []
        if channel_id is not None:
            clauses.append("channel_id=?")
            params.append(channel_id)
        if status:
            clauses.append("status=?")
            params.append(status[:30])
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_ts DESC LIMIT ?"
        params.append(limit)
        async with aiosqlite.connect(self.shared_db_path) as db:
            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()
        return [
            {
                "case_key": row[0] or "",
                "channel_id": row[1] or 0,
                "case_type": row[2] or "",
                "title": row[3] or "",
                "status": row[4] or "",
                "summary": row[5] or "",
                "enemy": row[6] or "",
                "updated_by": row[7] or "",
                "opened_ts": row[8] or 0,
                "updated_ts": row[9] or 0,
            }
            for row in rows
        ]

    def _shared_event_key(self, channel_id: int, topic: str) -> str:
        cleaned = re.sub(r"[^a-z0-9]+", "-", (topic or "").strip().lower()).strip("-")
        return f"{channel_id}:{cleaned[:90] or 'event'}"

    async def note_shared_event_memory(
        self,
        channel_id: int,
        topic: str,
        bot_name: str,
        memory_text: str,
        *,
        truth_hint: str = "",
        distorted: bool = False,
    ) -> str:
        topic = (topic or "").strip()[:180]
        memory_text = (memory_text or "").strip()[:320]
        if not topic or not memory_text:
            return ""
        event_key = self._shared_event_key(channel_id, topic)
        column = "wanderer_memory" if (bot_name or "").strip().lower() == "wanderer" else "scaramouche_memory"
        now = time.time()
        async with aiosqlite.connect(self.shared_db_path) as db:
            await db.execute(
                "INSERT INTO shared_event_memories (event_key,channel_id,topic,truth_hint,distorted_bot,last_noticed_by,created_ts,updated_ts) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(event_key) DO UPDATE SET "
                "topic=excluded.topic, "
                "truth_hint=CASE WHEN excluded.truth_hint IS NOT NULL AND excluded.truth_hint!='' THEN excluded.truth_hint ELSE shared_event_memories.truth_hint END, "
                "last_noticed_by=excluded.last_noticed_by, "
                "updated_ts=excluded.updated_ts",
                (
                    event_key,
                    channel_id,
                    topic,
                    (truth_hint or "")[:220],
                    (bot_name if distorted else "")[:40],
                    (bot_name or "")[:40],
                    now,
                    now,
                ),
            )
            await db.execute(
                f"UPDATE shared_event_memories SET {column}=?, distorted_bot=CASE WHEN ? THEN ? ELSE distorted_bot END, "
                "last_noticed_by=?, updated_ts=? WHERE event_key=?",
                (
                    memory_text,
                    1 if distorted else 0,
                    (bot_name or "")[:40],
                    (bot_name or "")[:40],
                    now,
                    event_key,
                ),
            )
            await db.commit()
        return event_key

    async def get_shared_event_memory(self, channel_id: int, topic_hint: str = "", limit: int = 1) -> list[dict]:
        topic_hint = (topic_hint or "").strip().lower()
        query = (
            "SELECT event_key,channel_id,topic,scaramouche_memory,wanderer_memory,truth_hint,distorted_bot,last_noticed_by,created_ts,updated_ts "
            "FROM shared_event_memories WHERE channel_id=?"
        )
        params: list[object] = [channel_id]
        if topic_hint:
            query += " AND LOWER(topic) LIKE ?"
            params.append(f"%{topic_hint[:80]}%")
        query += " ORDER BY updated_ts DESC LIMIT ?"
        params.append(limit)
        async with aiosqlite.connect(self.shared_db_path) as db:
            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()
        return [
            {
                "event_key": row[0] or "",
                "channel_id": row[1] or 0,
                "topic": row[2] or "",
                "scaramouche_memory": row[3] or "",
                "wanderer_memory": row[4] or "",
                "truth_hint": row[5] or "",
                "distorted_bot": row[6] or "",
                "last_noticed_by": row[7] or "",
                "created_ts": row[8] or 0,
                "updated_ts": row[9] or 0,
            }
            for row in rows
        ]

    async def mark_memory_distortion(self, event_key: str, bot_name: str):
        async with aiosqlite.connect(self.shared_db_path) as db:
            await db.execute(
                "UPDATE shared_event_memories SET distorted_bot=?, updated_ts=? WHERE event_key=?",
                ((bot_name or "")[:40], time.time(), event_key[:140]),
            )
            await db.commit()

    async def add_evidence_item(
        self,
        channel_id: int,
        evidence_type: str,
        label: str,
        summary: str,
        *,
        source_excerpt: str = "",
        owner_user_id: int = 0,
        linked_case: str = "",
        updated_by: str = "",
    ) -> str:
        label = (label or "").strip()[:140]
        summary = (summary or "").strip()[:260]
        if not label or not summary:
            return ""
        now = time.time()
        evidence_key = f"{channel_id}:{(evidence_type or 'evidence')[:24]}:{int(now * 1000)}"
        async with aiosqlite.connect(self.shared_db_path) as db:
            await db.execute(
                "INSERT INTO shared_evidence_locker (evidence_key,channel_id,evidence_type,label,summary,source_excerpt,owner_user_id,linked_case,updated_by,created_ts,updated_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence_key,
                    channel_id,
                    (evidence_type or "evidence")[:40],
                    label,
                    summary,
                    (source_excerpt or "")[:240],
                    owner_user_id or 0,
                    (linked_case or "")[:80],
                    (updated_by or self.bot_name)[:40],
                    now,
                    now,
                ),
            )
            await db.commit()
        return evidence_key

    async def list_evidence_items(
        self,
        channel_id: int,
        *,
        evidence_type: str | None = None,
        limit: int = 6,
    ) -> list[dict]:
        query = (
            "SELECT evidence_key,channel_id,evidence_type,label,summary,source_excerpt,owner_user_id,linked_case,updated_by,created_ts,updated_ts "
            "FROM shared_evidence_locker WHERE channel_id=?"
        )
        params: list[object] = [channel_id]
        if evidence_type:
            query += " AND evidence_type=?"
            params.append(evidence_type[:40])
        query += " ORDER BY updated_ts DESC LIMIT ?"
        params.append(limit)
        async with aiosqlite.connect(self.shared_db_path) as db:
            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()
        return [
            {
                "evidence_key": row[0] or "",
                "channel_id": row[1] or 0,
                "evidence_type": row[2] or "",
                "label": row[3] or "",
                "summary": row[4] or "",
                "source_excerpt": row[5] or "",
                "owner_user_id": row[6] or 0,
                "linked_case": row[7] or "",
                "updated_by": row[8] or "",
                "created_ts": row[9] or 0,
                "updated_ts": row[10] or 0,
            }
            for row in rows
        ]

    async def set_private_opinion(
        self,
        scope: str,
        bot_name: str,
        subject_type: str,
        subject_key: str,
        opinion: str,
        *,
        intensity: int = 1,
    ):
        opinion = (opinion or "").strip()[:260]
        if not opinion:
            return
        now = time.time()
        async with aiosqlite.connect(self.shared_db_path) as db:
            await db.execute(
                "INSERT INTO interbot_private_opinions (scope,bot_name,subject_type,subject_key,opinion,intensity,updated_ts) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(scope,bot_name,subject_type,subject_key) DO UPDATE SET "
                "opinion=excluded.opinion, intensity=excluded.intensity, updated_ts=excluded.updated_ts",
                (
                    (scope or "")[:80],
                    (bot_name or "")[:40],
                    (subject_type or "")[:30],
                    (subject_key or "")[:80],
                    opinion,
                    max(1, min(10, int(intensity))),
                    now,
                ),
            )
            await db.commit()

    async def list_private_opinions(self, scope: str, limit: int = 6) -> list[dict]:
        async with aiosqlite.connect(self.shared_db_path) as db:
            async with db.execute(
                "SELECT scope,bot_name,subject_type,subject_key,opinion,intensity,leaked_ts,updated_ts "
                "FROM interbot_private_opinions WHERE scope=? ORDER BY updated_ts DESC LIMIT ?",
                ((scope or "")[:80], limit),
            ) as cur:
                rows = await cur.fetchall()
        return [
            {
                "scope": row[0] or "",
                "bot_name": row[1] or "",
                "subject_type": row[2] or "",
                "subject_key": row[3] or "",
                "opinion": row[4] or "",
                "intensity": row[5] or 0,
                "leaked_ts": row[6] or 0,
                "updated_ts": row[7] or 0,
            }
            for row in rows
        ]

    async def mark_private_opinion_leaked(self, scope: str, bot_name: str, subject_type: str, subject_key: str):
        async with aiosqlite.connect(self.shared_db_path) as db:
            await db.execute(
                "UPDATE interbot_private_opinions SET leaked_ts=?, updated_ts=? WHERE scope=? AND bot_name=? AND subject_type=? AND subject_key=?",
                (
                    time.time(),
                    time.time(),
                    (scope or "")[:80],
                    (bot_name or "")[:40],
                    (subject_type or "")[:30],
                    (subject_key or "")[:80],
                ),
            )
            await db.commit()

    async def add_consequence_mark(
        self,
        user_id: int,
        kind: str,
        summary: str,
        *,
        severity: int = 2,
        decay_days: float = 5.0,
    ):
        kind = (kind or "scar").strip().lower()[:40]
        summary = (summary or "").strip()[:240]
        if not summary:
            return
        severity = max(1, min(10, int(severity)))
        decay_days = max(1.0, float(decay_days))
        now = time.time()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT id,severity FROM consequence_marks WHERE user_id=? AND kind=? AND summary=? ORDER BY last_seen DESC LIMIT 1",
                (user_id, kind, summary),
            ) as cur:
                row = await cur.fetchone()
            if row:
                await db.execute(
                    "UPDATE consequence_marks SET severity=MIN(10, severity+?), last_seen=? WHERE id=?",
                    (severity, now, row[0]),
                )
            else:
                await db.execute(
                    "INSERT INTO consequence_marks (user_id,kind,summary,severity,decay_days,created_ts,last_seen) VALUES (?,?,?,?,?,?,?)",
                    (user_id, kind, summary, severity, decay_days, now, now),
                )
            await db.commit()

    async def soften_consequence_marks(self, user_id: int, amount: int = 1):
        amount = max(1, int(amount))
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE consequence_marks SET severity=MAX(0, severity-?), last_seen=? WHERE user_id=?",
                (amount, time.time(), user_id),
            )
            await db.execute("DELETE FROM consequence_marks WHERE user_id=? AND severity<=0", (user_id,))
            await db.commit()

    async def get_active_consequence_marks(self, user_id: int, limit: int = 5) -> list[dict]:
        now = time.time()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT id,kind,summary,severity,decay_days,created_ts,last_seen FROM consequence_marks "
                "WHERE user_id=? ORDER BY last_seen DESC, severity DESC",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()
            active: list[dict] = []
            expired_ids: list[int] = []
            for row in rows:
                age_days = max(0.0, (now - (row[6] or row[5] or now)) / 86400)
                remaining = max(0, int((row[3] or 0) - (age_days / max(1.0, float(row[4] or 5.0)))))
                if remaining <= 0:
                    expired_ids.append(row[0])
                    continue
                active.append(
                    {
                        "id": row[0],
                        "kind": row[1] or "",
                        "summary": row[2] or "",
                        "severity": row[3] or 0,
                        "decay_days": row[4] or 5.0,
                        "created_ts": row[5] or 0,
                        "last_seen": row[6] or 0,
                        "remaining": remaining,
                    }
                )
                if len(active) >= limit:
                    break
            if expired_ids:
                await db.execute(
                    f"DELETE FROM consequence_marks WHERE id IN ({','.join('?' for _ in expired_ids)})",
                    expired_ids,
                )
                await db.commit()
        return active

    # ── Memory summary ────────────────────────────────────────────────────────
    async def record_bot_attention(
        self,
        user_id: int,
        bot_name: str,
        amount: int = 1,
        *,
        direct: bool = False,
        topic: str = "",
    ):
        now = time.time()
        inc = max(1, int(amount))
        direct_inc = inc if direct else 0
        mention_inc = 1 if direct else 0
        async with aiosqlite.connect(self.shared_db_path) as db:
            await db.execute(
                "INSERT INTO user_bot_attention (user_id,bot_name,score,direct_score,mention_count,last_topic,last_interaction) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(user_id,bot_name) DO UPDATE SET "
                "score=MIN(250, user_bot_attention.score + excluded.score), "
                "direct_score=MIN(250, user_bot_attention.direct_score + excluded.direct_score), "
                "mention_count=user_bot_attention.mention_count + excluded.mention_count, "
                "last_topic=CASE WHEN excluded.last_topic IS NOT NULL AND excluded.last_topic!='' THEN excluded.last_topic ELSE user_bot_attention.last_topic END, "
                "last_interaction=excluded.last_interaction",
                (user_id, (bot_name or "")[:40], inc, direct_inc, mention_inc, topic[:140], now),
            )
            await db.commit()

    async def get_triangle_state(self, user_id: int, current_bot: str, partner_bot: str) -> dict:
        async with aiosqlite.connect(self.shared_db_path) as db:
            async with db.execute(
                "SELECT bot_name,score,direct_score,mention_count,last_topic,last_interaction FROM user_bot_attention "
                "WHERE user_id=? AND bot_name IN (?,?)",
                (user_id, current_bot[:40], partner_bot[:40]),
            ) as cur:
                rows = await cur.fetchall()
        payload = {
            "favored_bot": "",
            "margin": 0,
            "jealousy_level": 0,
            "current_score": 0,
            "current_direct": 0,
            "partner_score": 0,
            "partner_direct": 0,
            "partner_topic": "",
            "partner_last_interaction": 0,
        }
        for row in rows:
            name = (row[0] or "").lower()
            if name == (current_bot or "").lower():
                payload["current_score"] = row[1] or 0
                payload["current_direct"] = row[2] or 0
            elif name == (partner_bot or "").lower():
                payload["partner_score"] = row[1] or 0
                payload["partner_direct"] = row[2] or 0
                payload["partner_topic"] = row[4] or ""
                payload["partner_last_interaction"] = row[5] or 0
        margin = (payload["partner_score"] + payload["partner_direct"]) - (payload["current_score"] + payload["current_direct"])
        payload["margin"] = margin
        if margin >= 6:
            payload["favored_bot"] = partner_bot
            payload["jealousy_level"] = max(0, min(100, margin * 6))
        elif margin <= -6:
            payload["favored_bot"] = current_bot
        return payload

    async def unlock_hidden_achievement(self, scope: str, achievement_key: str, note: str = "") -> bool:
        now = time.time()
        async with aiosqlite.connect(self.shared_db_path) as db:
            cur = await db.execute(
                "INSERT OR IGNORE INTO hidden_achievements (scope,achievement_key,note,unlocked_ts) VALUES (?,?,?,?)",
                (scope[:80], achievement_key[:80], note[:240], now),
            )
            await db.commit()
            return bool(cur.rowcount)

    async def get_hidden_achievements(self, scope: str, limit: int = 8) -> list[dict]:
        async with aiosqlite.connect(self.shared_db_path) as db:
            async with db.execute(
                "SELECT achievement_key,note,unlocked_ts FROM hidden_achievements WHERE scope=? ORDER BY unlocked_ts DESC LIMIT ?",
                (scope[:80], limit),
            ) as cur:
                rows = await cur.fetchall()
        return [
            {"achievement_key": row[0] or "", "note": row[1] or "", "unlocked_ts": row[2] or 0}
            for row in rows
        ]

    async def consume_shared_cooldown(self, scope: str, cooldown_seconds: int) -> tuple[bool, int]:
        now = time.time()
        async with aiosqlite.connect(self.shared_db_path) as db:
            async with db.execute("SELECT last_used FROM shared_cooldowns WHERE scope=?", (scope[:120],)) as cur:
                row = await cur.fetchone()
            if row and (now - (row[0] or 0)) < cooldown_seconds:
                remaining = max(0, int(cooldown_seconds - (now - (row[0] or 0))))
                return False, remaining
            await db.execute(
                "INSERT INTO shared_cooldowns (scope,last_used) VALUES (?,?) "
                "ON CONFLICT(scope) DO UPDATE SET last_used=excluded.last_used",
                (scope[:120], now),
            )
            await db.commit()
        return True, 0

    async def needs_summary(self, user_id: int) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT message_count, summary_msg_count FROM users WHERE user_id=?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
                if not row: return False
                count, last = row[0] or 0, row[1] or 0
                return count >= 500 and (count - last) >= 50

    async def save_summary(self, user_id: int, summary: str):
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT message_count FROM users WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone(); count = row[0] if row else 0
            await db.execute(
                "UPDATE users SET memory_summary=?, summary_msg_count=? WHERE user_id=?",
                (summary[:1000], count, user_id))
            await db.commit()

    # ── Contradiction tracking ────────────────────────────────────────────────
    async def update_last_statement(self, user_id: int, statement: str):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET last_statement=? WHERE user_id=?", (statement[:300], user_id))
            await db.commit()

    # ── Message count & milestones ────────────────────────────────────────────
    async def increment_message_count(self, user_id: int) -> tuple[int, bool]:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET message_count=message_count+1 WHERE user_id=?", (user_id,))
            await db.commit()
            async with db.execute("SELECT message_count,milestone_last FROM users WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone()
                if not row: return 1, False
                count, last = row
        milestone = _message_milestone_for_count(count)
        if milestone and (last or 0) < milestone:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE users SET milestone_last=? WHERE user_id=?", (milestone, user_id))
                await db.commit()
            return count, True
        return count, False

    async def rebuild_user_rank(
        self,
        user_id: int,
        username: str,
        display_name: str,
        message_count: int,
        *,
        first_seen: float = 0.0,
        last_seen: float = 0.0,
        replace: bool = False,
    ) -> dict:
        recovered_count = max(0, int(message_count or 0))
        recovered_first = float(first_seen or 0.0)
        recovered_last = float(last_seen or 0.0)
        now = time.time()
        recovered_milestone = _message_milestone_for_count(recovered_count)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT username,display_name,message_count,milestone_last,first_seen,last_seen,last_active FROM users WHERE user_id=?",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()

            if row:
                existing_username = row[0] or ""
                existing_display = row[1] or ""
                existing_count = int(row[2] or 0)
                existing_milestone = int(row[3] or 0)
                existing_first = float(row[4] or 0.0)
                existing_last = float(row[5] or 0.0)
                existing_active = float(row[6] or 0.0)

                final_count = recovered_count if replace else max(existing_count, recovered_count)
                final_milestone = _message_milestone_for_count(final_count) if replace else max(existing_milestone, _message_milestone_for_count(final_count), recovered_milestone)
                final_first = existing_first
                if recovered_first and (not final_first or recovered_first < final_first):
                    final_first = recovered_first
                if not final_first:
                    final_first = now
                final_last = max(existing_last, recovered_last, final_first)
                final_active = max(existing_active, recovered_last, final_last)

                await db.execute(
                    "UPDATE users SET username=?,display_name=?,message_count=?,milestone_last=?,first_seen=?,last_seen=?,last_active=? WHERE user_id=?",
                    (
                        (username or existing_username or str(user_id))[:120],
                        (display_name or existing_display or username or str(user_id))[:120],
                        final_count,
                        final_milestone,
                        final_first,
                        final_last,
                        final_active,
                        user_id,
                    ),
                )
            else:
                seed_first = recovered_first or now
                seed_last = max(recovered_last, seed_first)
                await db.execute(
                    "INSERT INTO users (user_id,username,display_name,last_seen,last_active,first_seen,message_count,milestone_last) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        user_id,
                        (username or str(user_id))[:120],
                        (display_name or username or str(user_id))[:120],
                        seed_last,
                        seed_last,
                        seed_first,
                        recovered_count,
                        recovered_milestone,
                    ),
                )
            await db.commit()

        async with aiosqlite.connect(self.shared_db_path) as db:
            async with db.execute(
                "SELECT first_seen,last_seen,last_active FROM shared_users WHERE user_id=?",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()

            if row:
                existing_first = float(row[0] or 0.0)
                existing_last = float(row[1] or 0.0)
                existing_active = float(row[2] or 0.0)
                shared_first = existing_first
                if recovered_first and (not shared_first or recovered_first < shared_first):
                    shared_first = recovered_first
                if not shared_first:
                    shared_first = now
                shared_last = max(existing_last, recovered_last, shared_first)
                shared_active = max(existing_active, recovered_last, shared_last)
                await db.execute(
                    "INSERT INTO shared_users (user_id,username,display_name,first_seen,last_seen,last_active) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,display_name=excluded.display_name,first_seen=excluded.first_seen,last_seen=excluded.last_seen,last_active=excluded.last_active",
                    (
                        user_id,
                        (username or str(user_id))[:120],
                        (display_name or username or str(user_id))[:120],
                        shared_first,
                        shared_last,
                        shared_active,
                    ),
                )
            else:
                seed_first = recovered_first or now
                seed_last = max(recovered_last, seed_first)
                await db.execute(
                    "INSERT INTO shared_users (user_id,username,display_name,first_seen,last_seen,last_active) VALUES (?,?,?,?,?,?)",
                    (
                        user_id,
                        (username or str(user_id))[:120],
                        (display_name or username or str(user_id))[:120],
                        seed_first,
                        seed_last,
                        seed_last,
                    ),
                )
            await db.commit()

        return {
            "user_id": user_id,
            "message_count": recovered_count,
            "milestone_last": recovered_milestone,
        }

    async def get_top_users_in_guild(self, guild_id: int, limit: int = 60) -> list[dict]:
        """Who talks to the bot most in one server, counted from that server's own messages (never DMs or other
        servers), so a ranking cannot list people from elsewhere or reveal their activity there."""
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT m.user_id, COUNT(*) AS n FROM messages m JOIN channels c ON c.channel_id=m.channel_id "
                "WHERE c.guild_id=? AND m.role='user' AND m.user_id>0 AND (m.bot_name=? OR m.bot_name IS NULL) "
                "GROUP BY m.user_id ORDER BY n DESC, m.user_id LIMIT ?",
                (guild_id, self.bot_name, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [{"user_id": r[0], "message_count": r[1]} for r in rows]

    async def get_top_users(self, limit: int = 8) -> list[dict]:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT user_id,display_name,message_count FROM users ORDER BY message_count DESC LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
        return [{"user_id":r[0],"display_name":r[1],"message_count":r[2]} for r in rows]

    # ── Greetings ─────────────────────────────────────────────────────────────
    async def should_greet(self, user_id: int) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT greeted_today,last_active FROM users WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone()
                if not row: return False
                return not row[0] and (time.time() - (row[1] or 0)) < 172800

    async def mark_greeted(self, user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET greeted_today=1 WHERE user_id=?", (user_id,))
            await db.commit()

    async def reset_daily_greetings(self):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET greeted_today=0"); await db.commit()

    # ── Anniversary ───────────────────────────────────────────────────────────
    async def check_anniversary(self, user_id: int) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
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
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET anniversary_last=? WHERE user_id=?", (time.time(), user_id))
            await db.commit()

    # ── Absence ───────────────────────────────────────────────────────────────
    async def get_absent_romance_users(self, days: float = 3.0) -> list[dict]:
        cutoff = time.time() - days*86400
        async with aiosqlite.connect(DB_PATH) as db:
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
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM inside_jokes WHERE user_id=?", (user_id,)) as cur:
                count = (await cur.fetchone())[0]
            if count >= 10:
                await db.execute("DELETE FROM inside_jokes WHERE id=(SELECT id FROM inside_jokes WHERE user_id=? ORDER BY ts ASC LIMIT 1)", (user_id,))
            await db.execute("INSERT INTO inside_jokes (user_id,joke,ts) VALUES (?,?,?)", (user_id,joke[:200],time.time()))
            await db.commit()

    async def get_random_inside_joke(self, user_id: int) -> str | None:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT joke FROM inside_jokes WHERE user_id=? ORDER BY RANDOM() LIMIT 1", (user_id,)) as cur:
                row = await cur.fetchone(); return row[0] if row else None

    async def get_joke_count(self, user_id: int) -> int:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM inside_jokes WHERE user_id=?", (user_id,)) as cur:
                return (await cur.fetchone())[0]

    # ── Conversation history ──────────────────────────────────────────────────
    async def get_history(self, user_id: int, channel_id: int, limit: int = 200) -> list[dict]:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT role,content FROM messages WHERE user_id=? AND channel_id=? AND (bot_name=? OR bot_name IS NULL)
                ORDER BY ts DESC LIMIT ?
            """, (user_id,channel_id,self.bot_name,limit)) as cur:
                rows = await cur.fetchall()
        return [{"role":r[0],"content":r[1]} for r in reversed(rows)]

    async def get_random_old_message(self, user_id: int) -> str | None:
        cutoff = time.time()-86400*2
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT content FROM messages WHERE user_id=? AND role='user' AND (bot_name=? OR bot_name IS NULL) AND ts<? ORDER BY RANDOM() LIMIT 1", (user_id,self.bot_name,cutoff)) as cur:
                row = await cur.fetchone(); return row[0] if row else None

    async def get_recent_messages(
        self, user_id: int, limit: int = 10, *, guild_id: int | None = None, public_only: bool = False,
    ) -> list[str]:
        """A user's recent messages. ``guild_id`` limits them to that server's channels and ``public_only`` to any
        server channel; either way DMs (and, with ``guild_id``, other servers) are left out. Use one of them whenever
        the result can end up in front of other people."""
        join, extra, params = "", "", [user_id, self.bot_name]
        if guild_id is not None or public_only:
            join = " JOIN channels c ON c.channel_id=m.channel_id"
            if guild_id is not None:
                extra = " AND c.guild_id=?"
                params.append(guild_id)
            else:
                extra = " AND c.guild_id IS NOT NULL"  # only server channels are tracked; never count on that alone
        params.append(limit)
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                f"SELECT m.content FROM messages m{join} WHERE m.user_id=? AND m.role='user' "
                f"AND (m.bot_name=? OR m.bot_name IS NULL){extra} ORDER BY m.ts DESC LIMIT ?",
                params,
            ) as cur:
                return [r[0] for r in await cur.fetchall()]

    # ── Google Doc edit access (who proved they can edit a document) ─────────
    DOC_VERIFY_TTL_SECONDS = 30 * 86400  # re-prove after a month: edit access can be taken away

    async def is_doc_verified(self, doc_id: str, user_id: int) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT verified_at FROM doc_verifications WHERE doc_id=? AND user_id=?", (doc_id, user_id),
            ) as cur:
                row = await cur.fetchone()
        return bool(row and time.time() - float(row[0] or 0) < self.DOC_VERIFY_TTL_SECONDS)

    async def mark_doc_verified(self, doc_id: str, user_id: int):
        """Record that ``user_id`` proved edit access to ``doc_id`` (several editors of one document may each do so)."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO doc_verifications (doc_id,user_id,verified_at) VALUES (?,?,?)",
                (doc_id, user_id, time.time()),
            )
            await db.commit()

    async def get_channel_recent(self, channel_id: int, limit: int = 20) -> list[dict]:
        """Get recent messages from all users in a channel."""
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT m.role, m.content, u.display_name FROM messages m
                LEFT JOIN users u ON m.user_id=u.user_id
                WHERE m.channel_id=? AND (m.bot_name=? OR m.bot_name IS NULL) ORDER BY m.ts DESC LIMIT ?
            """, (channel_id,self.bot_name,limit)) as cur:
                rows = await cur.fetchall()
        return [{"role":r[0],"content":r[1],"name":r[2] or "?"} for r in reversed(rows)]

    async def add_message(self, user_id: int, channel_id: int, role: str, content: str):
        async with aiosqlite.connect(DB_PATH) as db:
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

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()
        return [row[0] for row in rows if row and row[0]]

    async def reset_user(self, user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM inside_jokes WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM user_topics WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM memory_bank WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM consequence_marks WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM relationship_milestones WHERE scope LIKE ?", (f"{self.bot_name}:user:{user_id}%",))
            await db.execute("DELETE FROM scene_state WHERE channel_id=?", (user_id,))
            await db.execute("""UPDATE users SET mood=0,affection=0,trust=0,anger_level=0,affection_ever_maxed=0,anger_repair_active=0,
                anger_repair_kind=NULL,anger_repair_instruction=NULL,anger_repair_requirement=NULL,anger_aftercare_messages=0,rival_id=NULL,grudge_nick=NULL,
                affection_nick=NULL,message_count=0,milestone_last=0,slow_burn=0,slow_burn_fired=0,
                drift_score=0,memory_summary=NULL,last_statement=NULL,style_profile=NULL,emotional_arc='guarded',
                conflict_open=0,conflict_summary=NULL,last_conflict_ts=0,repair_progress=0,
                callback_memory=NULL,callback_ts=0,repair_count=0
                WHERE user_id=?""", (user_id,))
            await db.commit()
        async with aiosqlite.connect(self.shared_db_path) as db:
            await db.execute("DELETE FROM shared_inside_jokes WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM user_bot_attention WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM hidden_achievements WHERE scope LIKE ?", (f"%user:{user_id}",))
            await db.execute("DELETE FROM relationship_milestones WHERE scope LIKE ?", (f"%user:{user_id}",))
            await db.commit()

    async def reset_user_for_rebuild(self, user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM user_topics WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM memory_bank WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM consequence_marks WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM relationship_milestones WHERE scope LIKE ?", (f"{self.bot_name}:user:{user_id}%",))
            await db.execute("DELETE FROM scene_state WHERE channel_id=?", (user_id,))
            await db.execute(
                """UPDATE users SET mood=0,affection=0,trust=0,anger_level=0,affection_ever_maxed=0,anger_repair_active=0,
                anger_repair_kind=NULL,anger_repair_instruction=NULL,anger_repair_requirement=NULL,anger_aftercare_messages=0,rival_id=NULL,grudge_nick=NULL,
                affection_nick=NULL,message_count=0,milestone_last=0,slow_burn=0,slow_burn_fired=0,
                drift_score=0,memory_summary=NULL,last_statement=NULL,style_profile=NULL,emotional_arc='guarded',
                conflict_open=0,conflict_summary=NULL,last_conflict_ts=0,repair_progress=0,
                callback_memory=NULL,callback_ts=0,repair_count=0
                WHERE user_id=?""",
                (user_id,),
            )
            await db.commit()
        async with aiosqlite.connect(self.shared_db_path) as db:
            await db.execute("DELETE FROM user_bot_attention WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM relationship_milestones WHERE scope LIKE ?", (f"%user:{user_id}",))
            await db.commit()

    async def get_face_profile(self, profile_key: str = "owner_face") -> dict | None:
        async with aiosqlite.connect(self.shared_db_path) as db:
            async with db.execute(
                "SELECT owner_user_id,label,profile_json,updated_ts FROM face_profiles WHERE profile_key=?",
                (profile_key,),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        profile = {}
        if row[2]:
            try:
                profile = json.loads(row[2]) or {}
            except Exception:
                profile = {}
        profile["profile_key"] = profile_key
        profile["owner_user_id"] = row[0] or 0
        profile["label"] = row[1] or ""
        profile["updated_ts"] = row[3] or 0
        profile["sample_count"] = int(profile.get("sample_count") or len(profile.get("templates") or []))
        return profile

    async def save_face_profile(
        self,
        profile_key: str,
        owner_user_id: int,
        label: str,
        profile: dict,
    ):
        payload = json.dumps(profile or {})
        async with aiosqlite.connect(self.shared_db_path) as db:
            await db.execute(
                "INSERT INTO face_profiles (profile_key,owner_user_id,label,profile_json,updated_ts) VALUES (?,?,?,?,?) "
                "ON CONFLICT(profile_key) DO UPDATE SET owner_user_id=excluded.owner_user_id,label=excluded.label,"
                "profile_json=excluded.profile_json,updated_ts=excluded.updated_ts",
                (profile_key, owner_user_id, label[:120], payload, time.time()),
            )
            await db.commit()

    async def delete_face_profile(self, profile_key: str = "owner_face"):
        async with aiosqlite.connect(self.shared_db_path) as db:
            await db.execute("DELETE FROM face_profiles WHERE profile_key=?", (profile_key,))
            await db.commit()

    # ── Mute (in-memory) ──────────────────────────────────────────────────────
    async def block_dm_user(self, user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO blocked_dm_users (user_id,created_ts) VALUES (?,?) "
                "ON CONFLICT(user_id) DO NOTHING",
                (user_id, time.time()),
            )
            await db.commit()

    async def unblock_dm_user(self, user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM blocked_dm_users WHERE user_id=?", (user_id,))
            await db.commit()

    async def is_dm_user_blocked(self, user_id: int) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT 1 FROM blocked_dm_users WHERE user_id=?", (user_id,)) as cur:
                return bool(await cur.fetchone())

    async def list_dm_users(self, limit: int = 25) -> list[dict]:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                """
                SELECT
                    u.user_id,
                    COALESCE(NULLIF(u.display_name,''), u.username, CAST(u.user_id AS TEXT)) AS name,
                    COUNT(m.id) AS msg_count,
                    EXISTS(SELECT 1 FROM blocked_dm_users b WHERE b.user_id=u.user_id) AS blocked
                FROM users u
                LEFT JOIN messages m
                    ON m.user_id=u.user_id
                   AND m.channel_id=u.user_id
                   AND (m.bot_name=? OR m.bot_name IS NULL)
                GROUP BY u.user_id, u.display_name, u.username
                HAVING COUNT(m.id) > 0 OR blocked
                ORDER BY msg_count DESC, u.last_seen DESC
                LIMIT ?
                """,
                (self.bot_name, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [
            {
                "user_id": int(row[0] or 0),
                "name": row[1] or str(row[0]),
                "msg_count": int(row[2] or 0),
                "blocked": bool(row[3]),
            }
            for row in rows
        ]

    async def block_channel(self, channel_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO blocked_channels (channel_id,created_ts) VALUES (?,?) "
                "ON CONFLICT(channel_id) DO NOTHING",
                (channel_id, time.time()),
            )
            await db.commit()

    async def unblock_channel(self, channel_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM blocked_channels WHERE channel_id=?", (channel_id,))
            await db.commit()

    async def is_channel_blocked(self, channel_id: int) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT 1 FROM blocked_channels WHERE channel_id=?", (channel_id,)) as cur:
                return bool(await cur.fetchone())

    async def list_blocked_channels(self, limit: int = 25) -> list[int]:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT channel_id FROM blocked_channels ORDER BY created_ts DESC LIMIT ?",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
        return [int(row[0]) for row in rows if row and row[0] is not None]

    async def block_user(self, user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO blocked_users (user_id, blocked_ts) VALUES (?, ?)",
                (user_id, time.time()))
            await db.commit()

    async def unblock_user(self, user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM blocked_users WHERE user_id=?", (user_id,))
            await db.commit()

    async def get_blocked_users(self) -> set[int]:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT user_id FROM blocked_users") as cur:
                rows = await cur.fetchall()
        return {r[0] for r in rows}

    async def is_blocked(self, user_id: int) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT 1 FROM blocked_users WHERE user_id=?", (user_id,)) as cur:
                return await cur.fetchone() is not None

    async def get_user_logs(self, user_id: int, limit: int = 200) -> list[dict]:
        """Get full conversation log for a user across all channels, newest last."""
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT role, content, ts FROM messages
                WHERE user_id=? AND (bot_name=? OR bot_name IS NULL)
                ORDER BY ts ASC LIMIT ?
            """, (user_id, self.bot_name, limit)) as cur:
                rows = await cur.fetchall()
        return [{"role": r[0], "content": r[1], "ts": r[2]} for r in rows]

    async def ban_channel(self, channel_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO banned_channels (channel_id, banned_ts) VALUES (?, ?)",
                (channel_id, time.time()))
            await db.commit()

    async def unban_channel(self, channel_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM banned_channels WHERE channel_id=?", (channel_id,))
            await db.commit()

    async def get_banned_channels(self) -> set[int]:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT channel_id FROM banned_channels") as cur:
                rows = await cur.fetchall()
        return {r[0] for r in rows}

    async def get_most_active_channel(self, exclude_channels: set[int] | None = None, only_channels: set[int] | None = None) -> int | None:
        """Get the channel_id where the bot has been most active recently."""
        exclude = exclude_channels or set()
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT channel_id, COUNT(*) as cnt FROM messages WHERE role='assistant' AND (bot_name=? OR bot_name IS NULL) GROUP BY channel_id ORDER BY cnt DESC LIMIT 50",
                (self.bot_name,)
            ) as cur:
                rows = await cur.fetchall()
        for r in rows:
            if r[0] and r[0] not in exclude:
                if only_channels is not None and r[0] not in only_channels:
                    continue
                return r[0]
        return None

    async def set_muted(self, user_id: int, seconds: int = 600):
        expires_ts = time.time() + max(0, seconds)
        Memory._muted[user_id] = expires_ts
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO muted_users (user_id,expires_ts,created_ts) VALUES (?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET expires_ts=excluded.expires_ts, created_ts=excluded.created_ts",
                (user_id, expires_ts, time.time()),
            )
            await db.commit()

    async def is_user_muted(self, user_id: int) -> bool:
        now = time.time()
        exp = Memory._muted.get(user_id, 0)
        if exp:
            if now > exp:
                Memory._muted.pop(user_id, None)
            else:
                return True
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT expires_ts FROM muted_users WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone()
            if not row:
                return False
            expires_ts = float(row[0] or 0)
            if expires_ts <= now:
                await db.execute("DELETE FROM muted_users WHERE user_id=?", (user_id,))
                await db.commit()
                return False
            Memory._muted[user_id] = expires_ts
            return True

    async def clear_muted(self, user_id: int):
        Memory._muted.pop(user_id, None)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM muted_users WHERE user_id=?", (user_id,))
            await db.commit()

    def mute_user(self, user_id: int, seconds: int = 600):
        Memory._muted[user_id] = time.time() + seconds

    def is_muted(self, user_id: int) -> bool:
        exp = Memory._muted.get(user_id, 0)
        if exp and time.time() > exp:
            del Memory._muted[user_id]; return False
        return bool(exp)

    def unmute_user(self, user_id: int):
        Memory._muted.pop(user_id, None)

    # ── Trivia ────────────────────────────────────────────────────────────────
    async def update_trivia(self, user_id: int, correct: bool):
        async with aiosqlite.connect(DB_PATH) as db:
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
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO active_trivia (channel_id,asker_id,question,answer,source_note,asked_ts) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(channel_id) DO UPDATE SET asker_id=excluded.asker_id, question=excluded.question, "
                "answer=excluded.answer, source_note=excluded.source_note, asked_ts=excluded.asked_ts",
                (channel_id, asker_id, question[:500], answer[:240], source_note[:240], time.time()),
            )
            await db.commit()

    async def get_active_trivia(self, channel_id: int) -> dict | None:
        async with aiosqlite.connect(DB_PATH) as db:
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
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM active_trivia WHERE channel_id=?", (channel_id,))
            await db.commit()

    async def get_trivia_stats(self, user_id: int) -> dict:
        async with aiosqlite.connect(DB_PATH) as db:
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
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT INTO roast_battles (channel_id,user1_id,user2_id,round,scores,active,ts) VALUES (?,?,?,0,'{}',1,?)",
                (channel_id,u1,u2,time.time()))
            await db.commit()
            return cur.lastrowid

    async def get_active_roast(self, channel_id: int) -> dict | None:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT id,user1_id,user2_id,round,scores FROM roast_battles WHERE channel_id=? AND active=1", (channel_id,)
            ) as cur:
                row = await cur.fetchone()
                if not row: return None
                return {"id":row[0],"user1":row[1],"user2":row[2],"round":row[3],"scores":json.loads(row[4])}

    async def end_roast_battle(self, battle_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE roast_battles SET active=0 WHERE id=?", (battle_id,))
            await db.commit()

    async def increment_roast_round(self, battle_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE roast_battles SET round=round+1 WHERE id=?", (battle_id,))
            await db.commit()

    async def award_roast_round(self, battle_id: int, winner_user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
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

    # ── Game scores (persistent leaderboard) ──────────────────────────────────
    async def record_game_result(self, user_id: int, game_type: str, won: bool | None, points: int = 0):
        """Record a game result. won=True/False/None(draw)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO game_scores (user_id, game_type, wins, losses, draws, total_points) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(user_id, game_type) DO UPDATE SET "
                "wins=wins+excluded.wins, losses=losses+excluded.losses, "
                "draws=draws+excluded.draws, total_points=total_points+excluded.total_points",
                (user_id, game_type,
                 1 if won is True else 0,
                 1 if won is False else 0,
                 1 if won is None else 0,
                 points),
            )
            await db.commit()

    async def get_game_stats(self, user_id: int) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT game_type, wins, losses, draws, total_points FROM game_scores WHERE user_id=? ORDER BY wins DESC",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [{"game": r[0], "wins": r[1], "losses": r[2], "draws": r[3], "points": r[4]} for r in rows]

    async def get_leaderboard(self, game_type: str = None, limit: int = 10) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            if game_type:
                async with db.execute(
                    "SELECT user_id, wins, losses, draws, total_points FROM game_scores WHERE game_type=? ORDER BY wins DESC, total_points DESC LIMIT ?",
                    (game_type, limit),
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with db.execute(
                    "SELECT user_id, SUM(wins) as w, SUM(losses) as l, SUM(draws) as d, SUM(total_points) as p "
                    "FROM game_scores GROUP BY user_id ORDER BY w DESC, p DESC LIMIT ?",
                    (limit,),
                ) as cur:
                    rows = await cur.fetchall()
        return [{"user_id": r[0], "wins": r[1], "losses": r[2], "draws": r[3], "points": r[4]} for r in rows]

    # ── RPG System ─────────────────────────────────────────────────────────────
    async def get_rpg_state(self, user_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT current_boss, current_round, boss_points, total_points, bosses_beaten, scenario_data, active, "
                "world_type, char_type, element "
                "FROM rpg_state WHERE user_id=?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        return {
            "current_boss": row[0], "current_round": row[1], "boss_points": row[2],
            "total_points": row[3], "bosses_beaten": json.loads(row[4] or "[]"),
            "scenario_data": json.loads(row[5] or "{}"), "active": bool(row[6]),
            "world_type": row[7] or "", "char_type": row[8] or "", "element": row[9] or "",
        }

    async def save_rpg_state(self, user_id: int, **kwargs):
        allowed_fields = {
            "current_boss", "current_round", "boss_points", "total_points",
            "bosses_beaten", "scenario_data", "active", "world_type",
            "char_type", "element",
        }
        updates = {key: value for key, value in kwargs.items() if key in allowed_fields}
        now = time.time()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO rpg_state (user_id, last_updated) VALUES (?,?)",
                (user_id, now),
            )
            sets, vals = [], []
            for key, value in updates.items():
                if key in ("bosses_beaten", "scenario_data"):
                    value = json.dumps(value)
                elif key == "active":
                    value = 1 if value else 0
                sets.append(f"{key}=?")
                vals.append(value)
            sets.append("last_updated=?")
            vals.extend((now, user_id))
            await db.execute(f"UPDATE rpg_state SET {','.join(sets)} WHERE user_id=?", vals)
            await db.commit()

    async def reset_rpg(self, user_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM rpg_state WHERE user_id=?", (user_id,))
            await db.commit()

    async def award_rpg_medal(self, user_id: int, total_points: int):
        async with aiosqlite.connect(self.db_path) as db:
            now = time.time()
            await db.execute(
                "INSERT INTO rpg_medals "
                "(user_id, completions, best_points, first_clear_ts, last_clear_ts) "
                "VALUES (?,1,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "completions=rpg_medals.completions+1, "
                "best_points=MAX(rpg_medals.best_points, excluded.best_points), "
                "last_clear_ts=excluded.last_clear_ts",
                (user_id, total_points, now, now),
            )
            await db.commit()

    async def get_rpg_medal(self, user_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT completions, best_points, first_clear_ts FROM rpg_medals WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        return {"completions": row[0], "best_points": row[1], "first_clear_ts": row[2]}

    async def get_rpg_leaderboard(self, limit: int = 15) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT user_id, completions, best_points, first_clear_ts FROM rpg_medals ORDER BY completions DESC, best_points DESC LIMIT ?",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
        return [{"user_id": r[0], "completions": r[1], "best_points": r[2], "first_clear_ts": r[3]} for r in rows]

    async def list_inside_jokes(self, user_id: int, limit: int = 6) -> list[str]:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT joke FROM inside_jokes WHERE user_id=? ORDER BY ts DESC LIMIT ?",
                (user_id, limit),
            ) as cur:
                return [row[0] for row in await cur.fetchall() if row and row[0]]

    # ── Reminders ─────────────────────────────────────────────────────────────
    async def add_reminder(self, user_id: int, channel_id: int, reminder: str, due_ts: float):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO reminders (user_id,channel_id,reminder,due_ts) VALUES (?,?,?,?)", (user_id,channel_id,reminder,due_ts))
            await db.commit()

    async def get_due_reminders(self) -> list[dict]:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT id,user_id,channel_id,reminder FROM reminders WHERE due_ts<=? AND done=0", (time.time(),)) as cur:
                rows = await cur.fetchall()
            if rows:
                await db.execute(f"UPDATE reminders SET done=1 WHERE id IN ({','.join('?'*len(rows))})", [r[0] for r in rows])
                await db.commit()
        return [{"id":r[0],"user_id":r[1],"channel_id":r[2],"reminder":r[3]} for r in rows]

    # ── Channels ──────────────────────────────────────────────────────────────
    async def track_channel(self, channel_id: int, guild_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO channels (channel_id,guild_id,last_active) VALUES (?,?,?) ON CONFLICT(channel_id) DO UPDATE SET last_active=excluded.last_active", (channel_id,guild_id,time.time()))
            await db.commit()

    async def get_active_channels(self) -> list[tuple]:
        cutoff = time.time()-86400*3
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT channel_id,guild_id FROM channels WHERE last_active>?", (cutoff,)) as cur:
                return await cur.fetchall()

    # ── Cooldowns ─────────────────────────────────────────────────────────────
    async def can_proactive(self, channel_id: int, cooldown: int = 3600) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT last_sent FROM proactive_cooldown WHERE channel_id=?", (channel_id,)) as cur:
                row = await cur.fetchone(); return not row or (time.time()-row[0])>=cooldown

    async def set_proactive_sent(self, channel_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO proactive_cooldown (channel_id,last_sent) VALUES (?,?) ON CONFLICT(channel_id) DO UPDATE SET last_sent=excluded.last_sent", (channel_id,time.time()))
            await db.commit()

    async def can_lore_drop(self, channel_id: int, cooldown: int = 21600) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT last_sent FROM lore_cooldown WHERE channel_id=?", (channel_id,)) as cur:
                row = await cur.fetchone(); return not row or (time.time()-row[0])>=cooldown

    async def set_lore_sent(self, channel_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO lore_cooldown (channel_id,last_sent) VALUES (?,?) ON CONFLICT(channel_id) DO UPDATE SET last_sent=excluded.last_sent", (channel_id,time.time()))
            await db.commit()

    async def can_starter(self, channel_id: int, cooldown: int = 10800) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT last_sent FROM starter_cooldown WHERE channel_id=?", (channel_id,)) as cur:
                row = await cur.fetchone(); return not row or (time.time()-row[0])>=cooldown

    async def set_starter_sent(self, channel_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO starter_cooldown (channel_id,last_sent) VALUES (?,?) ON CONFLICT(channel_id) DO UPDATE SET last_sent=excluded.last_sent", (channel_id,time.time()))
            await db.commit()

    # ── Romance helpers ───────────────────────────────────────────────────────
    async def get_romance_users(self) -> list[int]:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT user_id FROM users WHERE romance_mode=1 AND proactive=1") as cur:
                return [r[0] for r in await cur.fetchall()]

    async def get_user_last_channel(self, user_id: int) -> int | None:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT channel_id FROM messages WHERE user_id=? AND (bot_name=? OR bot_name IS NULL) ORDER BY ts DESC LIMIT 1", (user_id,self.bot_name)) as cur:
                row = await cur.fetchone(); return row[0] if row else None

    # ── DM cooldown ───────────────────────────────────────────────────────────
    # Birthday helpers
    async def set_birthday(self, user_id: int, month: int, day: int, year: int | None = None):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET birth_month=?,birth_day=?,birth_year=? WHERE user_id=?",
                (int(month), int(day), int(year or 0), user_id),
            )
            await db.commit()

    async def clear_birthday(self, user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET birth_month=0,birth_day=0,birth_year=0,birthday_last_sent_year=0 WHERE user_id=?",
                (user_id,),
            )
            await db.commit()

    async def get_birthdays_due(self) -> list[dict]:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT user_id,display_name,timezone_name,birth_month,birth_day,birth_year,birthday_last_sent_year
                FROM users
                WHERE birth_month>0 AND birth_day>0
            """) as cur:
                rows = await cur.fetchall()

        due: list[dict] = []
        for user_id, display_name, timezone_name, month, day, birth_year, last_sent_year in rows:
            try:
                tz = ZoneInfo(timezone_name or "America/Los_Angeles")
            except Exception:
                tz = ZoneInfo("America/Los_Angeles")
            now = datetime.now(tz)
            if int(month) != now.month or int(day) != now.day:
                continue
            if int(last_sent_year or 0) >= now.year:
                continue
            if not (8 <= now.hour <= 22):
                continue
            age = None
            if int(birth_year or 0) > 0 and int(birth_year) <= now.year:
                age = now.year - int(birth_year)
            due.append({
                "user_id": int(user_id),
                "display_name": display_name or "someone",
                "timezone_name": timezone_name or "America/Los_Angeles",
                "birth_month": int(month),
                "birth_day": int(day),
                "birth_year": int(birth_year or 0),
                "age": age,
                "year": now.year,
            })
        return due

    async def mark_birthday_sent(self, user_id: int, year: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET birthday_last_sent_year=? WHERE user_id=?",
                (int(year), user_id),
            )
            await db.commit()

    async def can_dm_user(self, user_id: int, cooldown: int = 7200) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT last_sent FROM dm_cooldown WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone(); return not row or (time.time()-row[0])>=cooldown

    async def set_dm_sent(self, user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO dm_cooldown (user_id,last_sent) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET last_sent=excluded.last_sent", (user_id,time.time()))
            await db.commit()

    async def get_dm_eligible_users(self) -> list[dict]:
        cutoff = time.time()-86400*7
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT u.user_id,u.display_name,u.romance_mode,u.nsfw_mode
                FROM users u WHERE u.allow_dms=1 AND u.last_seen>?
                  AND EXISTS (SELECT 1 FROM messages m WHERE m.user_id=u.user_id AND (m.bot_name=? OR m.bot_name IS NULL))
            """, (cutoff,self.bot_name)) as cur:
                rows = await cur.fetchall()
        return [{"user_id":r[0],"display_name":r[1],"romance_mode":bool(r[2]),"nsfw_mode":bool(r[3])} for r in rows]

    # ── Stats ─────────────────────────────────────────────────────────────────
    async def get_stats(self, user_id: int) -> dict:
        async with aiosqlite.connect(DB_PATH) as db:
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
