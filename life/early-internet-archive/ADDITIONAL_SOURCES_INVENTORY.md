# Additional-source recovery inventory

Updated 2026-09-20. This projection contains attributed material recovered by
the breadth-first sweep but not large enough to justify a source-specific
database. Raw HTTP and archive payloads remain in `data/source_sweep/raw/`;
`scrape_additional_sources.py` records their timestamps and paths in
`data/additional/additional.sqlite3` and reproducible JSONL.

| Source | Records | Preserved content |
|---|---:|---|
| Backloggd | 12 | User-list names and entry counts; detail pages are bot-check responses, so these remain metadata-only |
| Steam | 14 | Two full reviews and 12 screenshot records, with 12 original images |
| FSU coursework | 4 | Original Breakout, MyDS, Cloysta, and pybank project records |
| GOG | 3 | Complete attributed forum posts |
| Mathematics Stack Exchange | 2 | Complete questions plus attributed follow-up comments |
| ChipMusic | 1 | Complete forum post |
| DS Fanboy | 1 | Complete article comment |
| itch.io | 1 | Complete game-page comment |
| SuperCheats | 1 | Complete Pokémon Diamond Action Replay submission |
| The Spriters Resource | 1 | Submission metadata and original 1798×3680 sprite sheet |
| WikiSider | 1 | Third-party biographical context, labeled as not authored by StarFoxA |
| Personal site | 1 | Preserved 2018 portfolio text |
| **Total** | **42** | **13 locally served assets** |

Each record retains an explicit completeness value. A list title from a
profile capture is not presented as a recovered list body; third-party text is
not presented as authored material. One Steam screenshot-detail request is
currently rate-limited, but its profile metadata and original image are both
preserved.

## Reproducible workflow

```bash
python3 scrape_additional_sources.py
python3 build_library.py
```

The parser also discovers exact list, review, screenshot, and original-image
targets and adds them to the shared sweep database for later retry.
