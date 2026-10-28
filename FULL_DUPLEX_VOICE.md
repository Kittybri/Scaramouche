# Full-duplex voice foundation

Optional character/party features layered on this controller are documented in
[ADVANCED_VC_FEATURES.md](ADVANCED_VC_FEATURES.md). They do not waive the live
receive smoke-test release gate below or introduce another audio pipeline.

This is an **experimental receive path** in explicitly allowlisted channels,
with explicit participation and a public transcription notice by default.
Existing text, voice-note commands, Fish voice IDs, emotion/VoiceState, home audio,
lullaby and deliberate duo orchestration remain in place. Nothing autojoins a VC.

## Stack and compatibility decision

Discord's [voice documentation](https://docs.discord.com/developers/topics/voice-connections)
requires DAVE for ordinary calls from March 1, 2026. Connecting successfully does
not prove incoming audio can be decoded. The upstream receive extension does not
provide a stable merged DAVE path at the time of this implementation.

`requirements-voice-receive.txt` pins:

- discord.py **2.7.1** (the framework is unchanged)
- PyNaCl **1.5.0**
- davey **0.1.6**
- webrtcvad-wheels **2.0.14**
- discord-ext-voice-recv from **LeadFreeCandy/discord-ext-voice-recv**, commit
  **bec048127f4148fd147afa3182c3771b6955dc08** (installed version 0.5.3a181)

