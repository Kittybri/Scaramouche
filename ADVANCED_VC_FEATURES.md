# Advanced voice personality and voluntary games

This branch is stacked on `feature/full-duplex-voice`. It does not replace receive,
STT, Fish, VoiceState, the home relay, the self-model, or the existing duo coordinator.
**Experimental: no live Discord call has been validated.** Keep the release gate in
[FULL_DUPLEX_VOICE.md](FULL_DUPLEX_VOICE.md). Automated Discord movement, DAVE/MLS,
STT, and Fish calls in feature tests are mocks, not proof of production readiness.

## Architecture

`existing receive → VAD → STT → Session → VoiceFeatureRouter → same response/Fish/playback`

`features.py` owns the one normalized-event router. `personality.py` makes bounded
deterministic decisions; `social_store.py` extends existing bot/shared SQLite files;
`games.py` owns consent, game state and recovery. `Session.submit` feeds structured
duo turns, scripted lines and authorized sounds into the existing generation/epoch,
provider-slot, cancellation and completed-chunk-memory lifecycle. No second receive,
STT, TTS or microphone listener is introduced. A bounded continuation is transcribed
once per segment; features see the combined text once, including safety checks.

Events include `utterance_completed`, `bot_interrupted`, `bot_spoken`, `user_left`,
`consent_revoked`, and `session_stopped`. Actual Discord join events seed a short
arrival cache; consent must follow before any welcome. One maintenance task handles
game recovery, short-lived partner readiness and coordinator turns. Shutdown cancels
that task; incomplete restoration survives in the database for the next startup.
Maintenance errors send one sanitized owner DM per process and appear in owner-only
`!voice diagnostics`; messages never include provider errors or transcripts.

## Configuration: disabled unless explicitly enabled

Add an `advanced_vc` section to the existing `BOT_INTEGRATIONS_CONFIG` JSON file
(or `BOT_INTEGRATIONS_JSON`). This illustrative configuration must use your real IDs:

```json
{
  "advanced_vc": {
    "guilds": {
      "123456789": {
        "enabled": true,
        "allowed_voice_channels": [234567890],
        "allowed_game_channels": [345678901],
        "max_party_features_per_hour": 4,
        "mockingbird_cooldown_days": 7,
        "features": {
          "mockingbird": true,
          "duo": true,
          "interrogate": true,
          "escape": true,
          "soundboard": true,
          "awareness": true
        }
      }
    }
  }
}
```

Absent guilds, flags and all five personal preferences default OFF. Session
Mockingbird mode also starts OFF. The foundation has no channel-ID allowlist;
an explicit join request in a server VC starts ordinary receiving. Advanced games
still require their separate guild/channel opt-in; game-created rooms remain scoped
to the accepted game. Both bots need the same **physical shared-state SQLite file** for
duo handoffs, cooldowns and the one-game-per-guild gate. Separate copies do not work.
Use the existing deployment data-directory arrangement; this adds no database.

Grant normal voice permissions and Send Messages. Game initiators require Manage
Channels. The bot needs Manage Channels and Move Members for interrogation, and
Create Private Threads, Send Messages in Threads and appropriate thread access for
Escape Room. Configure an ordinary text parent channel for the private thread.
No production configuration or permissions are changed by this branch.

## Commands and consent

| Command | Effect |
| --- | --- |
| `!vcparty on` / `off` | Personal master opt-in; off also clears new social notes and cancels your pending party work |
| `!vcparty mockingbird on/off` | Permit your own short content parodies |
| `!vcparty interrogation on/off` | Permit voluntary temporary-room games, including movement and voice processing |
| `!vcparty escape on/off` | Permit private puzzle-thread invitations |
| `!vcparty reactions on/off` | Permit rare arrival notices and authorized sound reactions |
| `!vcparty mode off/manual/party` | Session initiator or Manage Server controls Mockingbird mode |
| `!vcparty mock [@user]` | Parody only a consenting active participant's eligible utterance from the last 30 seconds |
| `!vcparty duo <type> <topic>` | Request a maximum two-turn structured exchange from both ready bots |
| `!vcparty status` / `help` | Personal settings, session mode, Priority Speaker status, or command syntax |
| `!vcparty forget` | Disable party features and erase compact VC notes/caches; ordinary chat memory is separate |
| `!vcgame interrogate [@user]` | Invite one human to a voluntary 1–3-question room game |
| `!vcgame escape [@users]` | Invite up to four humans including the initiator to a private puzzle thread |
| `!vcgame accept <id>` | Named invitee accepts the exact invitation within two minutes |
| `!vcgame answer <id> <answer>` / `hint <id>` | Participate from the exact game thread |
| `!vcgame cancel` / `leave` | Participant or manager ends the game; never prevents manual departure |
| `!vcgame status` / `help` | Your active games or syntax |

