# apartment-watch — Bay Area rental scraper → SMS

A CronJob that scrapes studio/1br listings across San Francisco, southern Marin
and Burlingame, filters them against `criteria.yaml`, and texts a link to
anything new that matches. The listings themselves live on a run page served by
a small read-only web pod (`homes.diemer.codes`).

```
hourly 07:00-22:00 PT          once a day, 09:00 PT
   ├─ dahlia (BMR, JSON API)      ├─ zumper ───────┐
   └─ craigslist (plain httpx)    ├─ apartments.com├─ Camoufox (Firefox, Xvfb)
      × 3 search areas            └─ zillow ───────┘
                    │
                    ▼
     evaluate vs criteria.yaml ──▶ SQLite on a PVC (dedup + price history)
                    │                     │
                    │                     └──▶ web pod ──▶ homes.diemer.codes/r/<token>
                    ▼   only listings that newly match, and only at 09/13/18
     POST /api/v1/messages ──▶ sms-relay.infra ──▶ Android handset ──▶ you
```

## Scraping often is not texting often

These are separate cadences on purpose, and conflating them is the obvious way
to make this tool annoying enough to ignore.

**Polling frequently doesn't create more matches** — dedup means a listing is
texted once, ever — it just finds them sooner, which is the whole game on
cheap listings. So the cheap sources run hourly (`every_hours: 1`) and a
digest only goes out during `alerts.send_hours`. Anything found in between
keeps `notified_at` NULL and leads the next window's digest.

The browser sources stay daily (`every_hours: 24`). Not for compute: a poll
there is a full stealth-browser navigation, and doing that sixteen times a day
from one residential IP is how the IP gets flagged — which loses the source
outright rather than just one run. `every_hours` is per-source so that trade is
visible and adjustable.

After the first run an hourly tick costs about three seconds, because detail
pages are only fetched for listings not already in the database.

## Why it's in this repo

The root README's convention is that apps we write live in their own repo and
come back as a submodule. This one deliberately doesn't — it's small, it has one
consumer, and the whole thing is a CronJob plus four parsers. The in-repo
precedent is [`minecraft/claude-bridge/`](../../minecraft/claude-bridge/):
chart, `Dockerfile`, and `src/` all live here, and the image still ships to
`ghcr.io/zdiemer/apartment-watch`.

## Two numbers, two audiences

`alerts.to` is whoever is flat-hunting. It receives **listings and nothing
else** — a headline, a price range, and a link:

```
apartment-watch 8/4 - 3 new, $2372-$3308
https://homes.diemer.codes/r/Ab3xY9kQ
```