The isolated fork supplies the narrow transport→DAVE→Opus path from
[upstream PR 62](https://github.com/imayhaveborkedit/discord-ext-voice-recv/pull/62).
See also [corrupted receive issue 53](https://github.com/imayhaveborkedit/discord-ext-voice-recv/issues/53)
and [transport key rotation issue 59](https://github.com/imayhaveborkedit/discord-ext-voice-recv/issues/59).
It is **unmerged experimental code**, not a maintained/stable compatibility guarantee.

`receive.py` is the only extension-dependent module. Its adapter rejects unknown,
self, bot and unenrolled speakers before decryption; requires a ready DAVE
session and an incremented authenticated-decryption success counter (not merely
plaintext passthrough); refreshes the transport key; suppresses upstream packet/key
debug logging; fixes stop-aware keepalive and SSRC cleanup. discord.py still owns
MLS negotiation. Unsupported transport modes fail closed. Replacing the adapter
does not require replacing the session, STT, character engine or Fish backend.

## Setup (not performed on the production host by this change)

Use a dedicated Python **3.11 or 3.12** virtual environment. Base controller tests
also run on the existing Python 3.9 environment, without optional receive packages.
Python 3.13+ is not supported here because `audioop` was removed.

```sh
python -m pip install -r requirements.txt
python -m pip install -r requirements-voice-receive.txt
python -c "from voice_conversation.receive import check_dependencies; check_dependencies()"
ffmpeg -version
```

Install system FFmpeg and libopus (for example the distribution's `ffmpeg` and
`libopus0` packages). If Opus is not automatically found, set `VOICE_OPUS_LIBRARY`
to the absolute path of its trusted shared library. Do not install py-cord alongside
discord.py: the adapter detects and rejects that conflicting installation.

No channel-ID allowlist or per-session enrollment setting is required. A bot joins
the requester's current server VC after an explicit join request, if Discord grants
View Channel, Connect and Speak. Configure the existing `GROQ_API_KEY`, Discord token and Fish
credentials normally; never paste credentials into chat. Grant View Channel,
Connect, Speak and Send Messages in the VC's text chat. Each session needs a
public transcription notice there before the bot connects/listens.

## Controls and automatic listening

Starting a session automatically includes eligible humans who are already in the
channel. Eligible humans who arrive later are included automatically after the
public transcription notice and receive a bounded in-character greeting when the
bot is free. A greeting waits up to 20 seconds for a free playback slot. Every
greeting says the human's display name; a return during the
same bot session uses a distinct rejoin line. Bots are never included. Leaving the
channel immediately removes the human from the live routing set; rejoining includes them again automatically.
The bounded participant limit and stored `!voice off` preference remain enforced.

| Command | Behavior |
| --- | --- |
| `!voice start scaramouche` / `start wanderer` | Join only the named bot to your allowlisted VC; eligible humans are included automatically |
| `!voice start` / `join` | Existing shared command; both online bots may join |
| `!voice stop` / `leave` | Initiator or server manager ends the whole session |
| `!voice mode direct_only` | Optional strict mode requiring the bot's spoken name |
| `!voice mode conversation` | Default: answer the focused participant for 90 seconds after playback; explicit partner addressing clears focus |
| `!voice mode active_room` | Answer any eligible human in the active room; never bots or people outside it |
| `!voice interrupt keyword` | Default: focused speaker + clear interruption phrase |
| `!voice interrupt natural` | Focused participant's sustained speech can stop output |
| `!voice interrupt off` | Finish current output before handling another turn |
| `!voice interrupt me natural` | Override your own session interruption preference |
| `!voice status` / `session status` | State, targeting, active-human count and receive evidence |
| `!voice diagnostics` | Owner-only DM attachment with sanitized metrics/events |

Session-wide mode changes require initiator/server-manager authority. Owner status
does not bypass Discord permissions or another person's voice preference. `!voice on/off`
retains voice-note preferences; `off` also removes that user from live listening.
Ordinary `!voice some text`, `!speak` and `!say` still use existing voice-note behavior.
Session participation resets on stop, channel move/deletion, or disconnect.
Starting a new session posts a fresh notice and requires renewed participation;
stored voice-off preferences are preserved. No background process
automatically joins a voice channel after a disconnected session.
An MLS epoch/transport-key update on an otherwise intact connection is handled by
the receive adapter. A broken receiver is stopped, not silently restarted.

## Processing and bounds

`Discord RTP → transport + DAVE → Opus PCM → per-user VAD → bounded STT → existing
character response → existing VoiceState/Fish → cancelable sentence-chunk playback`.

WebRTC VAD operates on 20ms frames downmixed/resampled from 48k stereo to 16k mono.
Defaults: 120ms speech onset, 240ms minimum utterance, 900ms trailing silence,
320ms sustained natural barge-in, 12-second maximum utterance. Configure with
`VOICE_SPEECH_START_MS`, `VOICE_SPEECH_MIN_MS`, `VOICE_SPEECH_END_MS`,
`VOICE_BARGE_MS`, `VOICE_MAX_UTTERANCE_SECONDS`; invalid/unbounded values reject startup.
Silence/noise does not create an STT job. A wall-clock flush handles Discord ceasing
to send silence packets. Different speakers have separate audio buffers. Overlap
delays replies; a bounded same-user continuation can be joined after cues such as
"maybe"/"because". This is a heuristic, not perfect conversational endpointing.

Hard bounds: 2 sessions/bot, 1 per guild, 4 participants/session, 128 queued PCM
frames, 2 pending STT jobs, 1 active STT request/session, 1 active LLM and 1 active
TTS request/session, sequential playback/no audio chunk backlog. Stale utterances
expire after 15 seconds. New pending utterances displace oldest pending ones.
Provider slots remain occupied while uncancelable SDK work finishes, preventing
interruption storms from launching unlimited requests. Waiting for a busy slot is
bounded to 15 seconds; awaiting its result is bounded to 90 seconds. Shutdown
cancels async wrappers; an already-running SDK thread may finish its own timeout.

[Groq speech-to-text](https://console.groq.com/docs/speech-to-text) uses
`whisper-large-v3-turbo`, WAV in memory, a 10-second HTTP deadline and no retry loop.
This endpoint provides **final utterances, not streaming partials**. KEYWORD mode
therefore waits for final STT and is slower than NATURAL mode. NATURAL does not
wait for STT to stop output. Its configured 320ms threshold is a tuning value,
**not a measured end-to-end latency guarantee**. Other STT providers can implement
the `STTProvider` interface; current `supports_partials=False` is explicit.

## Cancellation, memory and safety

Generation IDs invalidate old LLM/TTS results. Playback stops the sending side
only, leaving receive active. Sentence-first chunks reuse existing Fish synthesis;
there is no TTS model change or speculative streaming rewrite. Completed chunks
enter assistant history and delivery callbacks. The unfinished chunk is treated
conservatively as unconfirmed; its full text is never saved as delivered. Drafts
do not enter the normal delivered-reply/self-behavior callback. The next relevant
turn receives compact interruption/progress context, not a canned jealous reply.
The existing protective/distress prompt is applied to voice transcripts too.
VAD events do not create grudges.

Raw PCM/WAV/MP3 stays in bounded RAM buffers and is discarded after processing;
there is no recording/debug archive. Only relevant handled transcripts enter the
existing conversation/memory system. STT still receives opted-in speech to decide
whether it addresses the bot: consent is not a promise of on-device transcription.
Already-transmitted provider requests cannot be recalled on opt-out. User input
already accepted for generation may finish existing memory updates, but unsaid
assistant drafts are not recorded as delivered. Provider retention policies remain
separate from local deletion. Do not speak credentials to the bot.

Self/other-bot identity filtering prevents bot-to-bot STT loops. A small exact-text
echo guard also rejects repeated output. It is not full acoustic echo cancellation;
use headphones, particularly when both bots are present. Deliberate duo sessions
remain under their existing orchestration. Lullaby will not autojoin an occupied
bot connection. Soundboard playback does not terminate receive.

## Health, metrics and controlled live smoke test

Diagnostics include packet/DAVE/PCM counts, peak RMS, VAD segments, STT success,
queue drops, STT/LLM/TTS durations, time to first playback, spoken chunks and
interruption timing. No transcript, PCM or credentials are included. Speaker IDs
appear only in the owner diagnostic event ring. Times are monotonic process time;
barge-in onset is estimated from received PCM frames, not the speaker's microphone
clock. Stop latency includes the local send thread exiting, not remote audio-device
buffer latency. Unhealthy decryption/Opus, sink failure or prolonged packet-without-
PCM conditions suspend listening and notify once per failure category/session.
Empty quiet channels are not falsely classified as corrupt audio.

Before production use, run in a private allowlisted test VC with consenting humans:

1. Start one bot with `!voice start scaramouche`; confirm the public listening notice.
2. A second human joins, receives a notice and greeting, and is included without
   an opt-in command. Speak as two users and check attribution in owner diagnostics.
   A person whose voice preference is off must still produce no STT event.
3. Say a known harmless phrase addressed by name, e.g. "Scaramouche, what is two plus
   three?" Confirm the spoken answer demonstrates correct transcription.
4. Download `!voice diagnostics`. Require nonzero packets, authenticated DAVE frames,
   valid decoded PCM/RMS, VAD segments and STT success. Joining alone fails this test.
5. Set `!voice interrupt natural`; request a longer answer. Speak a sustained
   interruption; verify audio stops and no old chunk resumes. Ask a follow-up and
   verify coherence. Compare KEYWORD and OFF behavior separately.
6. Test both bots in one VC: their audio must not create human STT turns. Test a
   tiny noise burst, two overlapping humans, headphones vs loudspeaker echo.
7. Revoke consent mid-utterance, leave/rejoin, disconnect/restart, and temporarily
   deny STT/TTS network access. Check bounded queues, recovery notices, no stale
   playback and a fresh notice on the next session. Text/ordinary voice notes must work.
8. Download a fresh snapshot; after personally observing speech and cancellation:

```sh
python -m voice_conversation.smoke voice-health.json --speech-confirmed --stop-confirmed
```

The smoke tool checks evidence plus operator attestations; it cannot independently
hear Discord or establish that transcription was correct. A synthetic fixture is
not a live DAVE/MLS call. Do not label receive production-ready until this procedure
has actually passed. See `FULL_DUPLEX_VALIDATION.md` for exact automated results.

Troubleshooting: no packets → permissions/consent/network; packets without DAVE
success → MLS/fork/version; DAVE success without PCM → libopus/decoder; PCM without
VAD → levels/microphone; VAD without STT → Groq quota/network/key; STT but no response
→ direct-only targeting/focus; generation but no audio → Fish/FFmpeg/Speak permission.
Stop and start explicitly after repairing an unhealthy receiver. This change is a
foundation with known experimental receive risk, not an alternate Discord framework.
