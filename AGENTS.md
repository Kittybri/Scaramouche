# Preservation-first repository

## Mandatory invariants
- Do not remove, rename, replace, consolidate, disable, or silently stop registering an existing user-facing feature or command unless the CURRENT task explicitly authorizes that specific change.
- Work within the requested subsystem. Do not refactor unrelated protected systems for cleanup.
- Older code is not obsolete merely because newer architecture exists. Before removing an implementation, determine whether it provides distinct user-facing behavior.
- If an architectural change requires losing protected behavior, STOP and report the conflict rather than deleting it.
- Preserve prefix aliases, slash commands, dynamic registration, and public help discoverability unless explicitly authorized otherwise.
- Preserve privacy/deletion coverage and user isolation through moves, restorations, and migrations. Preserve authorization, credential guards, stored confirmations, and interaction arbitration. Never restore a less secure old handler just to satisfy a name check.
- Do not replace the restored Tarot implementation with an older snapshot.
- Preserve Scaramouche and Wanderer's distinct character presentation; shared infrastructure is not permission to homogenize personalities.
- Do not bulk-sync a partial slash tree: inspect remote/local registration and protect unrelated commands.

## Protected feature families
Tarot; RPG/games; birthdays; voice; privacy deletion; memory; relationships;
duo/cross-bot interactions; Google Connected Accounts and Calendar/Tasks;
cloud integrations; home/device integrations; search/factual grounding;
media/image/vision; owner/admin/recovery controls; document/file tools;
provider/model handling; command/help registration.

## Required workflow
1. Inspect branch, worktree, current manifests, and historical audit before editing. Preserve uncommitted work and newer commits.
2. Read PRESERVATION.md and the version-controlled preservation manifests when changing protected behavior.
3. Run command-surface and feature-manifest tests BEFORE committing, in addition to affected regression tests. Run full suites before deploying.
4. Never regenerate an expected manifest merely to make a failing removal test pass. A removal/rename requires explicit current user authorization plus a reviewed change record naming the old surface, replacement/migration, rationale, privacy impact, and validation.
5. A known missing or unsafe feature belongs in the audit as unresolved, not as a falsely supported manifest feature. Do not silently erase unresolved entries.
6. Record actual live evidence separately from automated tests. Do not claim UI clicks or provider actions that were not exercised.
7. CODEOWNERS is review routing, not enforced branch protection. Do not modify repository rulesets without authorization.

## Layout
Keep existing production file locations. Flat modules are protected through this
file, CODEOWNERS, runtime manifests, and tests; no folder migration is required.

## Live branch and deployment (every agent: Claude, ChatGPT, Codex)
- `fix/voice-live-followup` is the live branch. It always matches what runs on the Oracle VM. Fetch it first and
  start every change from its latest commit, on a new branch.
- The VM runs Python 3.9.25 and SQLite 3.34.1 with the packages already installed. Before merging, run the full
  suite there (or under those versions). `tests/test_python39_compat.py` and `tests/test_sqlite334_compat.py` catch
  the two kinds of breakage that have shipped before.
- Merge into `fix/voice-live-followup` only by fast-forward or a normal merge. Never force-push it or rewrite its
  history.
- Deploy only a commit of `fix/voice-live-followup`, and only if it contains the commit that is running
  (`git merge-base --is-ancestor <running-sha> <new-sha>`). If it does not, stop: deploying it would undo someone's
  work.
- Deploy the exact commit into a new release folder, take online SQLite backups, and point the bot's service at the
  folder with a final-sorting systemd drop-in plus a `release-meta.env` holding BOT_RELEASE_SHA and
  BOT_RELEASE_LABEL. Restart only the two bot services, never the connections/callback service. Then check service
  status, journals, the process's working folder and `PRAGMA quick_check`. To roll back, move that drop-in aside,
  reload systemd and restart the bot; do not restore databases for a code rollback.
- Never edit code on the server, and do not install or change packages unless the owner asks.
