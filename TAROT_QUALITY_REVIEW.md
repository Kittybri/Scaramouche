# Scaramouche tarot reading quality (draft review)

## Source and intent
- Base: Scaramouche `release/full-system-hardening` at `03a8ae03d35e42511390717ec2cbd63d4c508390`.
- Wanderer's tarot quality change was checked against the Scaramouche release versions of `tarot_system.py` and `tarot_commands.py`. The starting implementations were byte-identical.
- Reuse the generic, feature-preserving tarot fixes, **not Wanderer's personality**. Existing `persona_line("Scaramouche")` explicitly directs a colder, theatrical voice and remains unchanged.

## Corrections
1. Model generations are checked for `finish_reason` and an obviously incomplete final sentence. One bounded retry increases token allowance; an incomplete second result is never presented as a final answer.
2. Three-card and five-card results, clarifiers and follow-ups paginate instead of dropping the end of a message at the 1950-character boundary. All 78 illustrations, user-restricted buttons, saved readings and the existing three Celtic Cross pages remain supported.
3. Tarot prompts must directly address the original question, interpret each card in context, explain references to Genshin lore in plain language, and present past-life themes as symbolic storytelling rather than facts.
4. Brief and Detailed preferences remain, with a somewhat higher token budget for short multi-card spreads.

## Preserve
`!scaratarot`, `!scarat` and the rest of Scaramouche's existing aliases, slash command registrations, user privacy and deletion, historical DB, and all non-tarot features. Do not touch Scaramouche's voice, Wanderer, Google, RPG, birthdays, achievements, or main/master.

## Validation
GitHub CI includes existing command/preservation/Tarot tests and the new tarot quality cases, including Scaramouche personality-specific assertions. Full test suite, real model completion, Android Discord UI and Oracle deployment remain separate gates; no live tests, service restarts or merges are authorized by this draft.

# P0 owner-approved tarot command entrypoint change (2026-10-10)

Only reading/daily slash commands `/tarot` and `/dailycard` are retired for both bot applications. The `!` equivalents deliver ordinary Discord messages (public-channel output for public visibility settings, DM output for private settings), so users can find them again in Discord history. The historical `!scaradaily` entry remains functional alongside the new alias `!scaradailycard`. `/tarothistory` and `/tarotsettings` remain available because the user did not request removing them. Existing tarot art, spread behavior, history, privacy settings, and both personalities are unchanged. Scaramouche's scoped startup sync deletes only the two retired application commands by ID; Wanderer's existing whole-tree sync removes the same commands after deployment. No production command was removed in this draft-only change.
