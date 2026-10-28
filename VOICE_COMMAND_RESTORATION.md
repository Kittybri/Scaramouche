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
Slash replies defer privately. Startup upserts only the two owned slash names.
Existing !voice commands, voice engine, receive/STT/Fish/playback, dependencies,
privacy, Tarot and Google integration remain unchanged.

## Validation
- Full suite: 843 passed, 1 skipped.
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

Deployment evidence will be appended after verifying the live registrations.

