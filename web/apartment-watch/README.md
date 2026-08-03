# apartment-watch — daily SF rental scraper → SMS

A CronJob that scrapes SF studio/1br listings once a day, filters them against
`criteria.yaml`, and texts a digest of anything new that matches. No frontend,
no API, no Service, no Ingress — nothing ever connects *to* it.

```
09:00 PT  CronJob
             │
             ├─ craigslist ──── plain httpx (no browser needed)
             ├─ zumper ───────┐
             ├─ apartments.com├─ Camoufox (hardened Firefox, Xvfb)
             └─ zillow ───────┘
             │
             ▼
      evaluate vs criteria.yaml ──▶ SQLite on a PVC (dedup + price history)
             │
             ▼  only listings that newly match
      POST /api/v1/messages ──▶ sms-relay.infra ──▶ Android handset ──▶ you
```

## Why it's in this repo

The root README's convention is that apps we write live in their own repo and
come back as a submodule. This one deliberately doesn't — it's small, it has one
consumer, and the whole thing is a CronJob plus four parsers. The in-repo
precedent is [`minecraft/claude-bridge/`](../../minecraft/claude-bridge/):
chart, `Dockerfile`, and `src/` all live here, and the image still ships to
`ghcr.io/zdiemer/apartment-watch`.

## Silence is a feature, and that's a risk

A run with no matches sends **nothing**. That's the point — the tool is only
worth having if a text means something. But it makes one failure mode dangerous:
a parser that silently breaks looks exactly like a quiet market, forever.

So `source_health` tracks consecutive empty runs per source, and if a source
goes quiet for `health_alert_after_stale_runs` runs (default 3) or throws, the
next zero-match run breaks silence with a short health text — rate-limited to
once every three days so a permanently dead source can't train you to ignore it.

## What actually works, and what doesn't

Verified against the live sites in August 2026, from a residential IP:

| Source | Wall | Status |
|---|---|---|
| **Craigslist** | none | Serves a no-JS result list that honours the query string. No browser at all. Detail pages carry structured `laundry=`/`parking=` codes and lat/lon. **The load-bearing source.** |
| **Zumper** | F5 "Client Challenge" | Camoufox clears it. Ships laundry, bedrooms, and address inline as schema.org JSON-LD — zero detail fetches. |
| **Apartments.com** | Akamai Bot Manager | Camoufox clears it **only with a warm-up**: a cold request gets a 2.5KB challenge shell every time, but loading the homepage, waiting ~12s, then navigating in-site returns the real 800KB page. See `Fetcher.warm_up`. |
| **Zillow** | PerimeterX | Search page: cleared. **Detail pages: still 403**, and laundry only exists there. Combined with a list that skews to $3,500+ new-builds, a live run produced 41 cards and 0 matches. Left enabled because it costs ~20s and no successful detail fetches, but expect nothing from it. |
| **HotPads** | PerimeterX | **Blocked** even from a warmed browser session. Not implemented: it's Zillow-owned, so its inventory is already covered. |
| **PadMapper** | F5 | Renders, but its listings come from a map XHR with nothing parseable in the HTML. Not implemented: it's Zumper's own site with Zumper's own inventory. |

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

## The rules

`criteria.yaml` is the entire tuning surface, and it's a **ConfigMap** — editing
it and running `./upgrade.sh` needs no rebuild.

- **$3000/mo**, compared against *effective* rent: base plus any stated monthly
  parking fee.
- **Parking** is desired, never required. It doesn't gate a match; matches that
  have it get flagged in the text. A stated fee counts against the budget; an
  unstated one is assumed included (configurable).
- **In-building laundry** is a hard requirement. `unknown` does *not* pass — a
  listing that never mentions laundry usually doesn't have it.
- **Neighborhoods** are excluded by point-in-polygon on the listing's lat/lon
  against `data/sf-neighborhoods.geojson` (DataSF "SF Find Neighborhoods",
  `gfpk-269f`, 117 polygons). Coordinates decide. A poster-supplied neighborhood
  string is consulted *only* when there's no lat/lon, because posters mislabel
  constantly — "Potrero Hill" reads better than "Bayview".
- **Studio or 1br.**
- **Scam filter** (`src/scam.py`): scored, not a keyword blacklist. Payment-rail
  tells (wire, gift cards, crypto), absentee-landlord narratives, screening
  waivers, off-platform contact pushes, and rent below a plausible floor each
  add points; several have to agree before a listing is dropped. Single weak
  signals can't kill a real bargain, which matters when finding bargains is the
  whole job. Rejections record which rules fired.

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

Look for per-source listing counts, `MATCH` lines, any detail-fetch cap warning,
and then either `queued sms <uuid>` or `no new matches — staying quiet`.

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
