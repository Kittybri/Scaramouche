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

    def behavioral_stance(self) -> str:
        """Interpret the strongest modeled tension without exposing mechanics."""
        if not self.contradictions:
            return ""
        strongest = self.contradictions[0]
        pressure = float(strongest.get("pressure", 0) or 0)
        if pressure < 6:
            return ""
        summary = str(strongest.get("summary") or "").lower()
        if any(word in summary for word in ("attach", "care", "reply", "need anyone")):
            return (
                "He is increasingly unable to square his repeated attention with claimed detachment. "
                "Show that as defensive attention, inconvenient help, and sharp noticing—not open sweetness."
            )
        return (
            "His recent behavior conflicts with something he insists about himself. "
            "Let that make him more watchful and defensive; imply the tension and never explain the mechanism."
        )

    def prompt_fragment(self) -> str:
        """Return a compact private prompt fragment without raw evidence."""
        active = {k: round(v, 1) for k, v in self.dimensions.items() if v >= 2.5}
        lines = [f"SELF_STATE:{_json(active) or '{}'}"]
        if self.mood_cause:
            lines.append(f"SELF_STATE_CAUSE:{self.mood_cause[:120]}")
        if self.concerns:
            lines.append("CURRENT_CONCERNS:" + "; ".join(self.concerns[:3]))
        if self.beliefs:
            challenged_ids = {
                int(item["belief_id"]) for item in self.contradictions
                if item.get("belief_id") is not None
            }
            lines.append(
                "SELF_BELIEFS:" + "; ".join(
                    f"{belief['belief'][:100]} "
                    f"({'challenged; ' if int(belief['id']) in challenged_ids or float(belief['confidence']) < .55 else ''}"
                    f"confidence {float(belief['confidence']):.2f})"
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
        stance = self.behavioral_stance()
        if stance:
            lines.append(f"SELF_MODEL_STANCE:{stance}")
        lines.append(
            "INTERPRETATION_RULE: these are private modeled tendencies, not facts to recite. "
            "Let them alter cadence, attention, reluctance, and priorities subtly."
        )
        return "\n".join(lines)


class SelfModelStore:
    def __init__(self, db_path: str, config: AgentConfig = CONFIG):
        self.db_path = os.fspath(db_path)
        self.config = config

    @staticmethod
    async def _fetch_rows(
        db: aiosqlite.Connection, query: str, params: Iterable[Any],
    ) -> list[dict[str, Any]]:
        async with db.execute(query, tuple(params)) as cur:
            return [dict(row) for row in await cur.fetchall()]

    @staticmethod
    def _balanced(
        scoped: list[dict[str, Any]], global_rows: list[dict[str, Any]], limit: int,
    ) -> list[dict[str, Any]]:
        """Interleave relevant user state with global identity context."""
        result: list[dict[str, Any]] = []
        for index in range(max(len(scoped), len(global_rows))):
            if index < len(scoped):
                result.append(scoped[index])
            if len(result) >= limit:
                break
            if index < len(global_rows):
                result.append(global_rows[index])
            if len(result) >= limit:
                break
        return result

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
                    summary TEXT NOT NULL,
                    evidence_key TEXT
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
                    last_decay_ts REAL NOT NULL DEFAULT 0,
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
                    related_goal_id INTEGER,
                    related_belief_id INTEGER,
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
            # Additive compatibility for databases created before Repair Batch 8.
            async with db.execute("PRAGMA table_info(self_belief_evidence)") as cur:
                evidence_columns = {row[1] for row in await cur.fetchall()}
            if "evidence_key" not in evidence_columns:
                await db.execute("ALTER TABLE self_belief_evidence ADD COLUMN evidence_key TEXT")
            async with db.execute("PRAGMA table_info(self_contradictions)") as cur:
                contradiction_columns = {row[1] for row in await cur.fetchall()}
            if "last_decay_ts" not in contradiction_columns:
                await db.execute(
                    "ALTER TABLE self_contradictions ADD COLUMN last_decay_ts REAL NOT NULL DEFAULT 0"
                )
            async with db.execute("PRAGMA table_info(self_events)") as cur:
                event_columns = {row[1] for row in await cur.fetchall()}
            for column in ("related_goal_id", "related_belief_id"):
                if column not in event_columns:
                    await db.execute(f"ALTER TABLE self_events ADD COLUMN {column} INTEGER")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_belief_evidence_dedupe "
                "ON self_belief_evidence(belief_id,kind,evidence_key,ts DESC)"
            )
            await db.execute(
                "UPDATE self_contradictions SET last_decay_ts=last_seen_ts WHERE last_decay_ts<=0"
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
        limit = max(1, min(limit, 50))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            if user_id is None:
                async with db.execute(
                    "SELECT * FROM self_reflections ORDER BY ts DESC LIMIT ?", (limit,),
                ) as cur:
                    return [dict(row) for row in await cur.fetchall()]
            scoped = await self._fetch_rows(
                db,
                "SELECT * FROM self_reflections WHERE related_user_id=? "
                "ORDER BY importance DESC,ts DESC LIMIT ?",
                (user_id, limit),
            )
            global_rows = await self._fetch_rows(
                db,
                "SELECT * FROM self_reflections WHERE related_user_id IS NULL "
                "ORDER BY importance DESC,ts DESC LIMIT ?",
                (limit,),
            )
            return self._balanced(scoped, global_rows, limit)

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

    async def add_belief_evidence(
        self,
        belief_id: int,
        kind: str,
        weight: float,
        summary: str,
        *,
        evidence_key: str | None = None,
        dedupe_window_seconds: int = 21_600,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Apply gradual, optionally deduplicated evidence to one known belief."""
        if kind not in {"support", "contradict"}:
            raise ValueError("evidence kind must be support or contradict")
        weight = max(0.0, min(10.0, float(weight)))
        now = now or time.time()
        evidence_key = (evidence_key or "").strip().lower()[:160] or None
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
            if evidence_key:
                async with db.execute(
                    """SELECT 1 FROM self_belief_evidence
                       WHERE belief_id=? AND kind=? AND evidence_key=? AND ts>? LIMIT 1""",
                    (belief_id, kind, evidence_key, now - max(60, int(dedupe_window_seconds))),
                ) as cur:
                    if await cur.fetchone():
                        async with db.execute(
                            "SELECT id,pressure,status FROM self_contradictions WHERE belief_id=? AND related_user_id IS ? LIMIT 1",
                            (belief_id, user_id),
                        ) as contradiction_cur:
                            contradiction = await contradiction_cur.fetchone()
                        await db.rollback()
                        return {
                            "confidence": float(confidence),
                            "supporting_weight": float(supporting),
                            "contradicting_weight": float(contradicting),
                            "applied_weight": 0.0,
                            "deduplicated": True,
                            "contradiction_id": int(contradiction[0]) if contradiction else None,
                            "contradiction_pressure": float(contradiction[1]) if contradiction else 0.0,
                            "contradiction_status": str(contradiction[2]) if contradiction else None,
                        }
            if kind == "support":
                supporting += weight
                confidence = min(1.0, confidence + weight * .025)
            else:
                contradicting += weight
                confidence = max(0.05, confidence - weight * .02)
            await db.execute(
                "INSERT INTO self_belief_evidence(belief_id,ts,kind,weight,summary,evidence_key) VALUES(?,?,?,?,?,?)",
                (belief_id, now, kind, weight, summary[:500], evidence_key),
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
            contradiction_id = None
            contradiction_pressure = 0.0
            contradiction_status = None
            if kind == "contradict":
                contradiction_summary = f"Behavior conflicts with belief: {belief}"
                # The conflict target is spelled out because SQLite before 3.35 (the Oracle VM runs 3.34.1) rejects
                # "ON CONFLICT DO UPDATE" without one. It is idx_self_contradictions_unique, which also merges rows
                # whose related_user_id is NULL; whenever the table's own UNIQUE fails this index fails too.
                await db.execute(
                    """INSERT INTO self_contradictions
                       (belief_id,related_user_id,summary,pressure,status,first_seen_ts,last_seen_ts,last_decay_ts)
                       VALUES(?,?,?,?, 'open',?,?,?)
                       ON CONFLICT(belief_id,COALESCE(related_user_id,-1),summary) DO UPDATE SET
                         pressure=MIN(20,pressure+excluded.pressure),last_seen_ts=excluded.last_seen_ts,
                         last_decay_ts=excluded.last_decay_ts,status='open'""",
                    (belief_id, user_id, contradiction_summary[:500], weight, now, now, now),
                )
            else:
                # Supporting behavior reduces pressure gradually; history remains.
                await db.execute(
                    """UPDATE self_contradictions SET
                           pressure=MAX(0,pressure-?),last_decay_ts=?
                       WHERE belief_id=? AND related_user_id IS ? AND status='open'""",
                    (weight * .5, now, belief_id, user_id),
                )
            async with db.execute(
                "SELECT id,pressure,status FROM self_contradictions WHERE belief_id=? AND related_user_id IS ? LIMIT 1",
                (belief_id, user_id),
            ) as cur:
                contradiction = await cur.fetchone()
            if contradiction:
                contradiction_id = int(contradiction[0])
                contradiction_pressure = float(contradiction[1])
                contradiction_status = str(contradiction[2])
            await db.commit()
        return {
            "confidence": float(confidence),
            "supporting_weight": float(supporting),
            "contradicting_weight": float(contradicting),
            "applied_weight": float(weight),
            "deduplicated": False,
            "contradiction_id": contradiction_id,
            "contradiction_pressure": contradiction_pressure,
            "contradiction_status": contradiction_status,
        }

    async def list_beliefs(self, limit: int = 10, *, user_id: int | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 50))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            if user_id is None:
                async with db.execute(
                    "SELECT * FROM self_beliefs WHERE status='active' "
                    "ORDER BY confidence DESC,last_reinforced_ts DESC LIMIT ?", (limit,),
                ) as cur:
                    return [dict(row) for row in await cur.fetchall()]
            scoped = await self._fetch_rows(
                db,
                "SELECT * FROM self_beliefs WHERE status='active' AND scope_user_id=? "
                "ORDER BY confidence DESC,last_reinforced_ts DESC LIMIT ?",
                (user_id, limit),
            )
            global_rows = await self._fetch_rows(
                db,
                "SELECT * FROM self_beliefs WHERE status='active' AND scope_user_id IS NULL "
                "ORDER BY confidence DESC,last_reinforced_ts DESC LIMIT ?",
                (limit,),
            )
            return self._balanced(scoped, global_rows, limit)

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
                    """SELECT id,priority,related_user_id FROM self_goals
                       WHERE status='active' ORDER BY priority ASC,updated_ts ASC LIMIT 1"""
                ) as cur:
                    weakest = await cur.fetchone()
                if not weakest or int(weakest[1]) >= priority:
                    await db.rollback()
                    return None
                await db.execute("UPDATE self_goals SET status='abandoned',updated_ts=? WHERE id=?", (now, weakest[0]))
                await db.execute(
                    """INSERT INTO self_events(
                           ts,event_type,importance,related_user_id,related_goal_id,summary,processed,dedupe_key
                       ) VALUES(?,'goal_abandoned',4,?,?,?,0,?)""",
                    (now, weakest[2], weakest[0],
                     "A lower-priority modeled intention was displaced by a stronger one.",
                     f"goal_abandoned:{weakest[0]}"),
                )
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
            await self._prune_events(db)
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

    async def advance_goal(self, goal_id: int, amount: float) -> bool:
        """Advance an active goal without allowing progress to move backward."""
        amount = max(0.0, min(1.0, float(amount)))
        if not amount:
            return False
        now = time.time()
        async with self._connect() as db:
            cur = await db.execute(
                """UPDATE self_goals SET
                       progress=MIN(.95,progress+?),updated_ts=?
                   WHERE id=? AND status='active'""",
                (amount, now, goal_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def active_goal_by_dedupe(self, dedupe_key: str) -> dict[str, Any] | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM self_goals WHERE status='active' AND dedupe_key=? LIMIT 1",
                (dedupe_key[:240],),
            ) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

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
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT id,description,priority,related_user_id,dedupe_key
                   FROM self_goals WHERE status='active' AND expires_ts IS NOT NULL AND expires_ts<=?""",
                (now,),
            ) as pending_cur:
                expired = [dict(row) for row in await pending_cur.fetchall()]
            if expired:
                ids = [int(item["id"]) for item in expired]
                placeholders = ",".join("?" for _ in ids)
                cur = await db.execute(
                    f"UPDATE self_goals SET status='failed',updated_ts=? WHERE id IN ({placeholders})",
                    [now, *ids],
                )
                for item in expired:
                    if int(item["priority"]) < 5:
                        continue
                    dedupe = f"goal_failed:{item['id']}"
                    await db.execute(
                        """INSERT INTO self_events(
                               ts,event_type,importance,related_user_id,related_goal_id,summary,processed,dedupe_key
                           ) VALUES(?,'goal_failed',?,?,?,?,0,?)""",
                        (now, min(8, int(item["priority"])), item["related_user_id"], item["id"],
                         "An important modeled intention expired before it was resolved.", dedupe),
                    )
                await self._prune_events(db)
            else:
                cur = None
            await self._prune_goal_history(db)
            await db.commit()
            return int(cur.rowcount if cur else 0)

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
        limit = max(1, min(limit, 50))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            if user_id is None:
                async with db.execute(
                    "SELECT * FROM self_goals WHERE status=? "
                    "ORDER BY priority DESC,updated_ts DESC LIMIT ?", (status, limit),
                ) as cur:
                    return [dict(row) for row in await cur.fetchall()]
            scoped = await self._fetch_rows(
                db,
                "SELECT * FROM self_goals WHERE status=? AND related_user_id=? "
                "ORDER BY priority DESC,updated_ts DESC LIMIT ?",
                (status, user_id, limit),
            )
            global_rows = await self._fetch_rows(
                db,
                "SELECT * FROM self_goals WHERE status=? AND related_user_id IS NULL "
                "ORDER BY priority DESC,updated_ts DESC LIMIT ?",
                (status, limit),
            )
            return self._balanced(scoped, global_rows, limit)

    async def list_contradictions(self, min_pressure: float = 0, limit: int = 10,
                                  *, user_id: int | None = None,
                                  status: str = "open") -> list[dict[str, Any]]:
        if status not in {"open", "resolved"}:
            raise ValueError("invalid contradiction status")
        limit = max(1, min(limit, 50))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            if user_id is None:
                async with db.execute(
                    "SELECT * FROM self_contradictions WHERE status=? AND pressure>=? "
                    "ORDER BY pressure DESC,last_seen_ts DESC LIMIT ?",
                    (status, min_pressure, limit),
                ) as cur:
                    return [dict(row) for row in await cur.fetchall()]
            scoped = await self._fetch_rows(
                db,
                "SELECT * FROM self_contradictions WHERE status=? AND pressure>=? "
                "AND related_user_id=? ORDER BY pressure DESC,last_seen_ts DESC LIMIT ?",
                (status, min_pressure, user_id, limit),
            )
            global_rows = await self._fetch_rows(
                db,
                "SELECT * FROM self_contradictions WHERE status=? AND pressure>=? "
                "AND related_user_id IS NULL ORDER BY pressure DESC,last_seen_ts DESC LIMIT ?",
                (status, min_pressure, limit),
            )
            return self._balanced(scoped, global_rows, limit)

    async def resolve_contradiction(self, contradiction_id: int, *, now: float | None = None) -> bool:
        now = now or time.time()
        async with self._connect() as db:
            cur = await db.execute(
                """UPDATE self_contradictions SET status='resolved',last_decay_ts=?
                   WHERE id=? AND status='open'""",
                (now, contradiction_id),
            )
            await self._prune_contradictions(db)
            await db.commit()
            return cur.rowcount > 0

    async def decay_contradictions(
        self,
        now: float | None = None,
        *,
        pressure_per_day: float = .25,
        resolution_pressure: float = .75,
    ) -> list[dict[str, Any]]:
        """Decay open pressure by elapsed wall time and resolve quiet tensions."""
        now = now or time.time()
        changed: list[dict[str, Any]] = []
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                """SELECT id,belief_id,related_user_id,pressure,last_decay_ts,last_seen_ts
                   FROM self_contradictions WHERE status='open'"""
            ) as cur:
                rows = [dict(row) for row in await cur.fetchall()]
            for item in rows:
                baseline = float(item.get("last_decay_ts") or item.get("last_seen_ts") or now)
                elapsed_days = max(0.0, now - baseline) / 86400.0
                if elapsed_days < .25:
                    continue
                pressure = max(0.0, float(item["pressure"]) - pressure_per_day * elapsed_days)
                status = "resolved" if pressure <= resolution_pressure else "open"
                await db.execute(
                    "UPDATE self_contradictions SET pressure=?,status=?,last_decay_ts=? WHERE id=?",
                    (pressure, status, now, item["id"]),
                )
                item.update(previous_pressure=float(item["pressure"]), pressure=pressure, status=status)
                changed.append(item)
            await self._prune_contradictions(db)
            await db.commit()
        return changed

    async def _prune_contradictions(self, db: aiosqlite.Connection) -> None:
        await db.execute(
            """DELETE FROM self_contradictions
               WHERE status='resolved' AND id NOT IN (
                   SELECT id FROM self_contradictions WHERE status='resolved'
                   ORDER BY last_decay_ts DESC,last_seen_ts DESC LIMIT 250
               )"""
        )

    async def record_event(self, event_type: str, summary: str, *, importance: int = 1,
                           related_user_id: int | None = None,
                           related_goal_id: int | None = None,
                           related_belief_id: int | None = None,
                           dedupe_key: str | None = None,
                           now: float | None = None) -> int:
        now = now or time.time()
        async with self._connect() as db:
            if dedupe_key:
                async with db.execute(
                    "SELECT id FROM self_events WHERE dedupe_key=? AND ts>?", (dedupe_key[:200], now - 3600)
                ) as cur:
                    duplicate = await cur.fetchone()
                if duplicate:
                    return int(duplicate[0])
            cur = await db.execute(
                """INSERT INTO self_events(
                       ts,event_type,importance,related_user_id,related_goal_id,related_belief_id,
                       summary,processed,dedupe_key
                   ) VALUES(?,?,?,?,?,?,?,0,?)""",
                (now, event_type[:60], max(1, min(10, int(importance))), related_user_id,
                 related_goal_id, related_belief_id, summary[:800],
                 dedupe_key[:200] if dedupe_key else None),
            )
            await self._prune_events(db)
            await db.commit()
            return int(cur.lastrowid)

    async def recent_event_count(
        self, event_type: str, *, related_user_id: int | None = None,
        since: float | None = None, exact_scope: bool = False,
    ) -> int:
        clauses = ["event_type=?"]
        params: list[Any] = [event_type[:60]]
        if related_user_id is not None:
            clauses.append("related_user_id=?")
            params.append(related_user_id)
        elif exact_scope:
            clauses.append("related_user_id IS NULL")
        if since is not None:
            clauses.append("ts>=?")
            params.append(since)
        async with self._connect() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM self_events WHERE " + " AND ".join(clauses), params,
            ) as cur:
                return int((await cur.fetchone())[0])

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
        if status not in {"completed", "delivered", "failed", "skipped"}:
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
        clauses = ["action_type=?", "status IN ('pending','delivered','completed')", "ts>?"]
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
            async with db.execute(
                """SELECT id FROM self_beliefs WHERE scope_user_id=? AND (
                       INSTR(LOWER(belief),?)>0 OR EXISTS (
                           SELECT 1 FROM self_belief_evidence evidence
                           WHERE evidence.belief_id=self_beliefs.id
                             AND INSTR(LOWER(evidence.summary),?)>0
                       )
                   )""",
                (user_id, needle, needle),
            ) as matched_cur:
                matched_belief_ids = [int(row[0]) for row in await matched_cur.fetchall()]
            linked_goal_ids: list[int] = []
            if matched_belief_ids:
                placeholders = ",".join("?" for _ in matched_belief_ids)
                async with db.execute(
                    f"SELECT DISTINCT related_goal_id FROM self_events "
                    f"WHERE related_belief_id IN ({placeholders}) AND related_goal_id IS NOT NULL",
                    matched_belief_ids,
                ) as linked_goal_cur:
                    linked_goal_ids = [int(row[0]) for row in await linked_goal_cur.fetchall()]
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
            linked_reflections = linked_events = linked_goals = 0
            if matched_belief_ids:
                placeholders = ",".join("?" for _ in matched_belief_ids)
                cur = await db.execute(
                    f"DELETE FROM self_reflections WHERE related_belief_id IN ({placeholders})",
                    matched_belief_ids,
                )
                linked_reflections = int(cur.rowcount or 0)
                cur = await db.execute(
                    f"DELETE FROM self_events WHERE related_belief_id IN ({placeholders})",
                    matched_belief_ids,
                )
                linked_events = int(cur.rowcount or 0)
                goal_clauses = ["dedupe_key LIKE ?" for _ in matched_belief_ids]
                goal_params: list[Any] = [f"self_concept:{belief_id}:%" for belief_id in matched_belief_ids]
                if linked_goal_ids:
                    goal_clauses.append("id IN (" + ",".join("?" for _ in linked_goal_ids) + ")")
                    goal_params.extend(linked_goal_ids)
                cur = await db.execute(
                    "DELETE FROM self_goals WHERE related_user_id=? AND (" + " OR ".join(goal_clauses) + ")",
                    [user_id, *goal_params],
                )
                linked_goals = int(cur.rowcount or 0)
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
            "self_reflections": int(reflection_cur.rowcount or 0) + linked_reflections,
            "self_goals": int(goal_cur.rowcount or 0) + linked_goals,
            "self_events": int(event_cur.rowcount or 0) + linked_events,
            "autonomous_actions": int(action_cur.rowcount or 0),
            "self_contradictions": int(contradiction_cur.rowcount or 0),
            "self_beliefs": int(belief_cur.rowcount or 0),
        }

    async def diagnostic_summary(self) -> dict[str, Any]:
        state = await self.get_state()
        reflections = await self.recent_reflections(1)
        beliefs = await self.list_beliefs(50)
        open_contradictions = await self.list_contradictions(0, 50)
        resolved_contradictions = await self.list_contradictions(0, 50, status="resolved")
        active_goals = await self.list_goals(limit=self.config.max_active_goals)
        categories: dict[str, int] = {}
        for goal in active_goals:
            category = str(goal.get("category") or "conversation")
            categories[category] = categories.get(category, 0) + 1
        challenged_ids = {int(item["belief_id"]) for item in open_contradictions if item.get("belief_id")}
        return {
            "dimensions": {k: round(v, 1) for k, v in state["dimensions"].items()},
            "mood_cause": state.get("mood_cause", ""),
            "active_goals": active_goals,
            "active_goal_categories": categories,
            "belief_count": len(beliefs),
            "challenged_beliefs": sum(
                1 for belief in beliefs
                if int(belief["id"]) in challenged_ids or float(belief["confidence"]) < .55
            ),
            "open_contradictions": len(open_contradictions),
            "resolved_contradictions": len(resolved_contradictions),
            "highest_contradiction_pressure": max(
                (float(item["pressure"]) for item in open_contradictions), default=0.0,
            ),
            "last_reflection_ts": reflections[0]["ts"] if reflections else 0,
            "budget": await self.budget_status(),
        }
