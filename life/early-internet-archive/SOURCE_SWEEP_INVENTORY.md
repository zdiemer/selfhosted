# Breadth-first source inventory

Updated 2026-09-19. This register separates identity evidence, indexed leads,
and locally preserved artifacts. A search-engine result is discovery evidence;
it does not count as preserved until the original response or an archive
capture is frozen with provenance.

## Priority register

| Source | Identity | Discovered footprint | Preserved now | Next recovery pass |
|---|---|---:|---:|---|
| Giant Bomb | Confirmed `StarFoxA` | 5,266–5,267 forum posts, 12 reported reviews, and a larger historical artifact footprint | 148 profile snapshots; 103 artifact records; ten full reviews; hundreds of exact-page captures | Parse recovered blogs and lists, recover the *Fluid* body, identify the twelfth review, then enumerate forum posts |
| GameSpot | Confirmed `StarFoxA` | Five indexed reviews from 2007–08 | Search-index text and five exact seeds | Recover the pages, locate the historical profile/review index, and enumerate remaining reviews |
| Animal Crossing Community | Confirmed `Irock` | Joined in 2004; account later deleted for inactivity | First-person NSider2 evidence | Recover the historical profile and enumerate posts carrying the `Irock` byline |
| Twitter | Confirmed `@zach_diemer` | Profile known; tweet inventory unknown | Identity link on preserved 2019 personal site | Inventory exact profile/tweet URLs in web archives |
| FSU coursework | Confirmed `zdiemer` | Four original repositories, 2014–16 | Fully preserved under `web/fsu` | Add project records to the archive catalog; no new scrape required |
| Personal site | Confirmed | 2019 React portfolio and outbound identity links | Preserved under `web/old-diemer-codes` | Add a catalog record and retain a rendered snapshot |
| Steam | Confirmed `StarFoxA` / ID `76561197993375857` | Public profile, two reviews, 12 screenshot IDs | Four live targets frozen | Parse attributed reviews and screenshot metadata |
| GOG | Confirmed `StarFoxA` / ID `809111026997` | At least three posts from 2008–09 | Profile and three complete thread pages frozen | Parse the four known posts and enumerate the account's remaining forum history |
| Backloggd | Confirmed `starfoxa` | 1,603 played games; 673 reviews | Profile and all 45 review pages frozen | Import reviews; then inventory lists and diary data |
| ChipMusic | Confirmed `StarFoxA` | One known 2011 topic | Profile and complete topic frozen | Parse the authored topic post |
| Clacky's Hut | Membership confirmed; handle unresolved | Repeated participation from 2007–08 | First-person NSider2 evidence | Recover forum/profile patterns from archived captures |
| GIMPTalk | Membership confirmed; handle unresolved | At least one thread participation | First-person NSider2 evidence | Resolve the thread and profile URL |
| NationalSigLeague | Participation confirmed; handle unresolved | One exact 2008 thread and linked signature | Exact URL seeded | Recover the thread and identify the byline |
| XboxAchievements | Confirmed `StarFoxA` | 25-post account; one known 2009 thread | Search-index text; exact thread seeded | Resolve the profile and enumerate posts |
| DS Fanboy | Confirmed `StarFoxA` | One known 2008 article comment; profile ID `1596471` | Original page frozen live | Recover the historical profile/comment inventory |
| SuperCheats | Confirmed `StarFoxA` | One full 2007 Action Replay code submission | Original frozen; GameFAQs attribution thread seeded | Search for additional submissions |
| itch.io | Confirmed `starfoxa` | Profile registered in 2015; one public comment | Live profile and comment plus archive captures frozen | Check historical profile captures for deleted activity |
| Neoseeker | Confirmed `starfoxa` | One indexed 2022 *Fantasian* walkthrough correction | Full comment text retained by the search index; origin returns 403 | Retry archives and enumerate the account profile |
| PlayStation Underground | Membership confirmed; handle unresolved | Posting and December 2007 ban described in first-person | NSider2 evidence | Resolve the historical forum handle and profile URL |
| Mathematics Stack Exchange | Confirmed `starfoxa` | Unregistered 2014 account with two questions and follow-up comments | Profile frozen live; both complete question pages frozen from Wayback | Parse questions and attributed comments into the catalog |
| The Spriters Resource | Confirmed `StarFoxA` | One known *Uninvited* NES scene-sheet contribution | Live and archived metadata pages plus original 1798×3680 PNG frozen | Resolve the contributor profile and check for more assets |
| Chrono Compendium | Membership confirmed `StarFoxA` | Exactly one post by first-person account | NSider2 evidence only | Solve the site's guest-search verification or recover its member index |
| Kevan brain | Confirmed `StarFoxA` artifact | Personal interactive page created in 2005 | Exact URLs seeded; origin offline | Recover an archive capture |
| WikiSider | Confirmed subject `STARFOXA` | Third-party profile about NSider status | Exact URL and first-person discussion | Recover the page as third-party context only |

