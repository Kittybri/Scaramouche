"""Persistent, bounded self-model storage.

This is a software state model, not a claim of consciousness.  It stores the
bot's modeled moods, beliefs, goals, reflections, contradictions, and bounded
autonomous decisions independently from user relationship memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import asynccontextmanager
import asyncio
import json
import logging
import os
import time
from typing import Any, Iterable

import aiosqlite

from agent_config import AgentConfig, CONFIG


log = logging.getLogger(__name__)
MOOD_DIMENSIONS = (
    "irritation", "curiosity", "attachment", "boredom",
    "suspicion", "amusement", "defensiveness", "concern",
)
GOAL_STATUSES = {"active", "paused", "completed", "failed", "abandoned"}


def _clamp(value: float, low: float = 0.0, high: float = 10.0) -> float:
    return max(low, min(high, float(value)))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass
class SelfContext:
    dimensions: dict[str, float]
    mood_cause: str
    concerns: list[str]
    interests: list[str]
    frustrations: list[str]
    attachment_tendencies: list[str]
    beliefs: list[dict[str, Any]]
    goals: list[dict[str, Any]]
    contradictions: list[dict[str, Any]]
    reflections: list[dict[str, Any]]

    def prompt_fragment(self) -> str:
        """Return a compact private prompt fragment without raw evidence."""
        active = {k: round(v, 1) for k, v in self.dimensions.items() if v >= 2.5}
        lines = [f"SELF_STATE:{_json(active) or '{}'}"]
        if self.mood_cause:
            lines.append(f"SELF_STATE_CAUSE:{self.mood_cause[:120]}")
        if self.concerns:
            lines.append("CURRENT_CONCERNS:" + "; ".join(self.concerns[:3]))
        if self.beliefs:
            lines.append(
                "SELF_BELIEFS:" + "; ".join(
                    f"{belief['belief'][:100]} (confidence {float(belief['confidence']):.2f})"
                    for belief in self.beliefs[:3]
                )
            )
        if self.goals:
            lines.append("ACTIVE_INTENTIONS:" + "; ".join(g["description"][:100] for g in self.goals[:3]))
        if self.contradictions:
            lines.append("SELF_CONTRADICTIONS:" + "; ".join(c["summary"][:100] for c in self.contradictions[:2]))
        if self.reflections:
            lines.append(
                "RELEVANT_SELF_REFLECTIONS:" + "; ".join(
                    reflection["interpretation"][:180] for reflection in self.reflections[:2]
                )
            )
        lines.append(
            "INTERPRETATION_RULE: these are private modeled tendencies, not facts to recite. "
            "Let them alter cadence, attention, reluctance, and priorities subtly."
        )
        return "\n".join(lines)


class SelfModelStore:
    def __init__(self, db_path: str, config: AgentConfig = CONFIG):
        self.db_path = os.fspath(db_path)
        self.config = config

    @asynccontextmanager
    async def _connect(self):
        db = await aiosqlite.connect(self.db_path, timeout=15.0)
        try:
            await db.execute("PRAGMA busy_timeout=15000")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute("PRAGMA foreign_keys=ON")
            yield db
        finally:
            await db.close()

    async def _prepare(self, db: aiosqlite.Connection) -> None:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=15000")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA foreign_keys=ON")

    async def init(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(parent, exist_ok=True)
        async with self._connect() as db:
            await self._prepare(db)
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS self_state (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    irritation REAL NOT NULL DEFAULT 0,
                    curiosity REAL NOT NULL DEFAULT 2,
                    attachment REAL NOT NULL DEFAULT 0,
                    boredom REAL NOT NULL DEFAULT 1,
                    suspicion REAL NOT NULL DEFAULT 2,
                    amusement REAL NOT NULL DEFAULT 1,
                    defensiveness REAL NOT NULL DEFAULT 2,
                    concern REAL NOT NULL DEFAULT 0,
                    mood_cause TEXT NOT NULL DEFAULT '',
                    concerns_json TEXT NOT NULL DEFAULT '[]',
                    interests_json TEXT NOT NULL DEFAULT '[]',
                    frustrations_json TEXT NOT NULL DEFAULT '[]',
                    attachment_json TEXT NOT NULL DEFAULT '[]',
                    last_decay_ts REAL NOT NULL DEFAULT 0,
                    updated_ts REAL NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO self_state(singleton_id,last_decay_ts,updated_ts)
                VALUES(1, strftime('%s','now'), strftime('%s','now'));

                CREATE TABLE IF NOT EXISTS self_reflections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    trigger TEXT NOT NULL,
                    observation TEXT NOT NULL,
                    interpretation TEXT NOT NULL,
                    emotional_effect_json TEXT NOT NULL DEFAULT '{}',
                    importance INTEGER NOT NULL DEFAULT 1 CHECK(importance BETWEEN 1 AND 10),
                    confidence REAL NOT NULL DEFAULT .5 CHECK(confidence BETWEEN 0 AND 1),
                    related_user_id INTEGER,
                    related_goal_id INTEGER,
                    related_belief_id INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_self_reflections_ts ON self_reflections(ts DESC);

                CREATE TABLE IF NOT EXISTS self_beliefs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    belief TEXT NOT NULL,
                    scope_user_id INTEGER,
                    confidence REAL NOT NULL DEFAULT .5 CHECK(confidence BETWEEN 0 AND 1),
                    created_ts REAL NOT NULL,
                    last_reinforced_ts REAL NOT NULL,
                    supporting_weight REAL NOT NULL DEFAULT 0,
                    contradicting_weight REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    UNIQUE(belief, scope_user_id)
                );
                CREATE TABLE IF NOT EXISTS self_belief_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    belief_id INTEGER NOT NULL REFERENCES self_beliefs(id) ON DELETE CASCADE,
                    ts REAL NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('support','contradict')),
                    weight REAL NOT NULL,
                    summary TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_belief_evidence_belief ON self_belief_evidence(belief_id, ts DESC);

                CREATE TABLE IF NOT EXISTS self_goals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    description TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'conversation',
                    priority INTEGER NOT NULL DEFAULT 5 CHECK(priority BETWEEN 1 AND 10),
                    status TEXT NOT NULL DEFAULT 'active',
                    related_user_id INTEGER,
                    progress REAL NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 1),
                    created_ts REAL NOT NULL,
                    updated_ts REAL NOT NULL,
                    expires_ts REAL,
                    completion_condition TEXT NOT NULL DEFAULT '',
                    failure_condition TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'heuristic',
                    dedupe_key TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_self_goals_status ON self_goals(status, priority DESC, updated_ts DESC);

                CREATE TABLE IF NOT EXISTS self_contradictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    belief_id INTEGER REFERENCES self_beliefs(id) ON DELETE CASCADE,
                    related_user_id INTEGER,
                    summary TEXT NOT NULL,
                    pressure REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'open',
                    first_seen_ts REAL NOT NULL,
                    last_seen_ts REAL NOT NULL,
                    UNIQUE(belief_id, related_user_id, summary)
                );

                CREATE TABLE IF NOT EXISTS autonomous_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    action_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    related_user_id INTEGER,
                    channel_id INTEGER,
                    goal_id INTEGER,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    error_category TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_autonomous_actions_ts ON autonomous_actions(ts DESC);

                CREATE TABLE IF NOT EXISTS self_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    event_type TEXT NOT NULL,
                    importance INTEGER NOT NULL DEFAULT 1,
                    related_user_id INTEGER,
                    summary TEXT NOT NULL,
                    processed INTEGER NOT NULL DEFAULT 0,
                    dedupe_key TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_self_events_pending ON self_events(processed, importance DESC, ts);

                CREATE TABLE IF NOT EXISTS autonomous_budget (
                    bucket TEXT PRIMARY KEY,
                    count INTEGER NOT NULL DEFAULT 0,
                    reset_ts REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_runtime (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_ts REAL NOT NULL
                );
                """
            )
            # SQLite UNIQUE constraints consider NULL values distinct. Merge
            # any legacy global-belief duplicates, then enforce uniqueness with
            # a normalized user scope so contradiction pressure accumulates.
            await db.execute(
                """UPDATE self_contradictions AS kept SET
                       pressure=MIN(20,(
                           SELECT SUM(other.pressure) FROM self_contradictions other
                           WHERE other.belief_id=kept.belief_id
                             AND COALESCE(other.related_user_id,-1)=COALESCE(kept.related_user_id,-1)
                             AND other.summary=kept.summary
                       )),
                       first_seen_ts=(
                           SELECT MIN(other.first_seen_ts) FROM self_contradictions other
                           WHERE other.belief_id=kept.belief_id
                             AND COALESCE(other.related_user_id,-1)=COALESCE(kept.related_user_id,-1)
                             AND other.summary=kept.summary
                       ),
                       last_seen_ts=(
                           SELECT MAX(other.last_seen_ts) FROM self_contradictions other
                           WHERE other.belief_id=kept.belief_id
                             AND COALESCE(other.related_user_id,-1)=COALESCE(kept.related_user_id,-1)
                             AND other.summary=kept.summary
                       )
                   WHERE kept.id IN (
                       SELECT MAX(id) FROM self_contradictions
                       GROUP BY belief_id,COALESCE(related_user_id,-1),summary
                   )"""
            )
            await db.execute(
                """DELETE FROM self_contradictions WHERE id NOT IN (
                       SELECT MAX(id) FROM self_contradictions
                       GROUP BY belief_id,COALESCE(related_user_id,-1),summary
                   )"""
            )
            await db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_self_contradictions_unique
                   ON self_contradictions(belief_id,COALESCE(related_user_id,-1),summary)"""
            )
            await db.commit()

    async def get_state(self) -> dict[str, Any]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM self_state WHERE singleton_id=1") as cur:
                row = await cur.fetchone()
        if not row:
            raise RuntimeError("self_state was not initialized")
        result = dict(row)
        for source, target in (
            ("concerns_json", "concerns"), ("interests_json", "interests"),
            ("frustrations_json", "frustrations"), ("attachment_json", "attachment_tendencies"),
        ):
            try:
                result[target] = json.loads(result.pop(source) or "[]")
            except (TypeError, json.JSONDecodeError):
                result[target] = []
        result["dimensions"] = {key: float(result[key]) for key in MOOD_DIMENSIONS}
        return result

    async def set_state_lists(self, **values: Iterable[str]) -> None:
        columns = {
            "concerns": "concerns_json", "interests": "interests_json",
            "frustrations": "frustrations_json", "attachment_tendencies": "attachment_json",
        }
        updates, params = [], []
        for key, items in values.items():
            if key not in columns:
                continue
            cleaned = [str(item).strip()[:160] for item in items if str(item).strip()][:8]
            updates.append(f"{columns[key]}=?")
            params.append(_json(cleaned))
        if not updates:
            return
        updates.append("updated_ts=?")
        params.extend([time.time(), 1])
        async with self._connect() as db:
            await db.execute(f"UPDATE self_state SET {', '.join(updates)} WHERE singleton_id=?", params)
            await db.commit()

    async def apply_mood_event(self, cause: str, deltas: dict[str, float]) -> dict[str, float]:
        allowed = {k: float(v) for k, v in deltas.items() if k in MOOD_DIMENSIONS and v}
        if not allowed:
            return (await self.get_state())["dimensions"]
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT " + ",".join(MOOD_DIMENSIONS) + " FROM self_state WHERE singleton_id=1"
            ) as cur:
                row = await cur.fetchone()
            updated = {key: _clamp(row[i] + allowed.get(key, 0)) for i, key in enumerate(MOOD_DIMENSIONS)}
            assignments = ",".join(f"{key}=?" for key in MOOD_DIMENSIONS)
            await db.execute(
                f"UPDATE self_state SET {assignments},mood_cause=?,updated_ts=? WHERE singleton_id=1",
                [updated[k] for k in MOOD_DIMENSIONS] + [cause[:200], time.time()],
            )
            await db.commit()
        return updated

    async def decay_mood(self, now: float | None = None) -> bool:
        now = now or time.time()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT " + ",".join(MOOD_DIMENSIONS) + ",last_decay_ts FROM self_state WHERE singleton_id=1"
            ) as cur:
                row = await cur.fetchone()
            if not row:
                await db.rollback()
                return False
            elapsed_hours = max(0.0, now - float(row[-1] or now)) / 3600.0
            if elapsed_hours < 0.1:
                await db.rollback()
                return False
            amount = self.config.mood_decay_per_hour * elapsed_hours
            baselines = {"curiosity": 2.0, "suspicion": 2.0, "defensiveness": 2.0, "boredom": 1.0, "amusement": 1.0}
            updated = {}
            for i, key in enumerate(MOOD_DIMENSIONS):
                current = float(row[i])
                baseline = baselines.get(key, 0.0)
                updated[key] = max(baseline, current - amount) if current >= baseline else min(baseline, current + amount)
            assignments = ",".join(f"{key}=?" for key in MOOD_DIMENSIONS)
            await db.execute(
                f"UPDATE self_state SET {assignments},last_decay_ts=?,updated_ts=? WHERE singleton_id=1",
                [updated[k] for k in MOOD_DIMENSIONS] + [now, now],
            )
            await db.commit()
        return True

    async def add_reflection(
        self, trigger: str, observation: str, interpretation: str, *,
        emotional_effect: dict[str, float] | None = None, importance: int = 5,
        confidence: float = .6, related_user_id: int | None = None,
        related_goal_id: int | None = None, related_belief_id: int | None = None,
    ) -> int:
        now = time.time()
        async with self._connect() as db:
            cur = await db.execute(
                """INSERT INTO self_reflections
                   (ts,trigger,observation,interpretation,emotional_effect_json,importance,confidence,
                    related_user_id,related_goal_id,related_belief_id) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (now, trigger[:80], observation[:1000], interpretation[:1000], _json(emotional_effect or {}),
                 max(1, min(10, int(importance))), _clamp(confidence, 0, 1), related_user_id,
                 related_goal_id, related_belief_id),
            )
            reflection_id = cur.lastrowid
            await db.execute(
                "DELETE FROM self_reflections WHERE id NOT IN (SELECT id FROM self_reflections ORDER BY ts DESC LIMIT ?)",
                (self.config.max_reflections,),
            )
            await db.commit()
        if emotional_effect:
            await self.apply_mood_event(f"reflection:{trigger}", emotional_effect)
        return int(reflection_id)

    async def recent_reflections(self, limit: int = 10, *, user_id: int | None = None) -> list[dict[str, Any]]:
        clause = ""
        params: list[Any] = []
        if user_id is not None:
            clause = "WHERE related_user_id IS NULL OR related_user_id=?"
            params.append(user_id)
        params.append(max(1, min(limit, 50)))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM self_reflections {clause} ORDER BY ts DESC LIMIT ?", params
            ) as cur:
                return [dict(row) for row in await cur.fetchall()]

    async def add_belief(self, belief: str, *, confidence: float = .55, scope_user_id: int | None = None) -> int:
        now = time.time()
        belief = belief.strip()[:500]
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT id FROM self_beliefs WHERE belief=? AND scope_user_id IS ? LIMIT 1",
                (belief, scope_user_id),
            ) as cur:
                existing = await cur.fetchone()
            if existing:
                await db.commit()
                return int(existing[0])
            await db.execute(
                """INSERT INTO self_beliefs
                   (belief,scope_user_id,confidence,created_ts,last_reinforced_ts)
                   VALUES(?,?,?,?,?)""",
                (belief, scope_user_id, _clamp(confidence, 0, 1), now, now),
            )
            async with db.execute(
                "SELECT id FROM self_beliefs WHERE belief=? AND scope_user_id IS ?",
                (belief, scope_user_id),
            ) as cur:
                row = await cur.fetchone()
            await db.commit()
        return int(row[0])

    async def add_belief_evidence(self, belief_id: int, kind: str, weight: float, summary: str) -> dict[str, float]:
        if kind not in {"support", "contradict"}:
            raise ValueError("evidence kind must be support or contradict")
        weight = max(0.0, min(10.0, float(weight)))
        now = time.time()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT confidence,supporting_weight,contradicting_weight,scope_user_id,belief FROM self_beliefs WHERE id=?",
                (belief_id,),
            ) as cur:
                row = await cur.fetchone()
            if not row:
                await db.rollback()
                raise KeyError(f"belief {belief_id} not found")
            confidence, supporting, contradicting, user_id, belief = row
            if kind == "support":
                supporting += weight
                confidence = min(1.0, confidence + weight * .025)
            else:
                contradicting += weight
                confidence = max(0.05, confidence - weight * .02)
            await db.execute(
                "INSERT INTO self_belief_evidence(belief_id,ts,kind,weight,summary) VALUES(?,?,?,?,?)",
                (belief_id, now, kind, weight, summary[:500]),
            )
            await db.execute(
                "DELETE FROM self_belief_evidence WHERE belief_id=? AND id NOT IN "
                "(SELECT id FROM self_belief_evidence WHERE belief_id=? ORDER BY ts DESC LIMIT 50)",
                (belief_id, belief_id),
            )
            await db.execute(
                "UPDATE self_beliefs SET confidence=?,supporting_weight=?,contradicting_weight=?,last_reinforced_ts=? WHERE id=?",
                (confidence, supporting, contradicting, now, belief_id),
            )
            if kind == "contradict":
                contradiction_summary = f"Behavior conflicts with belief: {belief}"
                await db.execute(
                    """INSERT INTO self_contradictions
                       (belief_id,related_user_id,summary,pressure,status,first_seen_ts,last_seen_ts)
                       VALUES(?,?,?,?, 'open',?,?)
                       ON CONFLICT DO UPDATE SET
                         pressure=MIN(20,pressure+excluded.pressure),last_seen_ts=excluded.last_seen_ts,status='open'""",
                    (belief_id, user_id, contradiction_summary[:500], weight, now, now),
                )
                async with db.execute(
                    "SELECT pressure FROM self_contradictions WHERE belief_id=? AND related_user_id IS ? AND summary=?",
                    (belief_id, user_id, contradiction_summary[:500]),
                ) as cur:
                    contradiction = await cur.fetchone()
                if contradiction and float(contradiction[0]) >= self.config.contradiction_threshold:
                    dedupe_key = f"belief_contradiction:{belief_id}"
                    await db.execute(
                        """INSERT INTO self_events(ts,event_type,importance,related_user_id,summary,processed,dedupe_key)
                           SELECT ?, 'belief_contradiction', 8, ?, ?, 0, ?
                           WHERE NOT EXISTS (
                               SELECT 1 FROM self_events WHERE dedupe_key=? AND processed=0
                           )""",
                        (now, user_id, contradiction_summary[:500], dedupe_key, dedupe_key),
                    )
                    await self._prune_events(db)
            await db.commit()
        return {"confidence": float(confidence), "supporting_weight": float(supporting), "contradicting_weight": float(contradicting)}

    async def list_beliefs(self, limit: int = 10, *, user_id: int | None = None) -> list[dict[str, Any]]:
        clause, params = "status='active'", []
        if user_id is not None:
            clause += " AND (scope_user_id IS NULL OR scope_user_id=?)"
            params.append(user_id)
        params.append(max(1, min(limit, 50)))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM self_beliefs WHERE {clause} ORDER BY confidence DESC,last_reinforced_ts DESC LIMIT ?", params
            ) as cur:
                return [dict(row) for row in await cur.fetchall()]

    async def add_goal(
        self, description: str, *, category: str = "conversation", priority: int = 5,
        related_user_id: int | None = None, expires_ts: float | None = None,
        completion_condition: str = "", failure_condition: str = "", reason: str = "",
        source: str = "heuristic", dedupe_key: str = "",
    ) -> int | None:
        description = description.strip()[:600]
        if not description:
            raise ValueError("goal description is required")
        priority = max(1, min(10, int(priority)))
        dedupe_key = (dedupe_key.strip() or f"{category}:{related_user_id}:{description.lower()[:160]}")[:240]
        now = time.time()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT id FROM self_goals WHERE status='active' AND dedupe_key=?", (dedupe_key,)) as cur:
                existing = await cur.fetchone()
            if existing:
                await db.execute("UPDATE self_goals SET priority=MAX(priority,?),updated_ts=? WHERE id=?", (priority, now, existing[0]))
                await db.commit()
                return int(existing[0])
            async with db.execute("SELECT COUNT(*) FROM self_goals WHERE status='active'") as cur:
                count = int((await cur.fetchone())[0])
            if count >= self.config.max_active_goals:
                async with db.execute(
                    "SELECT id,priority FROM self_goals WHERE status='active' ORDER BY priority ASC,updated_ts ASC LIMIT 1"
                ) as cur:
                    weakest = await cur.fetchone()
                if not weakest or int(weakest[1]) >= priority:
                    await db.rollback()
                    return None
                await db.execute("UPDATE self_goals SET status='abandoned',updated_ts=? WHERE id=?", (now, weakest[0]))
            cur = await db.execute(
                """INSERT INTO self_goals
                   (description,category,priority,status,related_user_id,progress,created_ts,updated_ts,expires_ts,
                    completion_condition,failure_condition,reason,source,dedupe_key)
                   VALUES(?,?,?,'active',?,0,?,?,?,?,?,?,?,?)""",
                (description, category[:50], priority, related_user_id, now, now,
                 expires_ts, completion_condition[:400], failure_condition[:400], reason[:400], source[:50], dedupe_key),
            )
            goal_id = cur.lastrowid
            await self._prune_goal_history(db)
            await db.commit()
        return int(goal_id)

    async def update_goal(self, goal_id: int, *, progress: float | None = None, status: str | None = None) -> bool:
        if status is not None and status not in GOAL_STATUSES:
            raise ValueError(f"invalid goal status: {status}")
        updates, params = [], []
        if progress is not None:
            progress = _clamp(progress, 0, 1)
            updates.append("progress=?")
            params.append(progress)
            if progress >= 1 and status is None:
                status = "completed"
        if status is not None:
            updates.append("status=?")
            params.append(status)
        if not updates:
            return False
        updates.append("updated_ts=?")
        params.extend([time.time(), goal_id])
        async with self._connect() as db:
            cur = await db.execute(f"UPDATE self_goals SET {', '.join(updates)} WHERE id=?", params)
            await self._prune_goal_history(db)
            await db.commit()
            return cur.rowcount > 0

    async def complete_goals(self, *, related_user_id: int, category: str | None = None,
                             dedupe_key: str | None = None) -> int:
        """Complete matching active goals after a verified lifecycle event."""
        clauses = ["status='active'", "related_user_id=?"]
        params: list[Any] = [related_user_id]
        if category:
            clauses.append("category=?")
            params.append(category)
        if dedupe_key:
            clauses.append("dedupe_key=?")
            params.append(dedupe_key)
        now = time.time()
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE self_goals SET status='completed',progress=1,updated_ts=? WHERE " + " AND ".join(clauses),
                [now, *params],
            )
            await self._prune_goal_history(db)
            await db.commit()
            return cur.rowcount

    async def expire_goals(self, now: float | None = None) -> int:
        now = now or time.time()
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE self_goals SET status='failed',updated_ts=? WHERE status='active' AND expires_ts IS NOT NULL AND expires_ts<=?",
                (now, now),
            )
            await self._prune_goal_history(db)
            await db.commit()
            return cur.rowcount

    async def _prune_goal_history(self, db: aiosqlite.Connection) -> None:
        await db.execute(
            """DELETE FROM self_goals
               WHERE status!='active' AND id NOT IN (
                   SELECT id FROM self_goals WHERE status!='active'
                   ORDER BY updated_ts DESC LIMIT ?
               )""",
            (self.config.max_goal_history,),
        )

    async def list_goals(self, status: str = "active", limit: int = 10,
                         *, user_id: int | None = None) -> list[dict[str, Any]]:
        clause = "status=?"
        params: list[Any] = [status]
        if user_id is not None:
            clause += " AND (related_user_id IS NULL OR related_user_id=?)"
            params.append(user_id)
        params.append(max(1, min(limit, 50)))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM self_goals WHERE {clause} ORDER BY priority DESC,updated_ts DESC LIMIT ?",
                params,
            ) as cur:
                return [dict(row) for row in await cur.fetchall()]

    async def list_contradictions(self, min_pressure: float = 0, limit: int = 10,
                                  *, user_id: int | None = None) -> list[dict[str, Any]]:
        clause = "status='open' AND pressure>=?"
        params: list[Any] = [min_pressure]
        if user_id is not None:
            clause += " AND (related_user_id IS NULL OR related_user_id=?)"
            params.append(user_id)
        params.append(max(1, min(limit, 50)))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM self_contradictions WHERE {clause} ORDER BY pressure DESC,last_seen_ts DESC LIMIT ?",
                params,
            ) as cur:
                return [dict(row) for row in await cur.fetchall()]

    async def record_event(self, event_type: str, summary: str, *, importance: int = 1,
                           related_user_id: int | None = None, dedupe_key: str | None = None) -> int:
        now = time.time()
        async with self._connect() as db:
            if dedupe_key:
                async with db.execute(
                    "SELECT id FROM self_events WHERE dedupe_key=? AND ts>?", (dedupe_key[:200], now - 3600)
                ) as cur:
                    duplicate = await cur.fetchone()
                if duplicate:
                    return int(duplicate[0])
            cur = await db.execute(
                "INSERT INTO self_events(ts,event_type,importance,related_user_id,summary,processed,dedupe_key) VALUES(?,?,?,?,?,0,?)",
                (now, event_type[:60], max(1, min(10, int(importance))), related_user_id, summary[:800], dedupe_key[:200] if dedupe_key else None),
            )
            await self._prune_events(db)
            await db.commit()
            return int(cur.lastrowid)

    async def _prune_events(self, db: aiosqlite.Connection) -> None:
        await db.execute(
            "DELETE FROM self_events WHERE processed=1 AND id NOT IN (SELECT id FROM self_events ORDER BY ts DESC LIMIT 500)"
        )
        # Low-importance events are useful for short-term diagnostics but do
        # not qualify for reflection. Keep only a bounded recent sample.
        await db.execute(
            """DELETE FROM self_events
               WHERE processed=0 AND importance<? AND id NOT IN (
                   SELECT id FROM self_events WHERE processed=0 AND importance<?
                   ORDER BY ts DESC LIMIT 250
               )""",
            (self.config.reflection_threshold, self.config.reflection_threshold),
        )
        await db.execute(
            """DELETE FROM self_events WHERE id NOT IN (
                   SELECT id FROM self_events
                   ORDER BY processed ASC,importance DESC,ts DESC LIMIT ?
               )""",
            (self.config.max_self_events,),
        )

    async def pending_events(self, minimum_importance: int = 1, limit: int = 20) -> list[dict[str, Any]]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM self_events WHERE processed=0 AND importance>=? ORDER BY importance DESC,ts ASC LIMIT ?",
                (minimum_importance, max(1, min(limit, 100))),
            ) as cur:
                return [dict(row) for row in await cur.fetchall()]

    async def mark_events_processed(self, event_ids: Iterable[int]) -> None:
        ids = [int(value) for value in event_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        async with self._connect() as db:
            await db.execute(f"UPDATE self_events SET processed=1 WHERE id IN ({placeholders})", ids)
            await db.commit()

    async def record_action(self, action_type: str, status: str, reason: str, *,
                            related_user_id: int | None = None, channel_id: int | None = None,
                            goal_id: int | None = None, details: dict[str, Any] | None = None,
                            error_category: str | None = None) -> int:
        async with self._connect() as db:
            cur = await db.execute(
                """INSERT INTO autonomous_actions
                   (ts,action_type,status,reason,related_user_id,channel_id,goal_id,details_json,error_category)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (time.time(), action_type, status, reason[:500], related_user_id, channel_id, goal_id,
                 _json(details or {}), error_category),
            )
            await db.execute(
                "DELETE FROM autonomous_actions WHERE id NOT IN (SELECT id FROM autonomous_actions ORDER BY ts DESC LIMIT 1000)"
            )
            await db.commit()
            return int(cur.lastrowid)

    async def reserve_action(self, action_type: str, reason: str, *, related_user_id: int | None = None,
                             channel_id: int | None = None, goal_id: int | None = None,
                             details: dict[str, Any] | None = None,
                             stale_after_seconds: int = 3_600) -> int | None:
        """Atomically reserve an external action so reconnects cannot duplicate it."""
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            now = time.time()
            await db.execute(
                """UPDATE autonomous_actions
                   SET status='failed',error_category='ReservationExpired'
                   WHERE action_type=? AND status='pending' AND ts<=?""",
                (action_type, now - max(60, int(stale_after_seconds))),
            )
            clauses = ["action_type=?", "status='pending'"]
            params: list[Any] = [action_type]
            if related_user_id is not None:
                clauses.append("related_user_id=?")
                params.append(related_user_id)
            if channel_id is not None:
                clauses.append("channel_id=?")
                params.append(channel_id)
            async with db.execute(
                "SELECT id FROM autonomous_actions WHERE " + " AND ".join(clauses) + " LIMIT 1", params
            ) as cur:
                if await cur.fetchone():
                    await db.rollback()
                    return None
            cur = await db.execute(
                """INSERT INTO autonomous_actions
                   (ts,action_type,status,reason,related_user_id,channel_id,goal_id,details_json,error_category)
                   VALUES(?,?,'pending',?,?,?,?,?,NULL)""",
                (now, action_type, reason[:500], related_user_id, channel_id, goal_id, _json(details or {})),
            )
            await db.execute(
                "DELETE FROM autonomous_actions WHERE id NOT IN (SELECT id FROM autonomous_actions ORDER BY ts DESC LIMIT 1000)"
            )
            await db.commit()
            return int(cur.lastrowid)

    async def finish_action(self, action_id: int, status: str, *,
                            details: dict[str, Any] | None = None,
                            error_category: str | None = None) -> bool:
        if status not in {"completed", "failed", "skipped"}:
            raise ValueError(f"invalid terminal action status: {status}")
        updates = ["status=?", "error_category=?"]
        params: list[Any] = [status, error_category]
        if details is not None:
            updates.append("details_json=?")
            params.append(_json(details))
        params.append(action_id)
        async with self._connect() as db:
            cur = await db.execute(
                f"UPDATE autonomous_actions SET {', '.join(updates)} WHERE id=? AND status='pending'", params
            )
            await db.commit()
            return cur.rowcount > 0

    async def action_on_cooldown(self, action_type: str, seconds: int, *, user_id: int | None = None,
                                 channel_id: int | None = None) -> bool:
        # A pending reservation is intentionally conservative: after a process
        # crash we would rather skip one message than send it twice.
        clauses = ["action_type=?", "status IN ('pending','completed')", "ts>?"]
        params: list[Any] = [action_type, time.time() - seconds]
        if user_id is not None:
            clauses.append("related_user_id=?")
            params.append(user_id)
        if channel_id is not None:
            clauses.append("channel_id=?")
            params.append(channel_id)
        async with self._connect() as db:
            async with db.execute(
                "SELECT 1 FROM autonomous_actions WHERE " + " AND ".join(clauses) + " LIMIT 1", params
            ) as cur:
                return bool(await cur.fetchone())

    async def consume_budget(self, *, now: float | None = None) -> bool:
        now = now or time.time()
        buckets = (
            (f"hour:{int(now // 3600)}", self.config.autonomous_calls_per_hour, (int(now // 3600) + 1) * 3600),
            (f"day:{int(now // 86400)}", self.config.autonomous_calls_per_day, (int(now // 86400) + 1) * 86400),
        )
        if any(limit <= 0 for _, limit, _ in buckets):
            return False
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            for key, limit, reset in buckets:
                async with db.execute("SELECT count FROM autonomous_budget WHERE bucket=?", (key,)) as cur:
                    row = await cur.fetchone()
                if row and int(row[0]) >= limit:
                    await db.rollback()
                    return False
            for key, _, reset in buckets:
                await db.execute(
                    """INSERT INTO autonomous_budget(bucket,count,reset_ts) VALUES(?,1,?)
                       ON CONFLICT(bucket) DO UPDATE SET count=count+1,reset_ts=excluded.reset_ts""",
                    (key, reset),
                )
            await db.execute("DELETE FROM autonomous_budget WHERE reset_ts<?", (now - 86400,))
            await db.commit()
        return True

    async def budget_status(self, now: float | None = None) -> dict[str, int]:
        now = now or time.time()
        keys = (f"hour:{int(now // 3600)}", f"day:{int(now // 86400)}")
        async with self._connect() as db:
            async with db.execute(
                "SELECT bucket,count FROM autonomous_budget WHERE bucket IN (?,?)", keys
            ) as cur:
                found = dict(await cur.fetchall())
        return {
            "hour_used": int(found.get(keys[0], 0)), "hour_limit": self.config.autonomous_calls_per_hour,
            "day_used": int(found.get(keys[1], 0)), "day_limit": self.config.autonomous_calls_per_day,
        }

    async def set_runtime_value(self, key: str, value: str | float | int) -> None:
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO agent_runtime(key,value,updated_ts) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_ts=excluded.updated_ts""",
                (key[:100], str(value)[:500], time.time()),
            )
            await db.commit()

    async def get_runtime_float(self, key: str, default: float = 0.0) -> float:
        async with self._connect() as db:
            async with db.execute("SELECT value FROM agent_runtime WHERE key=?", (key[:100],)) as cur:
                row = await cur.fetchone()
        try:
            return float(row[0]) if row else float(default)
        except (TypeError, ValueError):
            return float(default)

    async def context(self, user_id: int | None = None) -> SelfContext:
        state = await self.get_state()
        beliefs, goals, contradictions, reflections = await asyncio.gather(
            self.list_beliefs(5, user_id=user_id),
            self.list_goals(limit=5, user_id=user_id),
            self.list_contradictions(self.config.contradiction_threshold, 3, user_id=user_id),
            self.recent_reflections(3, user_id=user_id),
        )
        return SelfContext(
            dimensions=state["dimensions"], mood_cause=state.get("mood_cause", ""),
            concerns=state.get("concerns", []), interests=state.get("interests", []),
            frustrations=state.get("frustrations", []), attachment_tendencies=state.get("attachment_tendencies", []),
            beliefs=beliefs, goals=goals, contradictions=contradictions, reflections=reflections,
        )

    async def backup(self, destination: str | None = None) -> str:
        destination = destination or f"{self.db_path}.backup"
        os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
        async with self._connect() as source:
            async with aiosqlite.connect(destination, timeout=15.0) as target:
                await source.backup(target)
        return destination

    async def delete_user_scoped_data(self, user_id: int) -> None:
        """Remove self-model records that can be tied to one user's identity."""
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("DELETE FROM self_reflections WHERE related_user_id=?", (user_id,))
            await db.execute("DELETE FROM self_goals WHERE related_user_id=?", (user_id,))
            await db.execute("DELETE FROM self_contradictions WHERE related_user_id=?", (user_id,))
            await db.execute("DELETE FROM autonomous_actions WHERE related_user_id=?", (user_id,))
            await db.execute("DELETE FROM self_events WHERE related_user_id=?", (user_id,))
            await db.execute("DELETE FROM self_beliefs WHERE scope_user_id=?", (user_id,))
            await db.commit()

    async def forget_user_matches(self, user_id: int, query: str) -> dict[str, int]:
        """Remove user-scoped modeled state containing a requested literal phrase."""
        needle = (query or "").strip().lower()[:80]
        if not needle:
            return {}
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            reflection_cur = await db.execute(
                """DELETE FROM self_reflections WHERE related_user_id=? AND (
                       INSTR(LOWER(trigger),?)>0 OR INSTR(LOWER(observation),?)>0 OR
                       INSTR(LOWER(interpretation),?)>0
                   )""",
                (user_id, needle, needle, needle),
            )
            goal_cur = await db.execute(
                """DELETE FROM self_goals WHERE related_user_id=? AND (
                       INSTR(LOWER(description),?)>0 OR INSTR(LOWER(reason),?)>0 OR
                       INSTR(LOWER(completion_condition),?)>0 OR INSTR(LOWER(failure_condition),?)>0
                   )""",
                (user_id, needle, needle, needle, needle),
            )
            event_cur = await db.execute(
                "DELETE FROM self_events WHERE related_user_id=? AND INSTR(LOWER(summary),?)>0",
                (user_id, needle),
            )
            action_cur = await db.execute(
                """DELETE FROM autonomous_actions WHERE related_user_id=? AND (
                       INSTR(LOWER(reason),?)>0 OR INSTR(LOWER(details_json),?)>0
                   )""",
                (user_id, needle, needle),
            )
            contradiction_cur = await db.execute(
                "DELETE FROM self_contradictions WHERE related_user_id=? AND INSTR(LOWER(summary),?)>0",
                (user_id, needle),
            )
            # Evidence contributes to confidence and contradiction pressure. If
            # matched evidence must be forgotten, remove its user-scoped belief
            # as a unit rather than retaining scores derived from deleted text.
            belief_cur = await db.execute(
                """DELETE FROM self_beliefs WHERE scope_user_id=? AND (
                       INSTR(LOWER(belief),?)>0 OR EXISTS (
                           SELECT 1 FROM self_belief_evidence evidence
                           WHERE evidence.belief_id=self_beliefs.id
                             AND INSTR(LOWER(evidence.summary),?)>0
                       )
                   )""",
                (user_id, needle, needle),
            )
            await db.commit()
        return {
            "self_reflections": int(reflection_cur.rowcount or 0),
            "self_goals": int(goal_cur.rowcount or 0),
            "self_events": int(event_cur.rowcount or 0),
            "autonomous_actions": int(action_cur.rowcount or 0),
            "self_contradictions": int(contradiction_cur.rowcount or 0),
            "self_beliefs": int(belief_cur.rowcount or 0),
        }

    async def diagnostic_summary(self) -> dict[str, Any]:
        state = await self.get_state()
        reflections = await self.recent_reflections(1)
        return {
            "dimensions": {k: round(v, 1) for k, v in state["dimensions"].items()},
            "mood_cause": state.get("mood_cause", ""),
            "active_goals": await self.list_goals(limit=self.config.max_active_goals),
            "belief_count": len(await self.list_beliefs(50)),
            "open_contradictions": len(await self.list_contradictions(0, 50)),
            "last_reflection_ts": reflections[0]["ts"] if reflections else 0,
            "budget": await self.budget_status(),
        }