No matches, no text. Not a "nothing today" message, not a note about a scraper:
silence is the signal, and anything else in that thread makes the thread easier
to ignore. (The digest used to append source-health warnings, which is how a
text arrived reading `3 new, $2400-$3000 <link> dahlia: 0 listings for 3 runs`
— information the reader can't act on, attached to the one message they must.)

`alerts.health_to` is whoever maintains this, and only hears the other thing.

## Silence is a feature, and that's a risk

A run with no matches sends nothing, which is the point — but it makes one
failure mode dangerous: a parser that silently breaks looks exactly like a quiet
market, forever.

So `source_health` tracks consecutive empty runs per source, and if a source
goes quiet for `health_alert_after_stale_runs` runs (default 3) or throws, the
next zero-match run texts **`alerts.health_to`** — rate-limited to once every
three days so a permanently dead source can't train you to ignore it. Leave
`health_to` blank and it's logged instead. It never goes to `alerts.to`.

## What actually works, and what doesn't

Verified against the live sites in August 2026, from a residential IP:

| Source | Wall | Status |
|---|---|---|
| **DAHLIA (BMR)** | none | SF's official Below Market Rate portal, a public JSON API. Income-restricted units far under market that appear nowhere else. Marked `trusted`: skips the scam filter (below-market is the point) and the laundry rule (no amenities published). **Not neighbourhood-filtered** — it publishes no coordinates — which is a deliberate accept, since BMR inventory is small and worth seeing regardless. |
| **Craigslist** | none | Serves a no-JS result list that honours the query string. No browser at all. Detail pages carry structured `laundry=`/`parking=` codes and lat/lon. **The load-bearing source.** |
| **Zumper** | F5 "Client Challenge" | Camoufox clears it. Ships laundry, bedrooms, and address inline as schema.org JSON-LD — zero detail fetches. |
| **Apartments.com** | Akamai Bot Manager | Camoufox clears it **only with a warm-up**: a cold request gets a 2.5KB challenge shell every time, but loading the homepage, waiting ~12s, then navigating in-site returns the real 800KB page. See `Fetcher.warm_up`. |
| **Zillow** | PerimeterX | Search page: cleared. **Detail pages: still 403**, and laundry only exists there. Combined with a list that skews to $3,500+ new-builds, a live run produced 41 cards and 0 matches. Left enabled because it costs ~20s and no successful detail fetches, but expect nothing from it — and left **San Francisco only**, since tripling the browser navigations of the least productive source is the worst trade available. |
| **HotPads** | PerimeterX | **Blocked** even from a warmed browser session. Not implemented: it's Zillow-owned, so its inventory is already covered. |
| **PadMapper** | F5 | Renders, but its listings come from a map XHR with nothing parseable in the HTML. Not implemented: it's Zumper's own site with Zumper's own inventory. |

A note on DAHLIA's deadlines: of 57 rentals in a live pull, 55 were past their
application due date — but 46 of those are "Lease Up" (lottery already run,
filling from the existing list) and 9 are still "Active" with units available
and an open waitlist. Only the former are dropped; the latter are surfaced
marked `BMR waitlist?`. Treating a passed deadline as closed threw away most of
the source.

Measured on one live run each: **Craigslist 198 results → 4 matches**,
**Apartments.com 35 → 7**, **Zumper 25 → 1**, **Zillow 41 → 0**. Craigslist and
Apartments.com are carrying this.

Expect this table to rot. Craigslist has been stable for a decade; the other
three are an arms race, and the health alert above is what tells you a round
was lost.

**Warming up is opt-in, and that's deliberate.** Only Apartments.com warms
eagerly. Warming Zillow's ~1.9MB list page up front stalled the run for minutes
and bought nothing. Every browser source passes `origin=`, which arms a
one-shot warm-and-retry *if* a cold fetch comes back blocked — so a site that
starts demanding a warm session gets one automatically, without paying for it
daily.

## Photos

Every source now has one, and none of them are hot-linked.

| Source | How |
|---|---|
| **Craigslist** | `sapi.craigslist.org` — the search API its own front-end calls. The static HTML has no photos at all, only an `imageConfig` naming the CDN, but SAPI returns an image ref per result: **one extra call photographs the whole run**, no browser. `searchPath=<subarea>/apa` is what scopes it, one call per area — the default batch spans all of sfbay and yielded 9 SF posts out of 360, against 263 of 263 with `sfc/apa`. Joined to the static results by URL slug, ~80% hit rate. Found by reading the sibling `talaria` repo, which uses this endpoint wholesale. |
| Zumper | `image_ids` on the result object |
| Zillow | `imgSrc` on the result card |
| DAHLIA | `imageURL`, on ~90 of 122 listings |
| Apartments.com | In the card markup, not the JSON-LD |

**Photos are re-hosted through `/img/{source}/{external_id}`.** Apartments.com
returns 403 to a foreign referrer, so its photos never render when hot-linked;
fetched server-side with a browser User-Agent and the listing page as Referer,
the same URL returns 200. Routing every source through one path also means one
policy instead of four.

That endpoint looks up the URL **in the database** by source and id — it never
accepts a URL from the query string. An open `?url=` proxy would let anyone use
this pod to fetch arbitrary internal addresses. Responses are cached in-process
(bounded, this pod has a 256Mi limit) and sent with a one-day `Cache-Control`.

A card with no photo renders compactly rather than reserving an empty 16:10
block. A photo that *fails* still holds its space, which avoids layout shift
after paint.

## The rules

`criteria.yaml` is the entire tuning surface, and it's a **ConfigMap** — editing
it and running `./upgrade.sh` needs no rebuild.

- **$3800/mo**, compared against *effective* rent: base plus any stated monthly
  parking fee. Where parking is required, its cost comes out of the budget
  rather than sitting beside it.
- **Parking is a distance rule.** Within `required_beyond_miles` (3) of
  `rules.parking.reference` — 2120 Broadway, the daily destination — it's a
  perk. Past that it's required, because that's where not having a car stops
  being a preference. Since `unknown` isn't an accepted parking value, a
  far-out listing that never mentions parking is dropped too; near work, the
  same listing sails through. A listing we **can't place** is never gated:
  unknown distance is not "far", and dropping a coordinate-less SF post over a
  guess costs more than it saves. The distance shows on every card.
- **In-building laundry** is a hard requirement. `unknown` does *not* pass — a
  listing that never mentions laundry usually doesn't have it.
- **Where we look** is `search.areas`, resolved by point-in-polygon:

  | Area | Polygons |
  |---|---|
  | `san_francisco` | `data/sf-neighborhoods.geojson` — DataSF "SF Find Neighborhoods" (`gfpk-269f`), 117 polygons |
  | `southern_marin` | `data/search-areas.geojson` — 16 Census places from Sausalito to San Rafael, plus a **fill**: Marin County clipped to the 101 corridor |
  | `burlingame` | `data/search-areas.geojson` — the city |

  The fill exists because incorporated towns don't tile a county. Greenbrae,
  Bon Air and the edges of Ross Valley sit in unincorporated gaps, and without
  it a perfectly good Greenbrae listing resolves to nowhere and is dropped as
  "outside search area". Places match first, so the fill only ever names
  somewhere no town claimed. Novato and West Marin fall outside the box.

  Per-site plumbing — Craigslist subarea codes, URL slugs, the city strings
  each site prints — lives in `src/areas.py`, not in `criteria.yaml`. Each
  source filters its own results first (cheap, and it saves detail fetches in
  Santa Rosa), then coordinates decide (authoritative). Craigslist's `nby`
  subarea is mostly Sonoma: a live pull was 332 results, 288 of them dropped
  before a single detail fetch.

  **`sf_records` is the sharp edge.** Rent control and the address-beats-pin
  geocode both query the *San Francisco* assessor, so only `san_francisco` may
  ask. A Burlingame "1200 Broadway" would otherwise match San Francisco's 1200
  Broadway and teleport the listing across the bay.
- **Neighborhoods** are excluded (`exclude_neighborhoods`) by the same
  point-in-polygon result, in this order:

  1. **The street address**, when the listing states one, geocoded against the
     SF assessor's parcel records. Craigslist has no address field, so it's
     lifted from the title ("755 O'Farrell Street #44").
  2. **The map pin**, when there's no address.
  3. **The poster's neighborhood label**, only when there's neither — posters
     mislabel constantly, since "Potrero Hill" reads better than "Bayview".

  The address outranks the pin because posters place pins by hand and get them
  wrong: a listing at 755 O'Farrell, squarely in the Tenderloin, carried a pin
  800m north in Nob Hill and so escaped the exclusion entirely. Lookups are
  cached forever per address and share the rent-control cache, so an address
  costs at most one request ever.
- **Studio or 1br.**
- **Scam filter** (`src/scam.py`): scored, not a keyword blacklist. Payment-rail
  tells (wire, gift cards, crypto), absentee-landlord narratives, screening
  waivers, lead-harvesting ("leave your phone number and tell me about
  yourself" — the loudest tell in practice, since a real landlord offers a
  showing), and price each add points; several have to agree before a listing
  is dropped. Single weak signals can't kill a real bargain, which matters when
  finding bargains is the whole job. Rejections record which rules fired.

  Price thresholds are **derived from real market rent**, not hand-picked, so
  re-tuning means updating four numbers in `scam_filter.market_rent` rather
  than guessing. Below `bait_ratio` (60%) of market a listing is dropped on
  that alone. Between 60% and `premium_ratio` (75%) it's only suspicious if it
  *also* claims in-unit laundry plus parking — cheap and over-amenitied is a
  unit that isn't there, but it's worth 3 points rather than being decisive,
  because an under-priced good unit is the entire point of this tool.

- **Room shares are rejected outright** — co-living, SROs, "private room", a
  bedroom in someone's flat. The brief is a place of your own. This is not part
  of the scam score and is not gated on `trusted`, because these aren't fraud
  and the trusted source is the one that needs it most: DAHLIA publishes SRO
  units, and an SRO is a room with a shared bathroom that reports itself as a
  studio to every bedroom check. Craigslist's own `sharedBa` / `splitBa`
  bathroom attribute is the most reliable tell there is — nothing with its own
  front door reports a shared bathroom — so the attr line is folded into the
  body text the rules read. Negations are stripped first: "no roommates" and
  "roommate-free" describe the thing we want.

  These ads are titled like apartments. `Center North Beach w/stunning views -
  fully furnished` was a bedroom in a two-bed Edwardian with a shared bath, and
  it only says so in the body — which is why the body is **stored**:

**Every rule must read the same text on every run.** A listing's detail page is
fetched exactly once; after that it comes back with only what the *search* page
shows, and `Store.hydrate` merges the stored detail back in before evaluation.
The posting body was the one field never persisted — hydrate substituted the
**title** — so every body rule (room share, the entire scam filter,
`exclude_keywords`) went blind on the second observation and could flip its
verdict. That is not theoretical: the room share above was correctly rejected
at 12:00 on the run that read its body, then passed at 13:00 and was texted.

So `listings.body` is a column, `hydrate` restores it, and `seen_ids` only
counts rows that actually have one — a row with no stored body was never really
parsed, and letting it cost one more detail fetch, once, is cheap and
self-limiting.

**Removed posts are dropped before they're texted about.** Craigslist answers
HTTP 410 for a post its author deleted or the community flagged down, and bait
gets flagged fast. Nothing else in the pipeline notices — a listing is scraped
once and `Store.hydrate` re-matches its stored detail on every later run — so
without this a post that died on Monday would still be in Thursday's digest.
Checked at send time, over `pending` and over the last week of already-sent
listings so existing run pages mark them "No longer listed" rather than sending
you to a dead page.

Only **404 and 410** count. A 403 is a bot wall and a timeout is the network;
treating either as "gone" would quietly delete good listings. For the same
reason only Craigslist is probed — the browser-walled sources answer 403 to a
plain request, which says nothing about whether the listing still exists.

Price drops come free: every listing is stored and **re-evaluated every run**, so
a unit that was $3200 in June and re-lists at $2950 matches then, and texts then.

## Prerequisites (one-time)

**1. An sms-relay API key.** `SMS_RELAY_API_KEYS` maps key → service name and an
unknown key is a flat 401. In the [`infra/sms-relay`](../../infra/sms-relay/)
checkout (a submodule — `git submodule update --init infra/sms-relay`):

```bash
openssl rand -hex 32
# merge into the EXISTING json — don't overwrite it, talaria's key is in there:
#   apiKeys: '{"talaria":"...","apartment-watch":"<new>"}'
./infra/sms-relay/upgrade.sh
```

**2. Both local files.** Neither is in git:

```bash
cp values.local.yaml.example values.local.yaml   # the API key from step 1
cp criteria.example.yaml     criteria.yaml       # phone number + stipulations
```

`criteria.yaml` is gitignored because it carries the alert phone number and a
personal shortlist of where to live.

## First install

```bash
./build.sh      # then set the GHCR package to Public (first push only)
./upgrade.sh    # namespace `web` already exists
```

`upgrade.sh` validates `criteria.yaml` through the real parser and verifies the
image tag is actually in GHCR before deploying — a typo otherwise means no
alerts until you happen to notice.

## Verification

The fast loop is local, and doesn't need the cluster or the image:

```bash
python -m venv .venv && .venv/bin/pip install -r src/requirements.txt
.venv/bin/python -m camoufox fetch

# No SMS, no DB writes — in-memory, so every listing looks new.
.venv/bin/python src/main.py --dry-run -v
.venv/bin/python src/main.py --dry-run --source craigslist     # one at a time
.venv/bin/python src/main.py --dry-run --no-camoufox           # cheap tier only
```

In-cluster:

```bash
kubectl -n web create job --from=cronjob/apartment-watch aw-manual-$(date +%s)
kubectl -n web logs -f job/aw-manual-...
```

Look for `[<source>/<area>]` counts on each of the three areas, `MATCH` lines,
any detail-fetch cap warning, and then either `queued sms <uuid>` or
`no new matches — staying quiet`.

Geography and the distance rule are checkable without any network at all:

```python
import geo
geo.resolve(37.9061, -122.5450, None)   # ('Mill Valley', 'southern_marin', 'coords')
geo.resolve(38.1074, -122.5697, None)   # (None, None, 'unknown')  — Novato is out
geo.miles_from(37.9061, -122.5450, (37.795073, -122.432554))   # 9.8
```

To prove the SMS path end to end without waiting for a real match, deploy once
with deliberately loose criteria (raise `max_effective_rent`, set
`laundry.required: false`, empty `exclude_neighborhoods`) — that's a real digest
through the real relay. Then put the real rules back and `./upgrade.sh`.

## Upgrade

```bash
./build.sh && ./upgrade.sh    # code changed
./upgrade.sh                  # only criteria.yaml changed
```

## Uninstall

```bash
helm uninstall apartment-watch -n web
kubectl -n web delete pvc apartment-watch-data   # deliberate: see below
```

The PVC has `helm.sh/resource-policy: keep`. The dedup history is the only thing
stopping the next run from texting you every listing in San Francisco at once, so
it survives an uninstall unless you go delete it on purpose.

## Notes and sharp edges

- **`XDG_CACHE_HOME=/opt/cache` in the Dockerfile is load-bearing.** Camoufox
  resolves its browser through platformdirs' `user_cache_dir`. The pod runs with
  `HOME=/tmp` on an emptyDir and a read-only root filesystem, so a browser
  cached under `$HOME` is masked at runtime, re-downloads 660MB, and then fails
  on the read-only mount. Same lesson as `money`.
- **One browser page for the whole run, not one per URL.** Playwright's
  `browser.new_page()` creates a *fresh isolated context* — new cookie jar, no
  history. Akamai and PerimeterX score a cold deep-link far worse than a second
  navigation inside a warmed session, so a page per URL throws away exactly the
  state that gets through the wall.
- **uBlock Origin is excluded** from the Camoufox launch. It's downloaded at
  launch, which costs startup time and writes into the browser cache — which is
  read-only here. It does nothing for scraping.
- **3Gi memory limit.** `money` had its sync OOM-killed (exit 137) at 1Gi
  rendering Zillow in the same browser.
- **Only listings whose URL actually went out are marked notified.** A digest
  spills across up to `max_messages_per_run` texts of
  `max_listings_per_digest` each; anything past that keeps `notified_at` NULL
  and leads the *next* run's digest. The first version put "+35 more" at the
  bottom and then retired all 40, so 35 listings you were never shown were
  never mentioned again. There is no UI to go and look at, so the overflow
  line has to be a promise rather than a dead end.
- **The SMS idempotency key is scoped by date *and* listing set.** sms-relay
  dedupes on `(service, idempotency_key)`. With a date-only key, the first send
  of the day wins and every later one returns 202 with the original message id
  having sent nothing — and the caller, seeing success, marks those listings
  notified so they're never mentioned again. Hashing the listing ids into the
  key keeps genuine retries deduped while letting a different set actually go
  out. This bit for real during the first deploy: a smoke run burned the day's
  key and the 40-match run that followed was silently swallowed.
- **`extraEnv` overrides the built-in env vars rather than appending.**
  Emitting the same name twice is legal and kubelet takes one of them, so it
  looks fine — but the next `helm upgrade` can't reconcile the duplicate in its
  strategic merge patch and drops the base entry entirely.
- **Politeness is enforced in code**: one search request per source per run,
  detail pages fetched only for listings not already in the DB, per-source
  fetch caps, and a jittered delay between requests. When a cap trims work the
  run logs it — a silent cap reads as "that was everything".
- Scraping Zumper, Apartments.com, and Zillow is against their terms of service.
  This is personal-use, once-daily, single-query traffic, which is a mitigation
  and not an exemption.

## Upstream

- [DataSF — SF Find Neighborhoods (`gfpk-269f`)](https://data.sfgov.org/resource/gfpk-269f.geojson)
- [Camoufox](https://camoufox.com/)
- [infra/sms-relay](../../infra/sms-relay/) — the send API
