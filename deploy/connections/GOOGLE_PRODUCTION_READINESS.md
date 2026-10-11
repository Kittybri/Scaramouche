# Google Connected Accounts Phase 1: Production verification

STATUS: Preparation files only. No Google authorization approval, DNS configuration, Search Console ownership, live token, or deployment is asserted by this document.

The two bots share **one** Google OAuth callback origin and a canonical shared encrypted connection database. Phase 1 supports Google Calendar and Tasks only. Keep Docs/Drive Phase 2 postponed.

## Domain and public content

1. Identify the real domain you own and will verify. It must have a valid HTTPS certificate. Add the registrable domain to Google Cloud **Google Auth Platform > Branding > Authorized domains** and verify ownership using Google Search Console with the same Google account that owns or edits the Google Cloud project.
2. Publish `deploy/connections/public/index.html` as `https://YOUR_HOST/` and `privacy.html` as `https://YOUR_HOST/privacy`. These are **drafts**, and the owner must verify disclosures and supply real public support contact details before publishing.
3. Review `deploy/connections/nginx.conf.example` and replace all sample host, certificate and public-root values. Keep the OAuth callback behind the existing loopback service, deny other paths, and disable URL/query logging (including CDN/WAF/proxy). Run `nginx -t` first. Do not enable the sample verbatim.

## Google Cloud Console

4. Under Google Auth Platform > Branding, set an accurate app name, the public homepage, privacy-policy URL, support email and developer contact. The privacy URL must match the link in the public homepage. Use a real support method.
5. Under Audience, select the correct audience (normally External for consumer Google accounts). Keep Testing until you are ready to publish. OAuth grants for external Testing projects can expire after seven days.
6. Under Data Access, list **only** the code-requested scopes:
   - `openid`
   - `email`
   - `https://www.googleapis.com/auth/calendar.events`
   - `https://www.googleapis.com/auth/tasks`
   Calendar.events permits viewing/editing events, and tasks permits creating/editing/organizing/deleting tasks. Phase 1's current product flow offers only the supported read/create/update actions and requires exact confirmation for writes. Do not misstate either scope as read-only. These scopes may require sensitive-scope verification.
7. Enable the Google Calendar API and Google Tasks API. Use a **Web application** OAuth client with exactly `https://YOUR_HOST/oauth/google/callback` as an Authorized redirect URI. That URI must equal the privately configured `GOOGLE_OAUTH_REDIRECT_URI` without any extra slash, query string or alternate hostname.
8. Complete Google's branding and data-access verification process as applicable. Prepare scope justifications and a demonstration video showing the app name, consent flow, private Discord confirmation, Calendar/Tasks read actions, and an explicitly authorized test write in an isolated test account. Monitor the project's support/developer email for verification requests.

## Security and rollout gates

9. Keep existing real values for `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI`, `CONNECTIONS_MASTER_KEY`, and `CONNECTIONS_SHARED_DB` in secret environment configuration, never GitHub. Preserve the existing master key and shared database across releases. Back up the encrypted DB before host migration. Never paste credentials into a chat.
10. External HTTPS smoke checks: homepage is public and explains the service, privacy page is public and linked, `/health` returns `ok`, unauthorized OAuth link fails, and no callback URL, OAuth code or state leaks into logs.
11. Controlled live test with a consenting Google test account: Discord `/google` connect, Google consent, return to requesting bot's DM, confirm, check Calendar and Tasks reads, explicitly confirm test create/update, check second bot is denied until separately granted, disconnect, verify local deletion and best-effort Google revocation. No unsupervised production writes.
12. Do not move the OAuth app to public production before the verified domain, public policy, accurate branding, necessary scope review and staging validation are complete. Do not merge this preparatory PR or deploy it without review.

## Verification text examples (edit to match actual product behavior)

Calendar events: the bot retrieves requested upcoming events and supports creating or editing events after explicit user confirmation. The requested view/edit scope is needed because read-only scope cannot create/update an event.

Tasks: the bot retrieves requested tasks and supports creating or updating tasks after explicit user confirmation. The app asks for the tasks scope for its supported write capability; this is broader than read-only access and must be disclosed.

Identity: basic identity scopes verify which Google account was linked to the requesting Discord account. The public UI only shows a masked identity.

Official information:
- https://developers.google.com/identity/protocols/oauth2/production-readiness/brand-verification
- https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification
- https://developers.google.com/workspace/calendar/api/auth
- https://developers.google.com/workspace/tasks/auth