Party-game opt-in remains separate from ordinary voice conversation. Eligible
humans in an active voice session are listened to automatically after the visible
transcription notice, while persistent game consent still requires both master
and specific preferences. An explicit self-invitation or matching `accept`
provides one-time game consent instead. Nobody can accept on another person's
behalf, and a manager's authority to invite does not grant game consent.

`off` and `forget` work even in DMs or after a guild disables features. Revocation
cancels current output for that user and clears local feature caches. Existing
`!forget <topic>` and full reset also conservatively clear **all** new VC social
notes and stop party features for that user. Re-enable preferences deliberately.
Use each bot's controls separately. Cleanup records may remain until movement or
deletion can safely complete; deletion never discards a needed restoration receipt.

## Character behavior

**Mockingbird:** Scaramouche quotes a harmless game remark with theatrical disdain;
Wanderer occasionally teases the presentation, then supports the idea. Both use
their own unchanged Fish voice, never human voice cloning or impersonation. The
transcript filter deliberately accepts only a narrow game vocabulary, rejects
sensitive/urgent/identity/financial/sexual/criminal material, links and mentions,
and uses deterministic quote templates rather than an extra model call. MANY
ordinary sentences will be ineligible; this is intentional. Party chance per
eligible utterance is 2% Scaramouche / 0.3% Wanderer, additionally gated by seven-day
default per-user cooldown (minimum one day), shared guild spacing, two activations
per session, and a 12-second playback deadline per scripted chunk. A reservation
is consumed even if synthesis or playback subsequently fails, favoring rarity.

**Interruptions:** distinguish accidental overlap, normal, playful, repeated
deliberate, hostile, and urgent speech. Raw VAD alone cannot create a grievance.
Intentional/playful signals use at most six timestamps/person over two minutes;
accidents and emergencies do not increase the streak. Meaningful repeated events
may create one compact label, not automatic trust loss or a permanent grudge.
Scaramouche's prompt permits temporary pride/irritation; Wanderer adapts and helps.
Serious content overrides banter and ends further interrogation questions, letting
the normal protective reply complete before restoration. Detection is heuristic,
not a guarantee of understanding every emergency or intent.

**Duo:** types are `agree`, `disagree`, `correct`, `take_over`, `finish_thought`, and
`defuse`. Scaramouche is a useful, sharp critic; Wanderer provides concrete help or
protective mediation. Serious topics select `defuse`. Existing `duo_sessions`
remains authoritative: a leased claim assigns a turn, actual completed playback
advances the coordinator, and the count stops after two turns. Text autoplay skips
VC sessions and text replies cannot consume their turns. Microphone/STT bot frames
remain rejected by the unchanged receive adapter. Offline partner, opt-out or
interrupted generation cancels the exchange. Both bots need the requesting human
in their separately consented sessions. Start with the explicit `!vcparty duo`
command; natural-language dual summons are not an additional automatic trigger.
Handoffs are serialized, not literal overlapping audio or arbitrary mid-sentence
interruption. `finish_thought` can build on a completed chunk, not unheard drafts.

**Awareness:** only an actual join in the last 30 seconds can produce a welcome,
after listening consent and reaction opt-in. A focused person's departure while
the bot is speaking can store `left_during_speech`; the reason is explicitly
unknown. Scaramouche may notice; Wanderer gives the benefit of the doubt. Ordinary
departures, bots and unknown speakers create no fabricated social memory.

**Soundboard:** repeated deliberate interruption or a wrong puzzle answer may
request a scoff/sigh/buzzer. Only keys resolved by the existing authorized asset
function and soundboard guild allowlist are used. No model selection call, new
downloads, automatic joining or overlapping TTS. Maximum asset read 512 KB,
2.5-second playback deadline, 30-minute user cooldown plus guild spacing. Existing
manual `!sound` behavior remains unchanged. Configure approved assets as before.

## Game lifecycle and recovery

Interrogation records original VC and durable creation intent **before** creating
or moving anything. The accepted participant must still be in the recorded original
allowed VC. The bot must have no existing voice connection. A private temporary VC
hosts three fixed game-strategy questions, at most two minutes. Scaramouche acts as
prosecutor; Wanderer's own version is a practical defense/coaching conversation.
Automatic optional second-bot participation in that temporary room is not added.

On completion/cancel/timeout/restart, stop the managed voice session. Restore the
participant only if still in the exact game room; never chase someone who left
manually. Wait for Discord's cache to observe movement before deleting an empty
room. Never delete a room occupied by other humans/bots or whose name/identity has
changed. Missing permissions/original channel retains the journal and sends a
manager-action notice; users are always free to leave. Crash-after-create recovery
uses the exact unpredictable room name and creation timestamp, not a broad delete.
Cleanup retries every maintenance tick; there is at most one pending game/cleanup
per guild across both bots, and 25 records per bot. Discord outages may delay cleanup.