## Giant Bomb evidence

NSider2 post `7559449` answers “A website. MY PROFILE” with
`http://www.giantbomb.com/profile/starfoxa/`. Post `10666370` calls the same
profile “my life for the past two years.” Other first-person posts say the
account moved primarily to Giant Bomb. Current indexed pages attach the
`StarFoxA` byline and expose a footprint of 12 reviews, 12 user lists, about
5,266 forum posts, and 260,824 wiki points.

The recovered profile snapshots produced 103 exact artifact records: 15 blogs,
27 lists, and 61 image records. The dedicated review index then exposed ten
historical review IDs plus *Fluid*, whose body remains missing. Ten complete
reviews from 2008–12 are now in the public catalog. The historical and modern
artifact-route formats are both retained; this recovered multiple deleted
pages that the modern form missed.

Known remaining high-value seeds include the authored 17-BIT thread and a
Chrono Trigger cease-and-desist thread. See `GIANTBOMB_INVENTORY.md` for the
route-level accounting and parser workflow. Live pages remain
Cloudflare-protected, so recovery continues from exact archive indexes rather
than treating live 403 responses as loss.

## GameSpot evidence

Five directly indexed reviews have an explicit `StarFoxA` byline and dates:

- *Metroid Prime 3: Corruption*, 2007-09-20;
- *Wii Channels*, 2007-09-25;
- *Marvel: Ultimate Alliance*, 2007-09-28;
- *Super Mario Galaxy*, 2007-11-16;
- *Professor Layton and the Curious Village*, 2008-04-29.

The pages expose substantial full review text, making them strong immediate
recovery targets. First-person NSider2 posts also describe receiving ratings,
private messages, and watching streams on GameSpot. The profile URL and total
review count are not yet established.

## Animal Crossing Community evidence

The ACC handle is `Irock`. In the same ACC discussion where post `2538650`
says the account had been deleted for inactivity and was created in 2004,
NSider2 post `2539114` states, “my old username was irock btw.” Post `546322`
independently identifies Animal Crossing Community as the author's first forum;
posts `1229000` through `1229063` connect `Irock`, age 11, and that first-forum
period; post `3076463` says the author had left ACC “eons ago.” This is a
first-person identity chain, not a fuzzy username match. `Parker94` belongs to
a different participant in the quoted conversation and remains specifically
excluded.

The initial recovery seed uses ACC's historical profile pattern
`accf_profile.asp?UserName=Irock`. Archive indexes must confirm the URL before
any profile fields are promoted into the catalog.

## Twitter and FSU evidence

The preserved personal site at `web/old-diemer-codes/site/src/Site.jsx` links
directly to `https://twitter.com/zach_diemer`, alongside the confirmed GitHub
and LinkedIn identities. This is first-party identity evidence, although it
does not establish that tweets survive.

