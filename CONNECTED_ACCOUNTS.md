# Connected Accounts — Google Phase 1

> **Current October 2026 note:** The original feature-branch SHAs below
> are preserved as historical implementation context. The present release
> heads are Scaramouche `03a8ae03d35e42511390717ec2cbd63d4c508390`
> and Wanderer `3b998a71b3f3d75c14b7eeef00932e31c60569a5`.
> The OAuth callback was deployed and staging-tested on Oracle as recorded
> in [STAGING_VALIDATION.md](STAGING_VALIDATION.md); Google Production
> verification, public site publication and Phase 2 remain incomplete.
> Do not restart the callback as collateral for documentation changes.


## Release boundary and current state

This feature is isolated on `feature/connected-accounts-google` in both repositories,
stacked on the verified `release/full-system-hardening` heads:

- Scaramouche: `5548cfa8895acfe0521417688e620a05e408b38e`.
- Wanderer: `b98912758d49fe7ef39d42c7df90b3e9ba23467b`.

No release PR was merged. No voice behavior, Fish Audio, character prompt, trolling
preference, or heavy staging setting is changed. Gate A remains passed; the available
voice evidence does **not** establish Gate B completion. See `STAGING_VALIDATION.md`.

**Historical preflight (superseded by the staging closure record):** At preflight the staging host
had no configured Google OAuth client/redirect/master-key variables and no callback
service or HTTPS reverse proxy listening. Google's app publishing status, approved
test-user list, domain/certificate, API enablement and verification status are
**unknown**, not assumed to be Production. Automated validation uses synthetic
credentials and fake Google responses only.

## Architecture

For current live HTTPS/OAuth deployment, two-user isolation, disconnect/reconnect,
full-suite results and remaining closure gates, see the canonical
[STAGING_VALIDATION.md](STAGING_VALIDATION.md), section "Google Phase 1 closure audit".
The preflight paragraph above describes the original starting state, not the
current deployment. Expanded two-user disconnect/reconnect and restart closure is
now READY_FOR_MERGE for Phase 1, with full current regressions in the canonical
record; Google Production preparation and external PR checks remain separate.

Both bots and the separate callback process use the **same existing shared SQLite
database**. `ConnectedAccountService` owns authorization; neither character's LLM
receives credentials or decides OAuth validity, scopes, grants, or revocation.

- `connections/security.py`: validated operator configuration; libsodium encryption.
- `connections/store.py`: additive transactional schema, concurrency and migration ledger.
- `connections/providers.py`: provider protocol and the only implemented provider, Google.
- `connections/service.py`: sessions, account linking, grants, refresh, disconnect/reset.
- `connections/discord_ui.py`: private panels, account confirmation, explicit bot grants.
- `connections/web.py`: three-route, loopback-only HTTP application behind HTTPS.
- `connections/runtime.py`: existing bounded Calendar/Tasks runtime with dynamic tokens.

The provider contract contains authorization URL, exchange, identity, refresh,
revocation and module scopes. A future provider needs an adapter, its approved
configuration/scopes and explicit callback routing, not a second token database.
No additional providers or Google modules are implemented here.

### Canonical records

`connected_accounts` has one `(user_id, provider)` row, a random connection revision,
masked identity, one encrypted credential envelope (access token, refresh token,
provider subject), granted scopes, status, expiry, creation/update/refresh times and
refresh lease/backoff fields. `connected_account_bot_grants` is keyed separately by
user/provider/bot and cascades on account deletion. No client secret is stored here.

`connection_sessions` contains hashed opaque links/state/browser bindings, the
requesting Discord user/bot/provider, fixed callback URI, short expiry, stage and
an encrypted PKCE verifier or pending credential envelope. One session per user,
provider and requesting bot replaces that bot's older attempt. Both bots' DM links
can coexist when both receive `!connections`; completing either link invalidates
all other pending sessions for that user/provider. Admission counters bound generation.
`connections_migrations` records schema version 1. Migration is additive and uses
`BEGIN IMMEDIATE`, commit/rollback and the shared SQLite foreign-key policy.
No old credentials are silently imported and no existing table is rewritten.

## Discord workflow

`!google` is the main prefix command, highlighted in the character's help menu.
`/google` opens the same controls as a private (ephemeral) slash-command response,
including in a server with DMs closed. The prefix aliases below still work and
continue to deliver the panel by DM. Each friend links their own Google account
and explicitly chooses each bot's grant. While the Google OAuth app is in Testing,
the operator must first approve that friend's Google account as a test user;
Discord command visibility does not bypass Google's test-user restrictions.

1. `!connections`, `!google`, `!google status`, or `!google permissions` sends a DM
   panel. Guild replies contain no identity, account link or token.
2. `!google connect` includes an actual Discord LINK button to
   `https://HOST/oauth/google/start/OPAQUE_RANDOM_TOKEN` (10-minute lifetime).
