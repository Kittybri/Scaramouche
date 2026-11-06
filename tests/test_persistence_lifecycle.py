import asyncio
import sqlite3
import time
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from db_migrations import LOCAL_MIGRATIONS, SHARED_MIGRATIONS, Migration, run_migrations
from memory import Memory
from privacy_deletion import PrivacyDeletionCoordinator


def run(coro):
    return asyncio.run(coro)


def columns(path, table):
    with sqlite3.connect(path) as db:
        return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def test_fresh_install_records_independent_current_schema_versions(tmp_path):
    memory = Memory(
        "scaramouche",
        db_path=str(tmp_path / "local.db"),
        shared_db_path=str(tmp_path / "shared.db"),
    )
    run(memory.init())

    status = run(memory.schema_status())
    assert status["local"] == {
        "scope": "local", "version": 7, "current": 7, "pending": 0, "error": ""
    }
    assert status["shared"] == {
        "scope": "shared", "version": 2, "current": 2, "pending": 0, "error": ""
    }
    assert {"bot_name"} <= columns(memory.db_path, "messages")
    user_columns = columns(memory.db_path, "users")
    assert "unrestricted_mode" in user_columns
    assert "ns" + "fw_mode" not in user_columns
    assert {"important_prop"} <= columns(memory.db_path, "scene_state")
    assert {"chaos_court", "voice_reactions_enabled"} <= columns(
        memory.db_path, "user_preferences"
    )
    assert {"awaiting_bot", "autoplay_remaining"} <= columns(
        memory.shared_db_path, "duo_sessions"
    )
    with sqlite3.connect(memory.db_path) as db:
        assert db.execute(
            "SELECT version,name FROM schema_migrations WHERE scope='local' ORDER BY version"
        ).fetchall() == [(m.version, m.name) for m in LOCAL_MIGRATIONS]
    with sqlite3.connect(memory.shared_db_path) as db:
        assert db.execute(
            "SELECT version,name FROM schema_migrations WHERE scope='shared' ORDER BY version"
        ).fetchall() == [(m.version, m.name) for m in SHARED_MIGRATIONS]


def test_current_schema_without_metadata_bootstraps_without_data_loss(tmp_path):
    memory = Memory("scaramouche", str(tmp_path / "local.db"), str(tmp_path / "shared.db"))
    run(memory.init())
    run(memory.upsert_user(44, "legacy", "Legacy User"))
    run(memory.add_message(44, 91, "user", "keep this history"))
    with sqlite3.connect(memory.db_path) as db:
        db.execute("DROP TABLE schema_migrations")
    with sqlite3.connect(memory.shared_db_path) as db:
        db.execute("DROP TABLE schema_migrations")

    reopened = Memory("scaramouche", memory.db_path, memory.shared_db_path)
    run(reopened.init())
    assert run(reopened.get_user(44))["display_name"] == "Legacy User"
    assert run(reopened.get_history(44, 91))[0]["content"] == "keep this history"
    assert run(reopened.schema_status())["local"]["version"] == 7  # 7: adult_confirmation


@pytest.mark.parametrize("legacy_value", [0, 1])
def test_retired_mode_value_is_preserved_once_for_unrestricted(tmp_path, legacy_value):
    memory = Memory("scaramouche", str(tmp_path / "local.db"), str(tmp_path / "shared.db"))
    run(memory.init())
    run(memory.upsert_user(71, "legacy", "Legacy"))
    legacy_column = "ns" + "fw_mode"
    with sqlite3.connect(memory.db_path) as db:
        db.execute(f"ALTER TABLE users ADD COLUMN {legacy_column} INTEGER DEFAULT 0")
        db.execute(
            f"UPDATE users SET {legacy_column}=? WHERE user_id=71", (legacy_value,)
        )
        db.execute(
            "UPDATE users SET unrestricted_mode=? WHERE user_id=71",
            (int(not legacy_value),),
        )
        db.execute("DELETE FROM schema_migrations WHERE scope='local' AND version=4")
    reopened = Memory("scaramouche", memory.db_path, memory.shared_db_path)
    run(reopened.init())
    assert run(reopened.get_user(71))["unrestricted_mode"] is bool(legacy_value)
    assert legacy_column in columns(memory.db_path, "users")
    run(reopened.set_mode(71, "unrestricted_mode", not bool(legacy_value)))
    run(reopened.init())
    assert run(reopened.get_user(71))["unrestricted_mode"] is not bool(legacy_value)
    assert run(reopened.schema_status())["local"]["version"] == 7  # 7: adult_confirmation
    assert run(reopened.schema_status())["shared"]["version"] == 2


