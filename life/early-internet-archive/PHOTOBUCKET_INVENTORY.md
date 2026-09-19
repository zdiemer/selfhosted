# PhotoBucket archive inventory

Capture completed: 2026-09-19

## Account namespace

- Primary album: `i123.photobucket.com/albums/o307/StarFoxA/`
- Alternate host checked: `s123.photobucket.com/albums/o307/StarFoxA/`
- Later `/user/StarFoxA/` URL schemes checked on three host variants

## Evidence inventory

- 199 distinct album file URLs recovered from the NSider2 post/context archive
- 605 total mentions of those files
- 39 successful Wayback capture rows
- 16 successful Common Crawl capture rows
- 38 distinct captured URLs
- 35 distinct archive digests, including one PhotoBucket HTML placeholder digest
- 34 verified image payloads recovered
- 183 referenced filenames with no surviving capture in the checked archives

The 34 recovered files contain 21 PNGs, 9 GIFs, and 4 JPEGs. Their combined
payload size is 1,918,933 bytes. Dimensions range from small avatars and rank
icons to a 500×375 photograph and 552×184 signature artwork.

## Coverage

Wayback was queried both by album prefix and individually for all 199 referenced
file URLs. All exact checks completed without outstanding request errors.

Common Crawl's public compressed index shards were searched directly for both
the `i123` and `s123` album prefixes in all 32 crawl collections from 2008
through 2016. Only `CC-MAIN-2012` contained matches: 16 image captures. Three of
those (`gw45.png`, `ninjagaiden.png`, and `starspot1.png`) were not present in
the NSider2-derived URL list.

The 183 unresolved filenames remain valuable inventory evidence: they were
extracted from contemporaneous posts even though neither archive retained their
payload. They are preserved in `manifest.json` with mention counts and lookup
status rather than discarded.

## Integrity

- Every downloaded payload passed image magic-byte validation.
- Every retained asset has a SHA-256 checksum.
- Duplicate captures are consolidated by archive digest without losing their
  original URL, timestamp, collection, or WARC/ARC provenance.
- PhotoBucket HTML replacement pages are not saved as images.

## Local outputs

- `data/photobucket/photobucket.sqlite3` — captures, URL evidence, and checks
- `data/photobucket/manifest.json` — portable inventory and provenance
- `data/photobucket/media/` — 34 recovered image payloads
- `data/photobucket/raw/` — raw archive index responses and collection catalog

The bulk `data/` directory is excluded from Git.
