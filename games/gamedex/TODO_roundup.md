# Roundup — shipped 2026-07-12 (v1.11.7)

## Bugs
- [x] 1. Home search: Enter jumps to All Games with the query
- [x] 2. Command palette: tab list now read from the live header (Shelf in, Reviews out); fixed a crash typing in search on special tabs (tabState[home] undefined)
- [x] 3. 3DS uploads sliced sideways — server now honours EXIF orientation; broken uploads re-cut from stored originals on deploy
- [x] 4. YouTube bot wall — device-side, can't beat it (nocookie already tried); added a persistent "Watch on YouTube" link so a walled embed always has a click-through
- [x] 5. Home "Picked for you" — icon (i-sparkle) instead of ✨
- [x] 6. Carousel — timer resets on manual paging; visual countdown bar added; initial hero pager now wired

## Features
- [x] 7. On this day — added Purchased and Added sections
- [x] 8. Home — closest THREE challenges
- [x] 9. Main search — naive ranking, title weighted above other fields