def test_representative_pre_duo_shared_schema_migrates_in_place(tmp_path):
    shared = str(tmp_path / "shared.db")
    with sqlite3.connect(shared) as db:
        db.execute(
            "CREATE TABLE duo_sessions(channel_id INTEGER PRIMARY KEY,mode TEXT,topic TEXT,"
            "initiator_bot TEXT,last_speaker TEXT,expires_ts REAL,updated_ts REAL)"
        )
        db.execute(
            "INSERT INTO duo_sessions VALUES(8,'argue','old topic','scaramouche',"
            "'wanderer',?,?)",
            (time.time() + 3600, time.time()),
        )
    memory = Memory("scaramouche", str(tmp_path / "local.db"), shared)
    run(memory.init())
    session = run(memory.get_duo_session(8))
    assert session["topic"] == "old topic"
    assert session["initiator_user_id"] == 0
    assert session["autoplay_remaining"] == 0


def test_preference_table_from_early_schema_keeps_rows_and_gains_columns(tmp_path):
    local = str(tmp_path / "local.db")
    with sqlite3.connect(local) as db:
        db.execute(
            "CREATE TABLE user_preferences(user_id INTEGER PRIMARY KEY,"
            "voice_enabled INTEGER DEFAULT 1)"
        )
        db.execute("INSERT INTO user_preferences(user_id,voice_enabled) VALUES(19,0)")
    memory = Memory("scaramouche", local, str(tmp_path / "shared.db"))
    run(memory.init())
    with sqlite3.connect(local) as db:
        row = db.execute(
            "SELECT voice_enabled,utility_mode,chaos_parody,voice_reactions_enabled "
            "FROM user_preferences WHERE user_id=19"
        ).fetchone()
    assert row == (0, 1, 0, 0)


def test_migration_rerun_is_metadata_idempotent(tmp_path):
    memory = Memory("scaramouche", str(tmp_path / "local.db"), str(tmp_path / "shared.db"))
    run(memory.init())
    with sqlite3.connect(memory.db_path) as db:
        before = db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    run(memory.init())
    with sqlite3.connect(memory.db_path) as db:
        after = db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert before == after == len(LOCAL_MIGRATIONS)


def test_failed_migration_rolls_back_and_can_retry(tmp_path):
    path = str(tmp_path / "migration.db")

    async def exercise():
        async with aiosqlite.connect(path) as db:
            await db.execute("CREATE TABLE durable(id INTEGER PRIMARY KEY,value TEXT)")
            await db.execute("INSERT INTO durable VALUES(1,'survives')")
            await db.commit()

            async def fail(connection, _bot_name):
                await connection.execute("CREATE TABLE partial(value TEXT)")
                await connection.execute("INSERT INTO partial VALUES('not committed')")
                raise aiosqlite.OperationalError("injected migration failure")

            with pytest.raises(aiosqlite.OperationalError):
                await run_migrations(
                    db, "test", (Migration(1, "fails", fail),), bot_name="test"
                )
            version = await (await db.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE scope='test'"
            )).fetchone()
            partial = await (await db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='partial'"
            )).fetchone()
            durable = await (await db.execute("SELECT value FROM durable WHERE id=1")).fetchone()
            assert version[0] == 0
            assert partial[0] == 0
            assert durable[0] == "survives"

            async def succeed(connection, _bot_name):
                await connection.execute("CREATE TABLE recovered(value TEXT)")

            await run_migrations(
                db, "test", (Migration(1, "retry", succeed),), bot_name="test"
            )
            assert (await (await db.execute(
                "SELECT name FROM schema_migrations WHERE scope='test' AND version=1"
            )).fetchone())[0] == "retry"

    run(exercise())


