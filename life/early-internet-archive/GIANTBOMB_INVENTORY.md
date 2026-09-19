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

Four full reviews survive inside the historical profile snapshots and are now
projected into the public catalog:

| Date | Game | Rating | Body characters |
|---|---|---:|---:|
| 2008-07-27 | *Professor Layton and the Curious Village* | 9/10 | 4,037 |
| 2008-07-27 | *Contra 4* | 8/10 | 3,905 |
| 2008-07-27 | *Super Mario Galaxy* | 9/10 | 6,713 |
| 2008-10-04 | *Chrono Trigger* | 10/10 | 6,327 |

The profile reports 12 reviews in total, so eight remain unresolved. The known
2012 *Eternal Darkness: Sanity's Requiem* review is separately seeded and is a
high-priority recovery target.

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

The collector reads gzip-compressed profile evidence beneath
`data/source_sweep/raw/wayback/giantbomb-profile/`, writes normalized artifact
and review records to `data/giantbomb/giantbomb.sqlite3`, and adds discovered
exact routes to the source-sweep database. Re-running it is idempotent.
