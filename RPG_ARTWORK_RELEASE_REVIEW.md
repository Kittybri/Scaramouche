# Harbinger Chronicles artwork: opt-in illustrated RPG

## Scope
Adds a **presentation layer only** to the existing Scaramouche `!rpg1` and
Wanderer `!rpg` Gauntlets. This is not a different RPG. Both original worlds,
original 11 boss order and lore, original prompts and generated scenarios, choices,
dice probabilities, points, saved progress, medals, character origins and narration
remain unchanged. Existing embeds and buttons are sent even when artwork fails.

* Living Battlefield: a layered JPEG composition beside each original encounter
  embed, with current boss, origin/Vision, points, round, and earned seals.
* Harbinger War Table: the same earned-seal tracking, presented as a separate
  full illustrated map in Scaramouche's `!rpgstats1` and as the image on
  Wanderer's `!rpgstats0` / `!rpgstats` embed.
* Actual rolled number appears on the Round-10 dice screen in Wanderer.
  Scaramouche's round-ten dice event is already included in the existing
  `last_result` before its next encounter panel; no dice logic was touched.
* Victory/defeat artwork comes from the **actual** outcome. No new story text or
  rewards are invented by the visual layer.

## Art pack (separate from the code PR)

## Art pack (separate from the code PR)

The previous flat-folder art ZIP is an **unapproved draft**, not a deployable
horror art pack. Only curated, reviewed artwork belongs in these paths:

- `assets/rpg/worlds/horror.jpg` and `assets/rpg/worlds/takeover.jpg`
- In EACH `assets/rpg/portraits/horror/` and `assets/rpg/portraits/takeover/`:
  `11.jpg` Tartaglia, `10.jpg` Capitano, `09.jpg` Pantalone,
  `08.jpg` La Signora, `07.jpg` Sandrone, `05.jpg` Pulcinella,
  `04.jpg` Arlecchino, `03.jpg` Columbina, `02.jpg` Il Dottore,
  `01.jpg` Pierro, `06-scaramouche.jpg`, and `06-wanderer.jpg`.

This is 26 images in total: 2 backgrounds and 12 portraits for each
world. Horror images are NEVER replaced by regular takeover portraits.

The first-pass art is generated illustrative reinterpretation, **not official
Genshin assets** or an exact visual-identity guarantee. Before production,
visually approve each of the 11 portraits against the original Genshin
character appearance, especially Sandrone and Pierro. Reject a wrong character
rather than allowing a generic portrait. Rank six is intentionally different
between the two bots: Scaramouche's *Balladeer's Shadow* is a cracked puppet
with dense purple smoke/strings, while Wanderer's *Wanderer's Echo* keeps
Wanderer's recognizable silhouette. The corruption overlay is keyed to the
existing boss lore:

| Rank | Preserved horror visual motif |
|---|---|
| 11 Tartaglia | Abyssal black Hydro and parasitic water |
| 10 Capitano | Empty armor leaking living black fog |
| 9 Pantalone | Cursed gold and coin-like growths |
| 8 La Signora | Ash, ice, and lethal Cryo-Pyro flames |
| 7 Sandrone | Flesh, puppet joints and gears |
| 6 Balladeer/Echo | Cracked doll, broken strings, haunted smoke |
| 5 Pulcinella | Haunted decrees and bureaucratic corruption |
| 4 Arlecchino | Black fire / fractured porcelain |
| 3 Columbina | Dream-threads and feather-like distortions |
| 2 Il Dottore | Clone fractures and clinical mechanisms |
| 1 Pierro | Frozen throne and cracked reality |

The takeover world uses the same character identity in the **un-corrupted**
military occupation setting rather than putting monster corruption everywhere.
World backgrounds, facial art and corruption are composited independently.

## Operation & safe failure
- Pillow is already in both requirements files; no new service, art-generation
  API, databases, scopes or external image hosting are added.
- `RPG_ART_ENABLED=0` disables the layer instantly without altering the game.
- New renders run in `asyncio.to_thread` to avoid blocking the event loop.
- If any asset is missing, the renderer uses a neutral *environment-only*
  placeholder, never a misleading substitute portrait.
- Rendering exceptions fall back to the original embed/message, including all
  original Discord buttons. Keep all art assets under the declared directory.
- The art pack is **not yet embedded in the GitHub draft PR**. The PR is a
  code/integration candidate; release requires including approved art assets,
  complete tests on that final art-inclusive SHA, private Discord staging
  checks, and explicit owner approval.
- No existing live Discord bot, Oracle process, or database was altered by
  creating this draft.

## Release gates
1. Curate all portrait appearances against actual game designs. Correct
   wrong costumes, headwear, hair, masks, and the rank-six puppet treatment.
2. Stage the art pack and both branch candidate SHAs in isolated environments.
3. Verify full-suite CI, both world buttons, both character origins, Vision
   overlay, all 11 bosses, dice 1/13/20, real saved-point math, seals and
   medal/defeat screens, fallbacks with missing/corrupted JPEGs.
4. Confirm no extra unwanted pings or RPG narrative changes; never deploy,
   restart, change live assets, or merge without the owner's approval.


## Code-hardening update (2026-10-10)

The user halted image generation and requested code-only continuation.

- Separate `assets/rpg/portraits/horror/` and `assets/rpg/portraits/takeover/`
  folders are now supported. Horror NEVER silently uses an ordinary/takeover
  portrait as its substitute. A legacy `assets/rpg/portraits/11.jpg` etc.
  file may be used for takeover only until that artwork is migrated.
- The rank-six filenames remain distinct by bot: `06-scaramouche.jpg` and
  `06-wanderer.jpg`, in *each* world's folder. Final horror forms must be
  clearly altered versions of the actual characters, not shared generic monsters.
- Artwork is now `RPG_ART_ENABLED=1` **explicit opt-in**, default OFF,
  until the artwork is approved and packaged; leaving the flag unset preserves
  the original text-only experience.
- The world-backdrop cache notices when approved files are installed/replaced.
- Preparing a Discord attachment happens before changing the original embed,
  so an asset-construction failure cannot leave a broken attachment URL.
- Existing portrait generation is not resumed, no assets are added to GitHub,
  and there are no service operations in this code-only patch.


## Read-only asset release gate

Run `python rpg_artcheck.py --root assets/rpg --strict` on the curated
art pack before enabling `RPG_ART_ENABLED=1`. It checks that all 26 JPEGs
are present, usable, sufficiently large, and not duplicated between horror
and takeover variants or between the two rank-six identities. Use
`--allow-partial` during curation to accept missing files but still reject
invalid or duplicate art. **Human approval of canonical likeness, costume,
and lore-specific corruption is still required**; the validator cannot
judge visual similarity or artistic accuracy. Do not deploy the older
unapproved art ZIP or auto-approve generated likenesses.
