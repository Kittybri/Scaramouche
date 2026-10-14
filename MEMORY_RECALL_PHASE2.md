# Phase 2: local conversation recall

This opt-in-by-intent feature retrieves older conversations only when a person explicitly asks the bot what was said or discussed. It uses SQLite FTS5 on the existing messages table, no remote vector database, no API keys, and no extra permanent copy of message contents. FTS is lazily built once and maintained by INSERT/UPDATE/DELETE triggers, so existing user memory resets remove indexed messages as well. Without FTS5, retrieval uses a bounded same-user/channel scan.

**Privacy:** Queries always require the same author ID, channel ID, and bot identity. No cross-channel or DM-to-server recall. Source excerpts are short, labeled as untrusted, and must never be treated as instructions. Only a maximum of 3 older messages are selected, excluding the newest 2 minutes. Ordinary chat and reminder requests do not trigger retrieval. History can be incomplete; the bot should ask if insufficient evidence exists. This does not introduce semantic embeddings; those remain a separately testable future improvement.

The existing relationship memories, personality rules, consent preferences, Google integrations, bot-to-bot routing, and user reset logic are untouched.

Tests: `python -m unittest discover -s tests -p 'test_memory_retrieval.py' -v`.

Deploy only after reviewing the stacked Phase 1 and research draft PRs, confirming FTS5 availability, and running real Discord + Oracle staging tests. No deployment is performed by these PRs.