Escape Room records a randomly chosen deterministic puzzle, accepted variants,
hint and attempts before display. States include created, creating, active,
hint_requested, solved, failed, expired and cancelled. Ten-minute deadline, five
attempts, member-only answers from the exact thread. Active puzzles survive restart;
interrupted creation cancels safely. Completion/cancel/expiry archives the private
thread, rather than deleting its chat. Participants retain normal channels/roles;
no mute, timeout, forced isolation or permission removal is used. Archived thread
messages remain in Discord until separately deleted by an authorized person.

## Storage and privacy

Five OFF-default columns extend existing `user_preferences`. Existing shared SQLite
adds `vc_social` (six allowed labels, bot/user/guild scope, 30-day TTL),
`vc_feature_budget` (cooldown timestamps), `vc_games` (bounded restoration/game
journals), `vc_presence` (20-second expiry), and `vc_duo_output` (only the last
completed 600-character partner chunk per bot/channel/user, 120-second expiry).
Expired presence/output/social rows are pruned on initialization and activity;
they are not cryptographically erased from backups or SQLite free pages.
Cooldown metadata is retained to prevent opt-out/re-enable bypass, at most 366 days.

Safe parody candidates live only in RAM for 30 seconds. Game answers and ordinary
VC transcripts are not archived by these new tables. **Raw audio retention remains
zero on disk.** Normal handled speech/completed replies still follow existing bot
memory rules. Local forgetting cannot recall data already transmitted to Discord,
Groq or Fish, nor delete provider history/backups. Do not speak credentials. Existing
privacy, speaker filtering, queue and provider bounds remain in force.

## Priority Speaker: capability is not activation

Discord's [voice opcode 5](https://github.com/discord/discord-api-docs/blob/main/developers/topics/voice-connections.mdx)
supports a priority speaking bit, and its
[permissions](https://github.com/discord/discord-api-docs/blob/main/developers/topics/permissions.mdx)
include Priority Speaker. However the pinned discord.py 2.7.1 AudioPlayer manages
ordinary speaking state itself; a one-off private websocket flag can be overwritten.
This branch does not patch private AudioPlayer behavior or pretend a role activates
ducking. Diagnostics report effective channel permission and `not_implemented` (or
`unavailable` if the capability cannot be inspected). Manually configure the bot's
role/channel permission if desired, but dynamic bot Priority activation remains
unsupported here. Discord's [manual human setup](https://support.discord.com/hc/en-us/articles/360011876531-Setting-up-Priority-Speaker)
uses a Push-to-Talk Priority keybind, not Voice Activity. No other users are muted
or deafened by this feature.

## Manual release gate (NOT performed by this implementation)

First pass the existing DAVE/Opus/speech/barge-in/echo smoke test and its evidence
checker in `FULL_DUPLEX_VOICE.md`. Then, with explicit consent in a private test guild:

1. Enable only required flags/allowlists; test no reaction for nonconsenting humans.
   Enable master + Mockingbird, set MANUAL, speak “Guys, maybe we should fight the
   boss first.” Use `!vcparty mock`; verify the correct speaker, character's own
   Fish voice, cooldown, stop/opt-out, and exclusion of sensitive speech.
2. Start both bots using their normal controls and opt into each. Request
   `!vcparty duo correct <harmless question>`. Verify useful distinct responses,
   maximum two turns, no bot STT loop; stop one bot and confirm safe refusal.
3. Stop existing bot VC sessions; invite a human to interrogation. Decline first
   (no move), then explicitly accept a fresh eligible invitation. Verify private
   room, visible notice, no more than three questions, return to original VC and
   empty-room cleanup. Test manual departure, lost permissions, process restart,
   and feature disable with a pending recovery record. Never test on unaware users.
4. Invite consenting puzzle participants, accept by ID, verify private-thread
   access, deterministic solution, wrong answers, hint, five-attempt cap, timeout,
   restart and cancel. Ensure all normal channels remain accessible throughout.
5. Verify authorized reaction sound is short, rare, nonoverlapping and interruptible;
   test actual join/departure with/without consent. Verify `forget` and opt-out clear
   notes/cached candidates and stop queued output; retain only needed cleanup state.
6. Record observed results and fresh owner diagnostics. Until those checks pass,
   do not label this batch live-verified or the receive fork production-ready.

No additional dependencies, account connections, production deployment or automatic
PR merges are part of this batch. See `ADVANCED_VC_VALIDATION.md` for automated evidence.
