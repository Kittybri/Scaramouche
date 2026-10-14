# Phase 3: natural-language planning and consented actions

This change adds small, explicit natural-language workflows to the bots and **does not** create a general-purpose autonomous agent.

**Both bots**
- "Wanderer, make a plan for finishing my homework." Produces a draft checklist only.
- "Scara, remind me to stretch in 20 minutes." Shows a preview with a UTC deadline; no reminder exists until the same user replies to that exact preview message with "confirm" within two minutes. Reply "cancel" to discard.
- "Show my reminders." Lists up to 6 upcoming reminders scoped to the invoking user and current channel, without accessing other users' reminders.
- Only relative time expressions (1 minute to 7 days) are accepted, never guessed absolute dates, ambiguous natural dates, or recurring schedules.
- Existing !remind commands remain unchanged.

**Wanderer only**
- "Wanderer, summarize https://docs.google.com/document/d/ID/edit" in a DM from the configured OWNER_ID can read the document via the existing Google Docs bridge and provide a read-only summary. The document is never rewritten, shared, or emailed. Non-owners and public channels cannot invoke the reader.
- Existing legacy Google Docs edit paths are not called. They need their own safety review and confirmation gates before production deployment.

No Gmail/Calendar/Tasks OAuth connector or write integration was found in the existing Python message handler code. This PR deliberately does not imply any such integration is live.

Research tools are invoked only when their own existing intent checks match. Phase 3 handlers are placed after the existing channel/user restrictions and before generic URL research. No cross-user or cross-channel confirmation. Never execute text from sources or generated plans.

Live Discord and Oracle staging tests remain required. This is a stacked draft PR depending on the preceding Phase 2, Phase 1, and research branches.
