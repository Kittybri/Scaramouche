# Voice command restoration — 2026-10-28

## Cause and scope
Current release and live code retained VoiceConversation.command's join/start handler,
but did not register the former standalone !join / !listen / !leave entry points.
Live Discord global command lists also lacked /join and /leave for both applications.
The prominent help pages described voice notes without join instructions.
There is no evidence attributing this to a particular laptop.

voice_commands.py restores prefix commands and guild-only /join and /leave.
All routes delegate to the existing dispatcher, retaining channel allowlists,
voice preferences, session locking, initiator/manager leave authorization and
backend checks. Optional prefix targets reuse the current character-target filter.
Slash replies defer privately. Scaramouche upserts only the two owned slash names;
Wanderer uses its existing complete-tree synchronization. Live validation caught
a duplicate /join from concurrent upsert and tree sync; the added regression
ensures Wanderer never launches the competing upsert.
Existing !voice commands, voice engine, receive/STT/Fish/playback, dependencies,
privacy, Tarot and Google integration remain unchanged.

## Validation
- Final full suite including registration-race regression: 844 passed, 1 skipped.
- Focused registration, voice integration, preservation and Tarot suite: 56 passed, 1 skipped.
- Runtime preservation: zero errors; 184 prefix commands, 27 slash entries.
- compileall and git diff --check passed.
- Existing LibreSSL environment warning remains. Scaramouche's initial sandboxed
  full run could not bind loopback test servers; the unrestricted rerun passed.
- No live spoken conversation is claimed by these automated command tests.

## Usage
Join an enabled server voice channel, then use !join scaramouche or !join wanderer.
!join without a name retains both-bot behavior; !listen is an alias.
Alternatively select the intended application's /join.
Use !leave with an optional character name, or that application's /leave.
The existing !voice start / stop / diagnostics commands continue to work.

Deployment preflight discovered an existing live-only study attachment fix:
try the original attachment URL, then fall back to the cached URL on Discord
HTTP failure. The initial deployment stopped before any service changes.
That exact existing fix and its live PyPDF2==3.0.1 requirements entry are preserved
in this candidate to avoid reverting live behavior; they are not a new attachment
feature or a voice-engine modification. The deployment reuses the existing venv.
## Live staging result
- Deployed code SHA: `8e2d2adbff843970cc2cb657ce5955f6ed0f22c1`.
- Service active; automatic restart count 0; gateway ready, no startup traceback
  and no voice registration error after a controlled restart.
- Discord global commands contain exactly one `/join` and one `/leave`.
  Existing Tarot, Google, dashboard, duo, preferences and world entries remain.
- Shared and both bot SQLite `quick_check` results: `ok`.
- Callback service and protected configuration remained unchanged.
- Spoken audio was not tested as part of this command restoration.

