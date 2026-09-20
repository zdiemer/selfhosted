# Giant Bomb recovery inventory

Updated 2026-09-19. `StarFoxA` is confirmed by the archive owner and by
first-person NSider2 links to the Giant Bomb profile. This inventory counts
only artifacts exposed by frozen originals; search-result snippets are leads,
not preserved records.

## Current recovery

The breadth sweep recovered 148 digest-distinct historical profile snapshots.
`scrape_giantbomb.py` parses those payloads into a dedicated preservation
database and exports reproducible JSONL. The profile snapshots expose 103
unique attributed artifacts:

| Kind | Discovered | Exact URLs seeded |
|---|---:|---:|
| Blogs | 15 | 15 |
| Lists | 27 | 57 canonical and observed routes |
| Image records | 61 | 61 |
| **Total artifacts** | **103** | **133 routes** |

The route count exceeds the artifact count because Giant Bomb migrated lists
from `/profile/starfoxa/{slug}/46-{id}/` to
`/profile/starfoxa/lists/{slug}/{id}/`. Both observed routes are retained and
queried. This recovered captures for deleted lists that returned zero results
under the modern route alone, including *Wish List*, *Pages I've Done a Lot of
Work On*, *Games I Plan on S-Ranking*, *Disappointing and Frustratingly
Mediocre Games*, *Pages to rename/add aliases*, *Non-Indexed Pages*, and *My
Collection*.

## Complete reviews

Four full reviews survive inside historical profile snapshots. A separate
15-snapshot review-index recovery exposed additional exact URLs, and dedicated
review-page captures raised the catalog to ten complete reviews:

| Date | Game | Rating | Body characters |
|---|---|---:|---:|
| 2008-07-27 | *Professor Layton and the Curious Village* | 9/10 | 4,037 |
| 2008-07-27 | *Contra 4* | 8/10 | 3,905 |
| 2008-07-27 | *Super Mario Galaxy* | 9/10 | 6,713 |
| 2008-10-04 | *Chrono Trigger* | 10/10 | 6,327 |
| 2009-03-08 | *The World Ends With You* | 10/10 | 4,292 |
| 2009-04-21 | *Burnout Paradise* | 9/10 | 6,670 |
| 2009-08-13 | *Demons of the Badlands* | 8/10 | 2,370 |
| 2010-04-19 | *Arkedo Series – 03 PIXEL!* | 9/10 | 3,592 |
| 2010-09-13 | *Castlevania: Harmony of Despair* | 7/10 | 5,899 |
| 2012-10-05 | *Eternal Darkness: Sanity's Requiem* | 6/10 | 4,630 |

The index also identifies *Fluid* (2010, 10/10) and preserves its excerpt, but
no complete review-page capture has been found. The profile reports 12 reviews
while the surviving indexes expose only eleven distinct titles, leaving one
deleted title unidentified. Neither is promoted as a complete review.

## Blogs, lists, and forum posts

Exact artifact-page recovery currently yields 34 additional authored catalog
records:

| Kind | Complete records | Detail |
|---|---:|---|
| Blogs | 8 | Full bodies dated 2008–13 |
| User lists | 19 | 1,918 distinct list entries retained in searchable bodies and structured metadata |
| Forum posts | 7 | Attributed messages from five seeded discussion threads |

The blogs include *Well that was weird*, *What I've been playing...*, *That's
a tough one...*, *New Review*, *100,000 wiki points, oh my!*, both recovered
*Indie Reviews* posts, and *Bundle Backlog – Solar 2 and Dynamite Jack*. The
list parser covers all three historical Giant Bomb layouts and merges legacy,
modern, and paginated captures by list ID. Five newly recovered pagination
captures expand *Humble Obsession Bundle* to 395 retained entries and preserve
the second page of *Xbox Live Indie Collection*.

All 103 profile artifacts now appear in the archive browser. Complete captures
retain their recovered bodies; the other 76 entries are visibly marked as
metadata-only rather than silently omitted. The *Fluid* profile excerpt is
likewise searchable and marked as an excerpt, bringing Giant Bomb to 121
public catalog records.

These counts distinguish discovered profile links from captured bodies: seven
of the fifteen discovered blog pages and eight of the twenty-seven discovered
list pages still lack a recoverable page capture. Their exact URLs remain
seeded for future archive passes.

## Notable authored inventory

The recovered URLs include early blogs such as *Hey, everyone*, *Japanese
Games*, *Giant Bomb site redesign and old PC games*, and *100,000 wiki points,
oh my!*; later indie-review and Bundle Backlog posts; and long-running lists
such as *Every Game I've Ever Finished*, *Humble Obsession Bundle*, and
*Bundle Backlog*. The 61 image records include game covers, screenshots, and
wiki-contribution uploads visible on the historical profile.

Artifact `snapshot_count` records how many preserved profile snapshots exposed
that URL. It does not claim that the artifact page itself was captured. Exact
artifact-page captures and their raw payload paths remain separately recorded
in `data/source_sweep/source_sweep.sqlite3`.

## Reproducible workflow

```bash
python3 scrape_giantbomb.py
python3 scrape_source_sweep.py --source giantbomb --skip-common-crawl
python3 scrape_source_sweep.py --download --skip-wayback --skip-common-crawl
python3 build_library.py
```

The collector reads gzip-compressed profile and artifact evidence beneath
`data/source_sweep/raw/wayback/`, writes normalized artifact, review, blog,
list, and forum-post records to `data/giantbomb/giantbomb.sqlite3`, and adds
discovered exact and pagination routes to the source-sweep database.
Re-running it is idempotent.