3. The user personally chooses an account and approves Google's consent screen.
4. Google redirects to the exact registered HTTPS callback. The service verifies
   single-use state, provider, expiry, fixed URI and a Secure/HttpOnly/SameSite=Lax
   browser cookie, then exchanges the code using a PKCE verifier.
5. The callback stores only **pending encrypted** credentials. It tells the user to
   return to Discord. The requesting bot DMs a **Confirm account link** button.
   This final check belongs to the original Discord user and requesting bot. A
   copied bearer link alone cannot activate another user's authorization. Confirm
   only if you personally just approved Google; otherwise disconnect/discard it.
6. Confirmation activates only the requesting bot's grant. The other bot remains
   disabled until that same user deliberately presses its Allow button.

`!google permissions` shows actual granted Calendar/Tasks modules and both bot
grants. No fake buttons for future modules. Disabling a bot's grant does not
duplicate/delete the shared credentials. Re-linking/replacing an account resets
the partner grant; it must be deliberately re-enabled. Old management buttons and
write proposals are bound to their connection revision, not a replacement account.

Confirmation views last 10 minutes. After a bot restart, missed DM or expired
component, use `!google` to recover any still-valid durable pending confirmation.
If DMs are blocked, the public response asks the user to open a DM; it never leaks
the link as a fallback. Notifications are bounded, best effort and emitted by the
requesting bot only. Expired sessions are cleaned on creation and notification polls.

## OAuth and scopes

The adapter uses Google's documented web-server authorization-code endpoints,
`access_type=offline`, `include_granted_scopes=true`, state and S256 PKCE. It requests
`openid`, `email`, `https://www.googleapis.com/auth/calendar.events` and
`https://www.googleapis.com/auth/tasks`. Calendar's event-only permission avoids
calendar-settings access; Tasks has no narrower scope supporting the existing
create/update operations. Google may describe deletion in these scopes, but the
bot exposes **no deletion capability**. Granular partial consent is respected:
only granted modules are authorized; no module grant means no completed link.
Reconnect reviews the missing permissions; no extra modules are silently requested.

Account identity is obtained from Google's authenticated UserInfo endpoint, not
from an unvalidated ID-token payload. Verified email is masked; subject identity
is encrypted. Provider names/email/event/task content remain untrusted data.

Official references checked during implementation:

