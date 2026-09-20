# Animal Crossing Community inventory

## Identity and route evidence

The exact ACC handle is `Irock`. This is connected to Zach by first-person
NSider2 posts: post `2539114` names the old ACC username, while posts `2538650`,
`546322`, `1229000`–`1229063`, and `3076463` place the account on Zach's first
forum beginning in 2004. `Parker94` is a different participant and is excluded.

Archived ACC pages correct the original route guess. The historical username
lookup submitted to `user_profile.asp?UserName=Irock`; resolved profiles and
forum bylines used `user_profile.asp?UserID=<number>`. Threads used
`thread_messages.asp?ThreadID=<number>` and later `/Topic/...` routes.

The strongest direct ACC evidence currently comes from the pattern gallery:

- a 2006-10-17 gallery snapshot is filtered to `UserLogin=Irock` and reports
  369 patterns;
- a 2022-05-05 capture of gallery page 2 reports 450 patterns;
- every recovered row has the exact `Irock` byline;
- the preserved detail page for pattern `141733`, “Giga Bowser (Anicro),” says
  `By Irock` and gives the original timestamp `7/31/2006 9:28:54 PM`.

## Recovered material

The normalized ACC database currently contains:

- 45 unique pattern records spanning 2006-07-08 through 2006-11-16;
- 50 listing observations, retaining both old and later vote/score values for
  the five patterns visible in both snapshots;
- 44 original pattern GIFs recovered from the archived image endpoint;
- compressed raw copies of both productive listing pages;
- one archived full pattern-detail page retained in the research evidence.

The recovered designs include Falco Lombardi, Fox McCloud, Dark Samus, Sonic,
Mario, Luigi, Zelda characters, Pokémon, Nintendo hardware, an Animal Crossing:
Wild World box-art design, and many others. The public catalog projects each
pattern as an attributed artifact and links its recovered GIF where available.

One known GIF (`PatternID=141733`) remains retryable after archive throttling.
The gallery totals show that at least 405 additional records existed beyond the
45 whose listing rows are currently recoverable.

## Forum-post sweep

The Wayback index contains 2,471 distinct 2004–06 `thread_messages.asp` pages
with usable thread IDs. The collector parses exact post bylines rather than
searching for the string `Irock` in message text. An exact byline match will
also resolve the account's stable numeric `UserID`.

Current replay progress:

- 36 pages fetched and parsed with no exact `Irock` byline;
- 66 requests retained as retryable fetch errors after the archive rate limit;
- no forum posts or numeric user ID claimed yet;
- 2,369 indexed pages remain unattempted.

Fetch errors are never counted as negative results. The scan is resumable and
stores a row for every attempted capture; raw HTML and complete page context
are retained for matches.

## Commands

```bash
python3 scrape_acc.py --status
python3 scrape_acc.py --retry-failures --workers 1
python3 scrape_acc.py --download-pattern-images --pattern-image-delay 8 --patterns-only
python3 build_library.py
```

The source database and media live under ignored `data/acc/`; the public
projection is rebuilt into `data/library.sqlite3`.
