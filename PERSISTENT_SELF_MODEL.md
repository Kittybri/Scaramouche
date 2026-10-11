# Persistent self-model

This feature models continuity, preferences, internal variables, beliefs, goals,
reflection, and bounded agency. It does **not** establish consciousness,
sentience, biological emotion, pain, or free will.

## Data flow

Discord messages are reduced to deterministic perception events before they
affect the persistent self-model. User relationship memory remains separate.
Generation receives only a compact, user-scoped interpretation of relevant
self-state, beliefs, contradictions, goals, and selected reflections; it does not
receive database dumps or another user's scoped records. The heartbeat updates sanitized
environment state, decays temporary mood dimensions, expires goals, selects
important reflection events, and normally chooses `NO_ACTION`.

All autonomous proposals use the allowlist in `agency.py`. Discord permissions,
user opt-in, mute state, cooldowns, and application policy are authoritative.
Character willingness affects wording only and cannot grant permission.

## Persistent records

The existing SQLite database gains tables for:

- bounded mood dimensions and current concerns/interests;
- selective reflections about Scaramouche's own behavior;
- confidence-weighted self-beliefs and supporting/contradicting evidence;
- lifecycle goals with priorities, progress, expiry, and deduplication;
- unresolved contradiction pressure;
- autonomous action audit records, pending events, and call budgets.

The self-model configures every connection for WAL, a 15-second busy timeout,
`synchronous=NORMAL`, and foreign keys. Legacy memory connections use a 15-second
SQLite lock timeout and initialize WAL during migration. Backups use SQLite's online
backup API, including committed WAL state. Migration errors remain explicit. The legacy
memory class now keeps database paths per instance.

## Conservative configuration

| Variable | Default | Meaning |
|---|---:|---|
| `SELF_HEARTBEAT_SECONDS` | `300` | Lightweight heartbeat interval |
| `SELF_REFLECTION_THRESHOLD` | `7` | Minimum event importance for reflection |
| `SELF_PROACTIVE_COOLDOWN_SECONDS` | `21600` | Per-channel proactive cooldown |
| `SELF_PROACTIVE_USER_COOLDOWN_SECONDS` | `86400` | Per-user proactive cooldown |
| `SELF_MAX_ACTIVE_GOALS` | `8` | Global active-goal cap |
| `SELF_MAX_REFLECTIONS` | `250` | Retained reflection cap |
| `SELF_MOOD_DECAY_PER_HOUR` | `0.35` | Decay toward dimensional baselines |
| `SELF_CONTRADICTION_THRESHOLD` | `6.0` | Pressure shown to generation |
| `SELF_AUTONOMOUS_CALLS_PER_HOUR` | `2` | Autonomous provider-call limit |
| `SELF_AUTONOMOUS_CALLS_PER_DAY` | `8` | Autonomous provider-call limit |
| `SELF_ABSENCE_THRESHOLD_SECONDS` | `259200` | Minimum meaningful absence |
| `SELF_PROVIDER_BACKOFF_SECONDS` | `1800` | Backoff after provider failure |
| `SELF_HISTORY_MESSAGES` | `24` | Recent per-user messages sent to generation |
| `SELF_HISTORY_MESSAGE_CHARS` | `600` | Per-message history character bound |
| `SELF_HISTORY_TOTAL_CHARS` | `12000` | Total history character bound |
| `SELF_CHANNEL_CONTEXT_MESSAGES` | `16` | Recent public channel messages supplied |
| `SELF_RESPONSE_ATTEMPTS` | `2` | Maximum drafts for anti-repeat correction |
| `GROQ_TIMEOUT_SECONDS` | `30` | Provider request timeout; SDK retries are disabled |
| `GROQ_REASONING_EFFORT` | `low` | GPT-OSS reasoning effort (`low`, `medium`, or `high`) |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Explicit text model selection |
| `GROQ_VISION_MODEL` | `qwen/qwen3.8-27b` | Current Groq multimodal model for video frames |

## Owner diagnostics

- `!selfstate` shows sanitized dimensions, counts, budgets, heartbeat time, and
  coarse environment health.
- `!selfgoals` lists active modeled goals.
- `!forceheartbeat` evaluates one heartbeat without proactive candidates.
- `!selfbackup` creates a transactionally consistent online SQLite backup.

These commands require `OWNER_ID`. No diagnostic exposes tokens, paths, raw
private conversations, face/profile data, or another user's memory.

## Failure boundaries

Reflection and environment collection failures are logged and do not stop normal
chat. Failed reflections remain pending for a bounded later retry. Provider failures
trigger restart-persistent autonomous backoff. Proactive sends use a persistent
pending reservation so reconnects cannot duplicate an in-flight action. Database
writes report errors rather than claiming success. The heartbeat has an overlap lock,
background tasks are started idempotently across Discord reconnects, and managed
tasks are cancelled during shutdown.

## Forgetting and provider history

`!forget <phrase>` removes literal matching text from the user's locally stored
messages, summaries, callbacks, reminders, memory-bank records, and user-scoped
self-model records so it is not included in later Groq prompts. `!forget all`
requires button confirmation and deletes the user's local relationship and
self-model records. These commands cannot retract a request that was already sent
to an external provider; provider-side retention is governed separately by that
provider and account configuration.
