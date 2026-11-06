"""Small transactional migration runner for bot-local and shared SQLite scopes."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
from typing import Awaitable, Callable

import aiosqlite


log = logging.getLogger(__name__)
MigrationAction = Callable[[aiosqlite.Connection, str], Awaitable[None]]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: MigrationAction


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    rows = await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
    return {str(row[1]) for row in rows}


async def ensure_columns(db, table: str, definitions: dict[str, str]) -> None:
    """Add only absent, statically allowlisted columns for legacy databases."""
    present = await _columns(db, table)
    for name, definition in definitions.items():
        if name not in present:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            present.add(name)


USER_COLUMNS = {
    "username": "TEXT", "display_name": "TEXT",
    "romance_mode": "INTEGER DEFAULT 0", "unrestricted_mode": "INTEGER DEFAULT 0",
    "proactive": "INTEGER DEFAULT 1", "allow_dms": "INTEGER DEFAULT 1",
    "timezone_name": "TEXT DEFAULT 'America/Los_Angeles'",
    "quiet_hours_start": "INTEGER DEFAULT 23", "quiet_hours_end": "INTEGER DEFAULT 8",
    "dm_frequency_hours": "INTEGER DEFAULT 8",
    "recent_activity_grace_minutes": "INTEGER DEFAULT 45",
    "mood": "INTEGER DEFAULT 0", "affection": "INTEGER DEFAULT 0",
    "trust": "INTEGER DEFAULT 0", "rival_id": "INTEGER DEFAULT NULL",
    "grudge_nick": "TEXT DEFAULT NULL", "affection_nick": "TEXT DEFAULT NULL",
    "message_count": "INTEGER DEFAULT 0", "milestone_last": "INTEGER DEFAULT 0",
    "first_seen": "REAL DEFAULT 0", "last_seen": "REAL DEFAULT 0",
    "last_active": "REAL DEFAULT 0", "greeted_today": "INTEGER DEFAULT 0",
    "anniversary_last": "INTEGER DEFAULT 0", "slow_burn": "INTEGER DEFAULT 0",
    "slow_burn_day": "INTEGER DEFAULT 0", "slow_burn_fired": "INTEGER DEFAULT 0",
    "drift_score": "INTEGER DEFAULT 0", "memory_summary": "TEXT DEFAULT NULL",
    "summary_msg_count": "INTEGER DEFAULT 0", "last_statement": "TEXT DEFAULT NULL",
    "style_profile": "TEXT DEFAULT NULL", "emotional_arc": "TEXT DEFAULT 'guarded'",
    "conflict_open": "INTEGER DEFAULT 0", "conflict_summary": "TEXT DEFAULT NULL",
    "last_conflict_ts": "REAL DEFAULT 0", "repair_progress": "INTEGER DEFAULT 0",
    "callback_memory": "TEXT DEFAULT NULL", "callback_ts": "REAL DEFAULT 0",
    "repair_count": "INTEGER DEFAULT 0", "weather_location": "TEXT DEFAULT NULL",
}

PREFERENCE_COLUMNS = {
    "grudge_enabled": "INTEGER DEFAULT 1", "lullaby_enabled": "INTEGER DEFAULT 0",
    "lullaby_start_hour": "INTEGER DEFAULT 23",
    "home_presence_enabled": "INTEGER DEFAULT 0",
    "home_actions_enabled": "INTEGER DEFAULT 0",
    "home_alarms_enabled": "INTEGER DEFAULT 0", "voice_output_target": "TEXT DEFAULT ''",
    "chaos_parody": "INTEGER DEFAULT 0", "chaos_ping": "INTEGER DEFAULT 0",
    "chaos_gossip": "INTEGER DEFAULT 0", "chaos_court": "INTEGER DEFAULT 0",
    "vc_party_features_enabled": "INTEGER DEFAULT 0",
    "mockingbird_enabled": "INTEGER DEFAULT 0",
    "interrogation_game_enabled": "INTEGER DEFAULT 0",
    "escape_room_enabled": "INTEGER DEFAULT 0",
    "voice_reactions_enabled": "INTEGER DEFAULT 0",
}

BASE_PREFERENCE_COLUMNS = {
    "voice_enabled": "INTEGER DEFAULT 1",
    "utility_mode": "INTEGER DEFAULT 1",
    "duo_autoplay": "INTEGER DEFAULT 1",
    "rp_depth": "TEXT DEFAULT 'medium'",
}


async def ensure_feature_preference_schema(path: str) -> None:
    """Bootstrap the centrally-defined preference schema for standalone features.

    Some feature services are intentionally usable in isolation (including recovery
    tools and their tests), before ``Memory.init`` has run.  They still use this one
    allowlisted schema definition rather than carrying their own ALTER statements.
    A later full initialization records the formal migration versions normally.
    """
    async with aiosqlite.connect(path, timeout=15) as db:
        await db.execute("PRAGMA busy_timeout=15000")
        try:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "CREATE TABLE IF NOT EXISTS user_preferences "
                "(user_id INTEGER PRIMARY KEY)"
            )
            await ensure_columns(
                db,
                "user_preferences",
                {**BASE_PREFERENCE_COLUMNS, **PREFERENCE_COLUMNS},
            )
            await db.commit()
        except asyncio.CancelledError:
            await db.rollback()
            raise
        except (aiosqlite.OperationalError, aiosqlite.IntegrityError):
            await db.rollback()
            raise
        except Exception:
            await db.rollback()
            raise


async def _local_relationship_columns(db, _bot_name):
    await ensure_columns(db, "users", USER_COLUMNS)


async def _local_feature_preferences(db, _bot_name):
    await ensure_columns(
        db, "user_preferences", {**BASE_PREFERENCE_COLUMNS, **PREFERENCE_COLUMNS}
    )


async def _local_message_scene_and_privacy(db, bot_name):
    await ensure_columns(db, "messages", {"bot_name": "TEXT DEFAULT NULL"})
    await ensure_columns(db, "scene_state", {"important_prop": "TEXT DEFAULT NULL"})
    await db.execute("UPDATE messages SET bot_name=? WHERE bot_name IS NULL", (bot_name,))
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_user_channel_role_ts "
        "ON messages(user_id,channel_id,role,ts DESC)"
    )
    await db.execute("""
        CREATE TABLE IF NOT EXISTS privacy_deletion_jobs(
            request_id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,status TEXT NOT NULL,
            stages_json TEXT NOT NULL DEFAULT '{}',created_at REAL NOT NULL,
            updated_at REAL NOT NULL,completed_at REAL DEFAULT 0,
            last_error_category TEXT DEFAULT NULL,last_error_stage TEXT DEFAULT NULL
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_privacy_jobs_status "
        "ON privacy_deletion_jobs(status,updated_at)"
    )


