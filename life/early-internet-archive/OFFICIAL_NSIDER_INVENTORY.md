# Official NSider inventory

Capture date: 2026-09-19

## Identity

The archived Nintendo NSider profile provides a direct identity match:

- username: `STARFOXA`
- Lithium user ID: `106819`
- registration: 2005-06-05 5:05 PM
- last visit shown by the capture: 2007-08-21 7:17 PM
- profile post counter: 10,138
- rank: Mr. Saturn
- profile capture: `20070822021707`

The raw profile has SHA-256
`8c0d068287ae1b6924c4897624e2f3b9552951698832715c84fbd7dc0d7003f3`.
This agrees with independent first-party statements preserved in the NSider2
corpus: post `1810649` names the Official NSider account `STARFOXA`, while
posts `2546100`, `1414438`, and `1372778` place registration on June 5, 2005
after an earlier period of lurking.

Primary namespace:

- `http://forums.nintendo.com/nintendo/`
- profile: `view_profile?user.id=106819`
- author tracker: `tracker?user.id=106819`

## Public archive coverage

The Wayback CDX inventory contains 153,680 unique successful HTML message-page
URLs for Official NSider. The largest relevant subsets are:

| Board ID | Board | Unique pages | Captured on/after registration |
|---|---|---:|---:|
| `np_po` | Power On | 10,336 | 5,297 |
| `Nowplaying` | Reviews | 4,233 | 3,039 |
| `poweron_rp` | Power On (RP) | 4,405 | 1,053 |
| `starfox` | Star Fox | 1,580 | 1,413 |

Wayback also exposes 8,422 unique archived profile URLs and 5,935 unique
archived author-tracker URLs. The `STARFOXA` profile survives, but its linked
author tracker does not: no successful CDX row exists for user ID `106819`, and
a direct replay resolves to Wayback's no-capture response. Lithium message URLs
contain board and message IDs but no author name or user ID, so recovery
requires scanning page bodies.

Common Crawl's public corpus begins with the 2008–2009 crawl, after Nintendo
closed NSider in September 2007. Its index service was unreachable from the
collector host during this sweep, so no Common Crawl absence claim is made and
the evidence-led recovery path remains Wayback. A future pass can still query
the two legacy ARC collections for late copies or shutdown pages.

## Initial preservation pass

The collector first preserved the definitive profile and complete CDX message
inventory. It then sampled archived pages from the account's contemporary
boards, retaining every fetched page as gzip-compressed raw evidence before
parsing it. Exact run counts are queryable with:

```bash
python3 scrape_official_nsider.py --status
```

The bounded initial pass attempted 220 archived page captures. It successfully
preserved and parsed 216 raw pages containing 1,939 forum posts:

| Board | Parsed pages | Parsed posts |
|---|---:|---:|
| Power On (`np_po`) | 147 | 1,298 |
| Reviews (`Nowplaying`) | 33 | 281 |
| Power On RP (`poweron_rp`) | 26 | 260 |
| Star Fox (`starfox`) | 10 | 100 |

Four requests failed (one Wayback 404 and three transient connection refusals)
and remain explicitly recorded for `--retry-failures`. The 1,939 parsed posts
contained no post attributed to user ID `106819`, so the portable post export
is presently empty. This is not evidence that the posts are absent from
Wayback: it only means the deterministic sample did not contain the target ID.
Failed fetches and negative matches remain in SQLite, so future runs advance
to unseen URLs rather than starting over.

## Local files

- `data/official_nsider/official_nsider.sqlite3` — profiles, page attempts,
  recovered posts, and surrounding context.
- `data/official_nsider/posts.jsonl` — portable export of recovered posts.
- `data/official_nsider/raw/profile/` — gzip-compressed profile evidence.
- `data/official_nsider/raw/pages/` — gzip-compressed scanned thread pages.
- `data/official_nsider/raw/cdx-messages.json` — complete message discovery
  inventory as returned by Wayback CDX.

Raw archive data remains excluded from Git by the project-level `.gitignore`.

## Resume

The default run scans the next 100 post-registration Power On captures. It is
safe to interrupt and rerun.

```bash
python3 scrape_official_nsider.py
python3 scrape_official_nsider.py --limit 500
python3 scrape_official_nsider.py --boards np_po,Nowplaying,poweron_rp,starfox
python3 scrape_official_nsider.py --retry-failures --limit 100
python3 scrape_official_nsider.py --status
```

`--limit 0` removes the page-attempt ceiling. That is intentionally not the
default: scanning the full public inventory is a large, rate-sensitive Wayback
workload. A globally unavailable Internet Archive response stops the batch
without marking an individual capture missing.

## Known limits

- The profile's 10,138 counter is historical metadata, not a claim that the
  same number of message bodies remains publicly recoverable.
- CDX inventories URLs and captures, not page text. With no author-tracker
  capture, attribution can only be established after downloading HTML.
- One archived thread page normally contains at most ten replies. A negative
  page match says nothing about other pages in that thread.
- Capture timestamps identify Wayback retrieval time. Post timestamps come
  from the preserved Lithium page and have no explicit timezone.
- A missing Wayback result does not prove that no private or independent copy
  survives elsewhere.
