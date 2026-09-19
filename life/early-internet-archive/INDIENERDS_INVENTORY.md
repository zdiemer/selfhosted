# IndieNerds inventory

Capture date: 2026-09-19

## Identity and source

The archived WordPress site identifies author ID `30` as both `Zach Diemer` and
`StarFoxA`. Its older theme also assigns those entries the CSS author slug
`author-starfoxa`. The collector requires this archived byline evidence; it does
not classify posts from title or subject matter alone.

Primary namespace:

- `http://indienerds.com/wordpress/`
- author page: `?author=30`

## Preserved corpus

The local library contains 12 attributed entries spanning 2010-10-17 through
2011-02-15:

| Post | Date | Title | Preservation |
|---:|---|---|---|
| 1392 | 2010-10-17 | Hands On Hypership Out of Control | excerpt |
| 1427 | 2010-11-11 | Hands On: Explosionade | full |
| 1475 | 2010-11-19 | Hands On: U Want Cookie? | excerpt |
| 1494 | 2010-11-29 | Hands On: The Deep Cave | excerpt |
| 1520 | 2010-12-20 | Interview with Mike Muir of Eyehook Games | full |
| 1522 | 2010-12-04 | Hands On: Break Limit | full |
| 1531 | 2010-12-07 | Hands On: Aesop’s Garden | full |
| 1583 | 2010-12-21 | Hands On: Chu’s Dynasty | full |
| 1663 | 2010-12-25 | Hands On: Football Games Room | full |
| 1736 | 2011-01-15 | Review: Osr unhinged | full |
| 1740 | 2011-02-15 | Review: Digital: A Love Story | full |
| 1774 | 2011-02-14 | Onslaught! Arena free for today only | full |

The two February entries appear in post-ID order rather than chronological
order on the preserved site; the dates above reproduce the archived bylines.

Nine entries have complete individual article captures. Three survive only as
the content shown in archived monthly listing pages. Those records are retained
with `completeness = "excerpt"`; the collector does not infer or reconstruct
their missing prose.

The normalized corpus contains 5,640 words. Thirty upload URLs are referenced
by the preserved HTML. Six original media files (768,125 bytes total) were
recovered and checksum-verified. Twenty-four references currently have no
recoverable full-size capture in the site's Wayback media index. Missing-media
rows remain in SQLite with their URL and retrieval error.

## Discovery coverage

- 451 unique successful archived HTML URLs inventoried from Wayback CDX.
- 134 individual WordPress article URLs downloaded and examined.
- Archived author page, home page, monthly pages, and pagination pages examined
  for attributed entries omitted from the individual-page URL inventory.
- 102 unique successful upload URLs inventoried from the site's archived media
  namespace.
- Public Common Crawl index checks for the three excerpt-only post URLs did not
  return an additional complete capture.

The archived author page exposes the ten most recent entries. Archived monthly
pages add post 1392 and confirm the three excerpt-only records. No earlier
`author-starfoxa` entry appears in the captured June–October monthly and
pagination pages.

## Local files

- `data/indienerds/indienerds.sqlite3` — queryable source of truth.
- `data/indienerds/articles.jsonl` — portable article export.
- `data/indienerds/raw/pages/` — gzip-compressed individual-page evidence.
- `data/indienerds/raw/listings/` — gzip-compressed listing-page evidence.
- `data/indienerds/raw/author/` — gzip-compressed author-index evidence.
- `data/indienerds/raw/cdx-html.json` and `cdx-media.json` — discovery evidence.
- `data/indienerds/media/` — recovered original uploads.

Raw archive data remains excluded from Git by the project-level `.gitignore`.

## Reproduce or inspect

```bash
python3 scrape_indienerds.py
python3 scrape_indienerds.py --skip-assets
python3 scrape_indienerds.py --status
```

Runs are resumable. Cached CDX responses and compressed pages are reused;
article and asset rows are updated in place. `--skip-assets` refreshes discovery,
normalization, and exports without retrying missing media.

## Known limits

- Posts 1392, 1475, and 1494 are partial because no individual article capture
  was found. Their listing-page excerpts and source snapshots are preserved.
- A missing Wayback or Common Crawl result means no capture was found in the
  checked public indexes, not that no copy exists anywhere.
- Comments are present in raw page evidence where captured, but are not folded
  into the author's normalized article text.