async def _local_unrestricted_mode(db, _bot_name):
    """Copy the retired preference once and leave its column unused."""
    columns = await _columns(db, "users")
    legacy_column = "ns" + "fw_mode"
    await ensure_columns(db, "users", {"unrestricted_mode": "INTEGER DEFAULT 0"})
    if legacy_column in columns:
        await db.execute(
            f"UPDATE users SET unrestricted_mode=COALESCE({legacy_column},0)"
        )


async def _local_adult_confirmation(db, _bot_name):
    """Unrestricted mode needs a stored 18+ confirmation. Nobody is grandfathered in: an existing preference stays
    stored but has no effect until that person confirms."""
    await ensure_columns(db, "users", {"adult_confirmed": "INTEGER DEFAULT 0"})


async def _shared_duo_columns(db, _bot_name):
    await ensure_columns(db, "duo_sessions", {
        "initiator_user_id": "INTEGER DEFAULT 0",
        "awaiting_bot": "TEXT DEFAULT NULL",
        "autoplay_remaining": "INTEGER DEFAULT 0",
        "next_autoplay_ts": "REAL DEFAULT 0",
    })


from restoration_store import migrate as _restore_character_storage
from birthday_commands import migrate as _restore_birthday_storage
from world_archive import migrate as _restore_world_archive

LOCAL_MIGRATIONS = (
    Migration(1, "legacy_relationship_columns", _local_relationship_columns),
    Migration(2, "feature_preference_columns", _local_feature_preferences),
    Migration(3, "message_scene_and_privacy_ledger", _local_message_scene_and_privacy),
    Migration(4, "unrestricted_mode_preference", _local_unrestricted_mode),
    Migration(5, "restore_scoped_harbinger_campaigns", _restore_character_storage),
    Migration(6, "restore_user_birthdays", _restore_birthday_storage),
    Migration(7, "adult_confirmation", _local_adult_confirmation),
)
SHARED_MIGRATIONS = (
    Migration(1, "duo_session_columns", _shared_duo_columns),
    Migration(2, "restore_shared_world_archive", _restore_world_archive),
)


async def run_migrations(db, scope: str, migrations, *, bot_name: str) -> int:
    """Apply each version atomically; compatible legacy schemas bootstrap safely."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations(
            scope TEXT NOT NULL,version INTEGER NOT NULL,name TEXT NOT NULL,
            applied_at REAL NOT NULL,PRIMARY KEY(scope,version)
        )
    """)
    await db.commit()
    for migration in migrations:
        started = time.monotonic()
        try:
            await db.execute("BEGIN IMMEDIATE")
            applied = await (await db.execute(
                "SELECT 1 FROM schema_migrations WHERE scope=? AND version=?",
                (scope, migration.version),
            )).fetchone()
            if not applied:
                await migration.apply(db, bot_name)
                await db.execute(
                    "INSERT INTO schema_migrations(scope,version,name,applied_at) VALUES(?,?,?,?)",
                    (scope, migration.version, migration.name, time.time()),
                )
            await db.commit()
        except asyncio.CancelledError:
            await db.rollback()
            raise
        except (aiosqlite.OperationalError, aiosqlite.IntegrityError):
            await db.rollback()
            log.exception("database migration failed", extra={
                "db_scope": scope, "migration_version": migration.version,
                "migration_name": migration.name,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            })
            raise
        except Exception:
            await db.rollback()
            log.exception("database migration failed", extra={
                "db_scope": scope, "migration_version": migration.version,
                "migration_name": migration.name,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            })
            raise
        else:
            log.info("database migration ready", extra={
                "db_scope": scope, "migration_version": migration.version,
                "migration_name": migration.name,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            })
    return migrations[-1].version if migrations else 0


async def migration_status(path: str, scope: str, migrations) -> dict:
    current = migrations[-1].version if migrations else 0
    try:
        async with aiosqlite.connect(path, timeout=15) as db:
            exists = await (await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            )).fetchone()
            version = 0
            if exists:
                row = await (await db.execute(
                    "SELECT COALESCE(MAX(version),0) FROM schema_migrations WHERE scope=?",
                    (scope,),
                )).fetchone()
                version = int(row[0] or 0)
        return {"scope": scope, "version": version, "current": current,
                "pending": max(0, current - version), "error": ""}
    except (aiosqlite.OperationalError, aiosqlite.IntegrityError) as exc:
        return {"scope": scope, "version": 0, "current": current,
                "pending": current, "error": type(exc).__name__}