def test_two_initializers_share_migration_history_without_lock_failure(tmp_path):
    local = str(tmp_path / "local.db")
    shared = str(tmp_path / "shared.db")

    async def initialize_both():
        first = Memory("scaramouche", local, shared)
        second = Memory("wanderer", local, shared)
        await asyncio.gather(first.init(), second.init())

    run(initialize_both())
    with sqlite3.connect(local) as db:
        rows = db.execute(
            "SELECT version,COUNT(*) FROM schema_migrations WHERE scope='local' GROUP BY version"
        ).fetchall()
        assert rows == [(1, 1), (2, 1), (3, 1), (4, 1), (5, 1), (6, 1), (7, 1)]
    with sqlite3.connect(shared) as db:
        rows = db.execute(
            "SELECT version,COUNT(*) FROM schema_migrations WHERE scope='shared' GROUP BY version"
        ).fetchall()
        assert rows == [(1, 1), (2, 1)]
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_privacy_deletion_retries_only_incomplete_stage_after_restart(tmp_path):
    memory = Memory("scaramouche", str(tmp_path / "local.db"), str(tmp_path / "shared.db"))
    run(memory.init())
    local = AsyncMock()
    companion = AsyncMock(side_effect=RuntimeError("offline"))
    coordinator = PrivacyDeletionCoordinator(
        memory.db_path, {"local": local, "companion": companion}
    )

    first = run(coordinator.run(22))
    assert first.status == "RETRYABLE"
    assert first.pending_stage == "companion"
    assert first.completed_stages == ("local",)
    assert run(coordinator.pending_count()) == 1
    assert run(coordinator.is_pending(22)) is True
    assert run(coordinator.is_pending(23)) is False

    resumed_companion = AsyncMock()
    restarted = PrivacyDeletionCoordinator(
        memory.db_path, {"local": AsyncMock(), "companion": resumed_companion}
    )
    second = run(restarted.run(22))
    assert second.complete
    assert second.request_id == first.request_id
    restarted.stages["local"].assert_not_awaited()
    resumed_companion.assert_awaited_once_with(22)
    assert run(restarted.pending_count()) == 0
    assert run(restarted.is_pending(22)) is False


def test_privacy_deletion_full_run_is_idempotent(tmp_path):
    memory = Memory("scaramouche", str(tmp_path / "local.db"), str(tmp_path / "shared.db"))
    run(memory.init())
    stages = {name: AsyncMock() for name in ("memory", "face", "voice")}
    coordinator = PrivacyDeletionCoordinator(memory.db_path, stages)
    assert run(coordinator.run(5)).complete
    assert run(coordinator.run(5)).complete
    for operation in stages.values():
        assert operation.await_count == 2


