# Backloggd recovery inventory

Updated 2026-09-19. The archive owner explicitly confirmed that the public
`starfoxa` identity may be attributed to them. The software-engineer bio,
Chrono Trigger favorite, and description of a high-school-era game spreadsheet
also independently align with the confirmed Giant Bomb history.

## Preserved coverage

| Artifact | Recovered |
|---|---:|
| Profile pages | 1 |
| Review-list pages | 45 of 45 |
| Full reviews | 673 |
| Page fetch errors | 0 |
| Earliest review | 2021-02-05 |
| Latest review | 2026-07-10 |
| Games shown as played at capture | 1,603 |

Every fetched response is stored gzip-compressed under
`data/backloggd/raw/` with its SHA-256 digest and fetch time in
`data/backloggd/backloggd.sqlite3`. Parsed reviews retain the title, game URL,
release year, rating, play status, platform, publication time, body, canonical
review URL, and source page. `reviews.jsonl` is a disposable export of the same
records.

The profile says game tracking and ratings began in high school, completion
dates were recorded from mid-2013 onward, about 600 entries lack dates, and
roughly 30 games were absent from IGDB during spreadsheet import. Those details
make the account a strong continuity match. Owner confirmation clears the
recovered reviews for public catalog import.

## Reproduce

```bash
python3 scrape_backloggd.py
python3 scrape_backloggd.py --status
```

The collector is resumable at the page level. Re-running it refreshes the
profile and only fetches review pages that do not already have a successful
record.
