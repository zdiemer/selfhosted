# NSider2 archive inventory

Capture completed: 2026-09-19

## Account

- Username: `StarFoxA`
- User ID: `3029`
- Profile post counter: 11,648
- Publicly retrievable unique posts archived: 10,404
- Unresolved difference from profile counter: 1,244
- Archived date range: 2007-10-11 18:06:04 UTC through 2011-10-31 04:27:35 UTC

The difference is not represented as missing post records. It may include
deleted posts, inaccessible/private forums, posts omitted from Tapatalk's
public indexes, or other historical counter behavior. No body text is invented
or reconstructed from the counter.

## Captured material

- 10,404 unique `StarFoxA` post IDs
- 3,687 distinct topics
- 10,404/10,404 posts mapped to conversation context
- 4,467 deduplicated context windows fetched
- 114,476 distinct neighboring messages retained
- 632 account-started topics enumerated
- 9,107 evidence/data files, approximately 379.4 MiB total

Every personal post record includes the complete API-exposed body, forum and
topic IDs/titles, post ID, timestamp, author identity, canonical URL, recovery
source, fetch time, and raw normalized payload. Context records preserve ordered
neighboring messages with author, timestamp, body, and topic identity. Original
XML responses are retained as gzip files.

## Retrieval passes

1. Global public author search: 10,000 posts (the API's hard result ceiling).
2. Full reads of the 632 account-started topics: 309 additional posts.
3. Author searches partitioned across 3,687 known topics: 95 additional posts.
4. Fifty-message context windows around all archived posts, deduplicated where
   one window covers several nearby `StarFoxA` posts.

## Integrity checks

- Duplicate personal post IDs: 0
- Empty personal post bodies: 0
- Wrong-author rows in personal post table: 0
- Personal posts without context mapping: 0

Twenty-two neighboring context rows have empty bodies in the public API. These
are retained rather than silently discarded because they may represent deleted,
attachment-only, or system-generated messages.

## Local outputs

- `data/nsider2/posts.sqlite3` — normalized archive and audit metadata
- `data/nsider2/posts.jsonl` — portable chronological personal-post export
- `data/nsider2/raw/` — gzip-compressed original XML-RPC responses

The `data/` directory is excluded from Git because it contains the bulk archive.