def test_reset_user_preserves_other_users_global_pair_and_guild_scene(tmp_path):
    memory = Memory("scaramouche", str(tmp_path / "local.db"), str(tmp_path / "shared.db"))
    run(memory.init())
    for uid in (1, 10):
        run(memory.upsert_user(uid, f"user{uid}", f"User {uid}"))
        run(memory.add_message(uid, uid, "user", f"message {uid}"))
    with sqlite3.connect(memory.db_path) as db:
        db.execute("INSERT INTO memory_bank(user_id,kind,memory,ts) VALUES(1,'manual','A',1)")
        db.execute("INSERT INTO memory_bank(user_id,kind,memory,ts) VALUES(10,'manual','B',1)")
        db.execute("INSERT INTO relationship_milestones VALUES('scaramouche:user:1','a','A',1)")
        db.execute("INSERT INTO relationship_milestones VALUES('scaramouche:user:10','b','B',1)")
        db.execute("INSERT INTO scene_state(channel_id,situation) VALUES(1,'A DM')")
        db.execute("INSERT INTO scene_state(channel_id,situation) VALUES(10,'B DM')")
        db.execute("INSERT INTO scene_state(channel_id,situation) VALUES(999,'guild scene')")
    with sqlite3.connect(memory.shared_db_path) as db:
        db.execute(
            "INSERT OR REPLACE INTO bot_relationships(pair_key,shared_history) "
            "VALUES('scaramouche:wanderer','global pair history')"
        )
        db.execute("INSERT INTO relationship_milestones VALUES('shared:user:1','a','A',1)")
        db.execute("INSERT INTO relationship_milestones VALUES('shared:user:10','b','B',1)")
        db.execute(
            "INSERT INTO duo_sessions(channel_id,mode,initiator_user_id) VALUES(101,'argue',1)"
        )
        db.execute(
            "INSERT INTO duo_sessions(channel_id,mode,initiator_user_id) VALUES(110,'argue',10)"
        )

    run(memory.reset_user(1))
    assert run(memory.get_user(1)) is None
    assert run(memory.get_user(10))["display_name"] == "User 10"
    with sqlite3.connect(memory.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM memory_bank WHERE user_id=1").fetchone()[0] == 0
        assert db.execute("SELECT memory FROM memory_bank WHERE user_id=10").fetchone()[0] == "B"
        assert db.execute("SELECT situation FROM scene_state WHERE channel_id=1").fetchone() is None
        assert db.execute("SELECT situation FROM scene_state WHERE channel_id=10").fetchone()[0] == "B DM"
        assert db.execute("SELECT COUNT(*) FROM relationship_milestones WHERE scope='scaramouche:user:10'").fetchone()[0] == 1
        assert db.execute("SELECT situation FROM scene_state WHERE channel_id=999").fetchone()[0] == "guild scene"
    with sqlite3.connect(memory.shared_db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM duo_sessions WHERE channel_id=101").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM duo_sessions WHERE channel_id=110").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM relationship_milestones WHERE scope='shared:user:10'").fetchone()[0] == 1
        assert db.execute(
            "SELECT shared_history FROM bot_relationships WHERE pair_key='scaramouche:wanderer'"
        ).fetchone()[0] == "global pair history"


def test_reset_user_shared_removes_compatible_wanderer_user_scopes(tmp_path):
    memory = Memory("scaramouche", str(tmp_path / "local.db"), str(tmp_path / "shared.db"))
    run(memory.init())
    with sqlite3.connect(memory.shared_db_path) as db:
        db.executescript("""
            CREATE TABLE user_bot_attention(user_id INTEGER,bot_name TEXT);
            CREATE TABLE face_profiles(profile_key TEXT,owner_user_id INTEGER);
            CREATE TABLE shared_event_memories(event_key TEXT,channel_id INTEGER);
            CREATE TABLE shared_evidence_locker(evidence_key TEXT,owner_user_id INTEGER,channel_id INTEGER);
            CREATE TABLE interbot_private_opinions(scope TEXT,subject_key TEXT);
        """)
        for uid in (1, 10):
            db.execute("INSERT INTO user_bot_attention VALUES(?, 'wanderer')", (uid,))
            db.execute("INSERT INTO hidden_achievements(scope,achievement_key) VALUES(?, 'a')", (f'user:{uid}',))
            db.execute("INSERT INTO shared_world_entities(entity_key,owner_user_id,channel_id) VALUES(?,?,?)", (f'e{uid}', uid, uid))
            db.execute("INSERT INTO shared_world_cases(case_key,channel_id) VALUES(?,?)", (f'c{uid}', uid))
            db.execute("INSERT INTO face_profiles VALUES(?,?)", (f'f{uid}', uid))
            db.execute("INSERT INTO shared_event_memories VALUES(?,?)", (f'm{uid}', uid))
            db.execute("INSERT INTO shared_evidence_locker VALUES(?,?,?)", (f'v{uid}', uid, uid))
            db.execute("INSERT INTO interbot_private_opinions VALUES(?,?)", (f'user:{uid}', str(uid)))

    run(memory.reset_user_shared(1))
    with sqlite3.connect(memory.shared_db_path) as db:
        for table in (
            "user_bot_attention", "hidden_achievements", "shared_world_entities",
            "shared_world_cases", "face_profiles", "shared_event_memories",
            "shared_evidence_locker", "interbot_private_opinions",
        ):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


def test_completed_deletion_ledger_prunes_on_startup_after_bounded_retention(tmp_path):
    memory = Memory("scaramouche", str(tmp_path / "local.db"), str(tmp_path / "shared.db"))
    run(memory.init())
    coordinator = PrivacyDeletionCoordinator(memory.db_path, {"memory": AsyncMock()})
    result = run(coordinator.run(7))
    with sqlite3.connect(memory.db_path) as db:
        db.execute(
            "UPDATE privacy_deletion_jobs SET completed_at=? WHERE request_id=?",
            (time.time() - 31 * 86400, result.request_id),
        )
    run(memory.init())
    with sqlite3.connect(memory.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM privacy_deletion_jobs WHERE request_id=?",
            (result.request_id,),
        ).fetchone()[0] == 0
