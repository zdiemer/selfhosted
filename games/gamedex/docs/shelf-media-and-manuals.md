# Openable boxes: cartridges, discs, and manuals

**Status:** deferred (2026-07-13). ScreenScraper's site was down when we went to wire it up.
Everything below is the state of the investigation, so none of it has to be rediscovered.

**The idea.** A box on the Shelf can be *opened*. The media comes out — a real cartridge or a
real disc, modelled to look like its actual counterpart — and the manual is there too, readable.

---

## 1. What the sources actually give us (all probed, not assumed)

### Manuals — Archive.org ✅ FREE, NO AUTH

The `gamemanuals` collection holds **7,552 items**. Targeted searches hit real scans:

| Query | Result |
|---|---|
| Chrono Trigger | `chrono-trigger-usa` |
| Mega Man 2 | `mega-man-2-nes-manual` |
| Sonic the Hedgehog | `sonic-2-pdf`, `sonic-cd-jp-manual` |
| Super Mario World | `manual-pt-supermarioworld-snes` |

Search endpoint (works unauthenticated):

```
https://archive.org/advancedsearch.php
  ?q=mediatype:texts AND title:("<TITLE>") AND (manual OR instruction)
  &fl[]=identifier&fl[]=title&rows=5&output=json
```

Then `https://archive.org/metadata/<identifier>` lists the files (PDF / JP2 / page images), and
`https://archive.org/download/<identifier>/<file>` serves them.

**Watch out:** a plain keyword search is very noisy — "Super Mario World manual" returned a Voice
of America broadcast as its top hit. It *must* be constrained with `mediatype:texts` and a title
match, and even then results want validating (platform in the title, year, publisher) before we
attach one to a game. Reuse `MatchValidator` the way the other sources do.

### Disc faces — GameTDB ✅ FREE, NO AUTH

Real disc art, verified live:

| URL | Result |
|---|---|
| `art.gametdb.com/wii/disc/US/RMGE01.png` | 200, 61,629 b |
| `art.gametdb.com/wii/disc/US/GALE01.png` (GameCube) | 200, 48,480 b |
| `art.gametdb.com/wii/coverfullHQ/US/RMGE01.png` | 200, 1,126,253 b |
| `art.gametdb.com/3ds/box/US/AREE.png` | 200, 90,757 b |
| `art.gametdb.com/switch/box/US/<titleid>.png` | 404 — Switch needs the real title id, not a guess |

Covers **GameCube, Wii, Wii U, DS, 3DS, PS3** (and Switch boxes, given a correct title id).

Keyed on the platform's own game id (`RMGE01`), which we do **not** have. GameTDB publishes full
database dumps that map id ↔ title — all returned `200`:

- `https://www.gametdb.com/wiitdb.zip?LANG=EN`
- `https://www.gametdb.com/PS3TDB.zip?LANG=EN`
- `https://www.gametdb.com/SwitchTDB.zip?LANG=EN`

So the join is: our title → GameTDB dump → game id → art URL. Cache the dumps on the PVC and
refresh occasionally; they're small and static.

**RomM may already know the id.** Its ROM records carry file names and often the serial. Worth
checking before building a title matcher — a serial is an exact join and a title is a guess.

### Cartridge labels — ScreenScraper ⚠️ BLOCKED ON CREDENTIALS

**libretro-thumbnails is NOT a source for this.** Confirmed: it carries only `Named_Boxarts`,
`Named_Logos`, `Named_Snaps`, `Named_Titles`. No cartridge or disc scans. Don't go back to it.

ScreenScraper is the real bulk source for physical-media scans (it calls them *support* images:
cart labels, disc faces, floppies) **and** it carries manuals too. It needs **two** credential
pairs, and it checks the developer one first — every endpoint refuses without it:

```
$ curl 'https://api.screenscraper.fr/api2/jeuInfos.php?output=json&romnom=Chrono%20Trigger.sfc&systemeid=4'
Erreur de login : Vérifier vos identifiants développeur !

$ curl 'https://api.screenscraper.fr/api2/systemesListe.php?output=json'
Erreur de login : Verifier vos identifiants developpeur !
```

| Credential | What it is | Status |
|---|---|---|
| `devid` / `devpassword` | a **developer key**, issued per piece of software, on request via their forum | **we do not have this** |
| `ssid` / `sspassword` | the **user account** — raises quota, unlocks higher-res media | Zach has one |

A user account alone is not enough. The dev key is free but a human grants it, so it takes a
day or two.

**When we have them**, they go in `games/gamedex/values.local.yaml` (gitignored, same as the IGDB
and RomM secrets — never in chat, never in git):

```yaml
screenscraper:
  devId: "..."
  devPassword: "..."
  user: "..."
  password: "..."
```

…then through `templates/secret.yaml` → `deployment.yaml` env → `src/screenscraper.py`, exactly
like `igdb.py` and `romm.py` do it.

API shape once authenticated: `api2/jeuInfos.php?devid=&devpassword=&softname=gamedex&ssid=&sspassword=&output=json&systemeid=<n>&romnom=<name>`
returns a `jeu` object whose `medias[]` array carries typed entries — the ones we want are the
**support** media (the cartridge/disc scan) and **manuel** (the manual). Note it is rate-limited
per account and returns a quota in every response; be a good citizen and cache hard, one lookup
per game forever, like `enrich.py` already does.