The FSU source is already a preservation success rather than a scrape target.
`web/fsu` presents four original coursework repositories: Cloysta, Breakout,
pybank, and my-data-structure. The remaining task is to project these into the
archive catalog without duplicating or rewriting the original Git history.

## Newly confirmed GOG continuity

The GOG account is no longer merely a username match. On 2008-09-11, NSider2
posts `4539521` and `4539549` say the author had just joined GOG's beta and
bought *Fallout* and *Fallout 2*. A `StarFoxA` GOG post from the same day says
it is the account's first time playing Fallout and celebrates the same
two-for-one purchase. The live pages also expose GOG user ID `809111026997`.
The profile and three known thread pages are frozen locally; at least three
authored posts are visible across them.

## Confirmed small-site leads

NSider2 supplies first-person evidence for several otherwise hard-to-index
communities. Posts `1561534`–`1561599` identify the Kevan brain page as an old
personal artifact made in 2005. Post `2549187` links an exact
NationalSigLeague thread and says the signature on the left is the author's.
Post `3207259` confirms participation in a GIMPTalk thread. Dozens of posts
confirm active Clacky's Hut membership, including use of its shoutbox and game
pages, though the historical account URL is not yet resolved.

The archive owner explicitly confirmed that exact `StarFoxA` identities may be
attributed to them. This promotes the XboxAchievements, DS Fanboy, SuperCheats,
ChipMusic, Backloggd, itch.io, and Neoseeker accounts from evidence-supported
candidates to confirmed sources. The SuperCheats lead retains the full
September 2007 Pokémon Diamond code submission, and later GameFAQs threads
repeatedly credit `StarFoxA` for it. The itch.io profile and its lone visible
*Bittersweet Birthday* comment are frozen live. Neoseeker's index exposes a
January 2022 technical correction on its *Fantasian* Metal Ribbidon guide, but
the origin and archive payload still need recovery.

NSider2 also confirms PlayStation Underground membership without resolving a
handle: posts `993821` through `995163` describe beginning to post there and
then trying to bypass a December 2007 ban. This remains a membership lead, not
an attributed `StarFoxA` account.

The broader exact-handle pass found an unregistered Mathematics Stack Exchange
profile with two 2014 discrete-mathematics questions and several follow-up
comments. Both question pages survive in full. The same pass found a credited
*Uninvited* NES scene sheet at The Spriters Resource; its 497,240-byte original
PNG is verified at 1798×3680 pixels and frozen alongside the page metadata.

## Reproducible sweep

`source_sweep_seeds.json` holds confirmed identities and exact known URLs.
`scrape_source_sweep.py` records Wayback and Common Crawl index checks in
`data/source_sweep/source_sweep.sqlite3`. Its default Common Crawl pass samples
every pre-2014 legacy index and one index per later year; `--all-collections`
fills every available index after the breadth pass.

```bash
python3 scrape_source_sweep.py --skip-wayback
python3 scrape_source_sweep.py --download --skip-wayback
python3 scrape_source_sweep.py --source animal_crossing_community --skip-wayback
python3 scrape_source_sweep.py --live --skip-wayback --skip-common-crawl
python3 scrape_source_sweep.py --all-collections --skip-wayback
python3 scrape_source_sweep.py --status
```

Raw Common Crawl and Wayback responses are stored gzip-compressed beneath
`data/source_sweep/raw/` and linked to their collection, WARC filename, byte
range, capture time, digest, and exact seeded target in SQLite. Wayback errors
remain retryable; an index outage never becomes a false zero-result check.
The live pass follows the same rule: successful responses are frozen with
digests and final URLs, while DNS, origin, and HTTP failures remain errors.

Current sweep state: 23 identity records, 213 exact targets, 334 completed or
retryable index checks, and 571 capture records across 80 targets. The target
count includes 133 routes dynamically discovered from frozen Giant Bomb
profiles plus 30 historical and modern routes discovered from its review
index. Every indexed capture currently has a frozen raw payload. Common Crawl
index errors remain queued for retry.
