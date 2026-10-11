# Scaramouche administrative boundary audit

The current release already denies access to legacy server inventory
when no owner ID is configured, and sends inventory only to the owner's DM.
The self-model backup commands route through the fail-closed `_owner_only`
gate. This PR adds explicit negative preservation contracts to make a future
accidental regression detectable without changing production behavior.

Wanderer's historic rebuild commands have different authorization risks and
are handled independently on its own repair branch. All-user history replay,
biometric export/import, old admin mutation commands and other unsafe surfaces
remain deferred; this audit must not silently re-enable them.

No voice, Google, Discord command, database or deployment changes.