- [Web-server OAuth, offline access, incremental authorization and revocation](https://developers.google.com/identity/protocols/oauth2/web-server).
- [Calendar scopes](https://developers.google.com/workspace/calendar/api/auth).
- [Tasks scopes](https://developers.google.com/workspace/tasks/auth).
- [OAuth security best practices](https://developers.google.com/identity/protocols/oauth2/resources/best-practices).
- [Refresh-token expiration, including Testing projects](https://developers.google.com/identity/protocols/oauth2#expiration).

An External OAuth app in **Testing** generally issues refresh tokens expiring after
seven days when Calendar/Tasks are requested; this is not a refresh implementation
failure. Restrict initial staging to approved test users. Production/public
onboarding requires the appropriate publishing configuration and any required
sensitive-scope verification. This repository does not establish either status.

## Encryption and operator secrets

Use the existing vetted PyNaCl/libsodium `SecretBox` authenticated encryption with
random nonces. The authenticated envelope binds credentials to their Discord user,
provider and connection revision; session envelopes also bind their bot and session.
Ciphertext tampering, swapping to another user, missing keys and wrong keys fail
closed. Tokens never enter prompt context, diagnostics, Discord embeds or HTML.

All three processes require protected environment settings (outside Git/SQLite):

| Variable | Purpose |
| --- | --- |
| `GOOGLE_OAUTH_CLIENT_ID` | One Google Web application client ID |
| `GOOGLE_OAUTH_CLIENT_SECRET` | That application's client secret |
| `GOOGLE_OAUTH_REDIRECT_URI` | Exact `https://HOST/oauth/google/callback` |
| `CONNECTIONS_MASTER_KEY` | URL-safe base64 of 32 cryptographically random bytes |
| `CONNECTIONS_SHARED_DB` | Callback process only: absolute canonical shared DB path |

Bots use their existing `mem.shared_db_path`; the callback must point at precisely
that same file. Keep the same master key/client/redirect in both bots and the
callback. Generate the key into a protected secret file or secret manager; never
print/paste it into chat, logs or Git. Restrict files/directories to the service
account (0600/0700 or narrowly shared service group) and protect encrypted backups.
Back up the master key separately. Key rotation is **not** implemented: changing
the key requires a planned re-encryption migration or deliberate unlink/reconnect;
never silently regenerate it at startup. Local deletion remains possible with a
lost key, but provider revocation cannot then be verified.

SQLite logical deletion removes active records, not historical backups or forensic
disk remnants. Encrypted backup retention and any disk sanitization are operator
responsibilities. Secure the host too: a compromised process with access to both
the key and database can use credentials. Encryption is not a substitute for that.

## Refresh, disconnect and privacy

Access tokens are resolved at request time, not copied to user JSON or cached
across users. Refresh uses a 40-second SQLite lease shared across processes, bounded
waiting and a 12-second provider timeout. Transient refresh failure backs off for
30 seconds. Rotation persists atomically. Revoked refresh authorization becomes
`REAUTH_REQUIRED` and encrypted tokens are removed rather than retried forever.
Disabling a bot during refresh still permits safe canonical rotation persistence,
but denies that bot the returned token. Disconnect/relink wins over in-flight
callbacks/refresh; a stale worker cannot recreate the deleted account.

`!google disconnect` requires a second deliberate button. It first deletes local
tokens, grants and pending OAuth sessions in one transaction, then attempts Google
revocation. Remote failure is reported separately and cannot undo local deletion.
Already-dispatched provider requests cannot be recalled; no new authorized request
or stale write confirmation may proceed after deletion.

Both resumable privacy sagas include initial and final connected-account erasure
plus local proposal/cache cleanup. These stages are idempotent. They remove account
metadata, sessions, grants and per-user rate records; a failed later deletion stage
can resume without recreating authorization. Other users' records remain intact.

## Calendar/Tasks and legacy boundary

Scaramouche keeps its existing Calendar/Tasks commands and protected runtime.
Wanderer's actual starting branch lacked those command groups; it now reuses the
same bounded runtime and command behavior. Lists remain bounded; Calendar updates
require the bot-created-event marker; Tasks fields remain allowlisted. Create/update
still require a short-lived user/provider-specific exact confirmation ID and execute
the stored payload, never regenerated prose. The proposal is also account-revision
bound. Plain conversation has no write execution route. Calendar/Tasks previews
do not perform provider writes; Calendar update may read the existing event.

There is deliberately **no automatic legacy Calendar/Tasks fallback**, including
for the owner: a dynamic denial/disconnect must not revive an old JSON credential.
`BOT_INTEGRATIONS_CONFIG` is preserved without migrating or deleting its values.
The legacy, owner-only application-owned Sheets allowlist remains a separate
boundary with no new OAuth Sheets/Drive scope and no arbitrary spreadsheet browsing.
Existing optional companion deadline reads resolve dynamic credentials too; this
batch does not activate companion or physical-device features.

## Callback deployment (not performed yet)

Run one callback service: `python -m connections.web`, listening **only** on
`127.0.0.1:8787`. The public routes are `/health`, `/oauth/google/start/{token}` and
`/oauth/google/callback`. No executor, admin shell, arbitrary redirect or generic
proxy exists. Use the reviewed systemd/nginx examples under `deploy/connections/`,
adjusting service account, paths, hostname and certificate before installing.

The public origin needs a real hostname and valid HTTPS certificate. Raw public IP
redirects, alternate ports, credentials/query/fragment in redirect configuration,
and disabled TLS validation are rejected. Local fake-provider tests can use an
HTTP test server without weakening production redirect validation.

**Disable access logging of OAuth URLs and headers at every layer**, including the
reverse proxy, load balancer, WAF and observability tooling: callbacks contain codes,
and start URLs contain bearer links. The app has no access log; failures return
fixed safe messages, not provider bodies/tracebacks. Security headers prohibit cache,
referrers, frames and external content. The proxy must not cache redirects/pages.
Rate limits: three link attempts/user/15-minute bucket, 100 globally/bucket, 256
pending sessions maximum, plus bounded HTTP traffic and refresh backoff. Bucket
boundaries can permit a short burst; this is not a billing-grade quota system.

## Validation and next live steps

Automated results are recorded in `STAGING_VALIDATION.md` after final regression.
Tests cover state/replay/user binding, grants, encryption, refresh races/rotation,
restart persistence, privacy sagas, provider failures, dynamic Calendar/Tasks,
no-write previews, stored confirmations and restricted Sheets behavior.

Before live testing, the operator must provide the HTTPS hostname/certificate and
configure a Google **Web application** OAuth client with the exact callback URI,
Calendar/Tasks APIs and approved staging test user. Inspect and record actual
publishing/verification status. Store app secrets/master key through a protected
deployment channel, **not Discord or this chat**. No user's Google password, MFA
code or recovery code is ever needed by the bots or operator.

Then deploy the exact reviewed feature candidates, point all three services at the
same shared database, and ask the user to personally click `!connections` → Connect
Google → choose account → approve Calendar/Tasks → Confirm account link in Discord.
Stop for the user's sign-in/consent; do not automate their authentication.

Validate `!google status`, Calendar tomorrow and Tasks reads. Make Calendar/Task
proposals **without confirming them**, verify no real objects were created, restart
both bots and callback, and verify persisted connection/grants and safe refresh.
Do not claim live success until those checks actually pass. Do not begin Gmail,
Drive/Docs, Photos, Contacts, Meet, YouTube or other later features.
