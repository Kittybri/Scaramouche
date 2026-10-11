# Provider-status and error diagnostics
- No external provider requests are made by `!status`, `!aistatus`,
  `!scarastatus` or `!wanderstatus`.
- Owner-only provider labels distinguish `not_configured`, `cooldown`,
  `recovering`, `degraded`, `configured` and
  `diagnostics_unavailable`. `configured` means only that a client
  exists and recent observations have not signaled an issue; it is not a live
  successful API probe.
- A failing provider monitor is reported as `diagnostics_unavailable`
  rather than raising and possibly leaking raw exception details.
- Ordinary users receive only bot-specific character wording, not internal
  provider state. Failure to reach the owner's DM does not send private
  diagnostics publicly.
- The existing bot/personality text response and voice systems are unchanged.