---

## 2. The art plan: three tiers, so nothing is ever a blank shell

1. **Derived from the box art** (default, zero dependencies, works for all 14,746 games).
   Crop the cover onto a platform-correct label template. This looks convincing because a real
   cart label usually *is* a crop of the cover art. Every game gets something on day one.
2. **Real scans from ScreenScraper** (when the dev key lands). Genuine cart labels and disc faces
   for essentially the whole retro library. This is the tier that actually satisfies "it must look
   like its real counterpart."
3. **Hand-uploaded** — point the existing cropping uploader (`openCoverEditor`, `shelf.py
   set_cover`) at the cart/disc face too. Same machinery, new `kind`.

Disc faces additionally come from GameTDB where it knows the game (see above), which is likely
*better* than ScreenScraper for GameCube/Wii/PS3.

**Honest caveat, worth restating to Zach:** the geometry can be genuinely right. The label art is
only *truly* real via ScreenScraper or his own uploads. The derived fallback will look good, but a
purist knows a Chrono Trigger cart label isn't a crop of the Chrono Trigger box.

---

## 3. The models

The Shelf is already CSS 3D with textured faces (`static/shelf.js`, `.sh-stage`, `preserve-3d`,
one 3D case at a time). A cartridge is the same machinery with a different silhouette, so this
needs no new rendering approach — but each shell has to be *specifically* shaped or it's just a
grey slab:

- **NES** — chunky slab, deep front bezel, label recessed
- **SNES** — curved shoulders, ridged grip, the distinctive tapered top
- **N64** — tall shell, finger grooves, label high on the face
- **Game Boy / GBC** — small, notched corner (the anti-insert cut)
- **GBA** — stubby, rounded, label nearly the whole face
- **DS / 3DS / Switch** — flat cards, tiny; Switch has the distinctive notch
- **Genesis / Mega Drive** — tall, angled, ridged
- **Optical** — a real clear hub ring, a data-side rainbow sheen (a conic-gradient does this well),
  and the **GameCube mini-disc** is a smaller diameter, which people notice
- **Manual** — a booklet with visible page edges and a slight fan

Platform brand colours for the shells are already in `SPINE_LOGOS` / `spineStyle()` in `shelf.js`
— reuse them rather than inventing a second table.

### The opening interaction — OPEN QUESTION, ask Zach

Two physically honest behaviours, and they're not the same:

- **Hinged case** (optical discs, DS/Switch/modern carts): the case opens like a real case, the
  disc seated on its hub in one half, the manual in the lid.
- **Slide-out** (classic cartridges): a cart never lived in a hinged case — it comes *out of* the
  box and you hold it up.

My instinct is to do both, chosen by platform. Zach hasn't ruled on whether he'd rather it be
uniform. **Ask before building.**

---

## 4. Where this plugs in

- `src/screenscraper.py` — new source client. Model it on `src/keitai.py` (rate limiter,
  `serves(platform)` gate, `match(game)` returning a record with `confidence`, and
  `override_from_url()` so a wrong match can be fixed by hand from the drawer's mapping control).
- `src/manuals.py` — Archive.org client. Same shape. Gate it on retro platforms; there's no point
  asking about a 2024 PC game.
- `src/gametdb.py` — dump-backed id lookup + disc art URLs. Cache the dumps on the PVC.
- `src/enrich.py` — register both as sources; add their light fields to `_IGDB_LIGHT` /
  `_FACET_LIGHT` (`cartArt`, `discArt`, `manualUrl`, `manualPages`) and add a `backfill_media()`
  in the shape of `backfill_extras()` — **and remember what that one taught us: chunk it, retry
  with backoff, and commit each chunk as it lands.** A 429 four seconds in threw away the whole
  first pass.
- `static/shelf.js` — the models, the open/close animation, and a media face on the 3D case.
- New `static/manual.js` — the page-turning reader.

---

## 5. Facts about the library, for scoping

Measured 2026-07-13:

| | |
|---|---|
| Games total / owned | 14,746 / 7,570 |
| No box art at all | 283 |
| No metadata match at all | 188 |
| Owned on RetroAchievements-class platforms | 428 |
| Owned GameCube/Wii/Wii U/DS/3DS (GameTDB disc art territory) | ~1,326 modern Nintendo + retro |

---

## 6. Next actions

1. **Zach:** request `devid`/`devpassword` from ScreenScraper (their forum), then put all four
   credentials in `values.local.yaml`. Their site was down on 2026-07-13 — retry.
2. **Zach:** decide hinged-case vs slide-out vs both (§3).
3. **Claude:** the parts that need nothing from anyone, and can start any time —
   - the manual reader on Archive.org (nothing else in the app gives you this)
   - GameTDB disc art (check RomM for serials first, it may make the join exact)
   - the cartridge/disc geometry and the open animation
   - derived cart labels from box art
4. **Claude:** when the dev key lands, slot ScreenScraper in *above* the derived labels. Everything
   built before it keeps working — it just gets better art.
