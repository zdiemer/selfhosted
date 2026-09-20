# Early internet archive

Tools and manifests for preserving Zach Diemer's public early-internet work.
Downloaded archive data is intentionally excluded from Git under `data/`.

## Breadth-first source sweep

The cross-source sweep tracks confirmed identities and exact recovery targets
for Giant Bomb, GameSpot, Animal Crossing Community, GOG, Steam, Backloggd,
ChipMusic, Mathematics Stack Exchange, The Spriters Resource, several smaller
forums, Twitter, FSU coursework, and the preserved personal site. It inventories
live pages, Wayback, and Common Crawl
without promoting search-result snippets into the public archive as if they
were preserved originals.

```bash
python3 scrape_source_sweep.py --skip-wayback
python3 scrape_source_sweep.py --download --skip-wayback
python3 scrape_source_sweep.py --source animal_crossing_community --skip-wayback
python3 scrape_source_sweep.py --live --skip-wayback --skip-common-crawl
python3 scrape_source_sweep.py --status
```

See [`SOURCE_SWEEP_INVENTORY.md`](SOURCE_SWEEP_INVENTORY.md) for the evidence,
coverage, and next recovery action for each source.

The Backloggd collector preserves the public profile and every review-list page
as compressed raw evidence, then stores attributed review text and provenance
in SQLite and JSONL:

```bash
python3 scrape_backloggd.py
python3 scrape_backloggd.py --status
```

Current Backloggd capture: all 45 review pages and 673 full reviews, with no
page errors. Owner confirmation permits these reviews to be projected into the
public catalog. See [`BACKLOGGD_INVENTORY.md`](BACKLOGGD_INVENTORY.md) for
coverage and provenance.

The Giant Bomb collector mines frozen profile snapshots for authored artifact
URLs, retains both legacy and modern routes, and recovers reviews embedded in
the historical profile:

```bash
python3 scrape_giantbomb.py
python3 scrape_source_sweep.py --source giantbomb --skip-common-crawl
python3 scrape_source_sweep.py --download --skip-wayback --skip-common-crawl
```

Current Giant Bomb discovery: 103 attributed artifacts (15 blogs, 27 lists,
and 61 image records). Captured artifact pages currently yield eight complete
blogs, nineteen user lists, and seven forum posts, alongside ten complete
reviews from 2008–12. The remaining review excerpt and every metadata-only
profile artifact are also searchable and explicitly labeled, for 121 Giant
Bomb records in the catalog. See
[`GIANTBOMB_INVENTORY.md`](GIANTBOMB_INVENTORY.md) for coverage and recovery
details.

`scrape_additional_sources.py` promotes attributed material preserved by the
breadth-first sweep into a normalized database while retaining capture paths,
timestamps, and completeness labels:

```bash
python3 scrape_additional_sources.py
python3 build_library.py
```

The current projection adds 42 records and 13 assets from Backloggd lists,
ChipMusic, DS Fanboy, GOG, itch.io, Mathematics Stack Exchange, Steam,
SuperCheats, The Spriters Resource, WikiSider context, FSU coursework, and the
preserved personal site. See
[`ADDITIONAL_SOURCES_INVENTORY.md`](ADDITIONAL_SOURCES_INVENTORY.md).

## Official NSider

The Official Nintendo NSider collector identifies `STARFOXA` as Lithium user
ID `106819` from the archived profile, preserves the complete Wayback CDX
message inventory, and scans surviving thread pages in resumable deterministic
batches. Each fetched page is stored as compressed raw evidence before parsing;
matching posts and the surrounding page context are normalized into SQLite.

```bash
python3 scrape_official_nsider.py
python3 scrape_official_nsider.py --limit 500
python3 scrape_official_nsider.py --status
```

Nintendo's linked author-tracker page was not archived, so the default pass is
bounded to post-registration captures from Power On, the account's documented
primary board. See [`OFFICIAL_NSIDER_INVENTORY.md`](OFFICIAL_NSIDER_INVENTORY.md)
for quantified coverage, evidence, current results, and broader resume options.

## NSider2

The NSider2 collector uses the forum's read-only Tapatalk XML-RPC API. It stores
decoded posts in SQLite and keeps every raw XML response as gzip-compressed
evidence. Runs are resumable and duplicate post IDs are updated in place.

```bash
python3 scrape_nsider2.py
python3 scrape_nsider2.py --status
python3 scrape_nsider2.py --export-jsonl data/nsider2/posts.jsonl
```

The normal run performs two passes:

1. page through the public author search for user ID `3029` (`StarFoxA`);
2. enumerate topics started by the account and re-read their threads;
3. partition the author search by each known thread, recovering posts hidden
   behind Tapatalk's global 10,000-result search ceiling;
4. capture a deduplicated 50-message conversation window around every archived
   `StarFoxA` post. Context by other authors is kept in separate tables and is
   not included in the personal post count.

The profile post counter is not treated as a guarantee that every historical
post remains publicly retrievable. The database records the profile count, API
search count, unique archived count, and recovery sources separately.

Current public-data capture (2026-09-19): 10,404 unique `StarFoxA` posts across
3,687 topics, with context mappings for every post. See
[`NSIDER2_INVENTORY.md`](NSIDER2_INVENTORY.md) for the audit summary.

The PhotoBucket pass inventories URLs embedded in NSider2, checks Wayback
captures, directly searches Common Crawl's public index shards through 2016,
and verifies downloaded image payloads. See
[`PHOTOBUCKET_INVENTORY.md`](PHOTOBUCKET_INVENTORY.md).

## IndieNerds

The IndieNerds collector inventories the archived WordPress site, identifies
author ID `30` through the preserved `Zach Diemer` / `StarFoxA` bylines, and
stores article HTML, normalized text, raw evidence, provenance, and recoverable
uploads. It distinguishes full article captures from listing-page excerpts.

```bash
python3 scrape_indienerds.py
python3 scrape_indienerds.py --skip-assets
python3 scrape_indienerds.py --status
```

Current public-data capture (2026-09-19): 12 attributed entries, including nine
complete articles and three archived excerpts, plus six recovered original
uploads. See [`INDIENERDS_INVENTORY.md`](INDIENERDS_INVENTORY.md).

## Archive browser

`build_library.py` turns the source-specific preservation databases into a
disposable, read-only SQLite/FTS catalog. The FastAPI/Jinja application under
`app/` searches that projection, displays retained conversation context, and
serves recovered media through stable asset IDs. It never migrates or writes
the preservation databases.

```bash
python3 build_library.py
python3 -m unittest discover -s tests -v
```

The Helm chart runs two rootless readers behind Authelia. The complete ignored
`data/` tree is stored on a dynamically provisioned `truenas-nfs` PVC; each pod
copies only `library.sqlite3` to local scratch before opening it. See
[`DEPLOYMENT.md`](DEPLOYMENT.md) for the PVC sync, image build, and deployment
workflow.
