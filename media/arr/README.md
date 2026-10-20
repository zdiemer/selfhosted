
### Preserve FlareSolverr's resource budget during upgrades

FlareSolverr requires the tracked 1Gi memory request / 4Gi limit for the four
Waitress request threads shared by Prowlarr and Talaria. The October 19 version
upgrade retained stale Helm values (512Mi request / 2Gi limit), undoing the live
memory fix while leaving the correct defaults in `values.yaml`. Four concurrent
challenge requests then OOM-killed the browser container.

Use this directory's `upgrade.sh`, which reapplies the tracked values, rather
than a tag-only `helm upgrade --reuse-values`. If deliberately reusing values,
explicitly carry forward any resource-policy changes too. Verify the deployed
container resources after an upgrade; the repository defaults alone do not
prove the live budget. Restoring 1Gi/4Gi passed four simultaneous test solves
with HTTP 200, roughly 1.6Gi sampled peak memory, and zero OOM events. A simple
page test does not establish the worst-case memory cost of real challenges.

## Executable and scam-download protection

`harden-downloads.py --apply` reconciles persistent application settings through
local APIs; `upgrade.sh` runs it after the deployments are ready. Run it without
`--apply` to check for drift. Credentials are read from each application's config
in memory and sent through stdin, never logged or placed in command arguments.

- Sonarr/Radarr: enable **Fail Downloads → Executables + Potentially Dangerous**
  on every indexer and reject blocklisted torrent hashes before sending grabs to
  the downloader. Existing failed-download removal/redownload settings remain in
  place. Prowlarr's application sync also enables hash rejection; its sync retains
  the per-indexer fail-download settings.
- qBittorrent: enable case-insensitive exclusions for executable, installer,
  script and shortcut suffixes, including `.exe`, `.scr`, `.msi`, `.bat`, `.cmd`,
  `.ps1`, `.vbs`, `.js`, `.lnk`, `.url`, and `.sh`. Existing exclusions are retained.
  Normal video, subtitle and archive formats are not blanket-blocked.

This is file-type filtering, not a malware scanner. Exclusions apply to newly
added torrents and do not remove already downloaded files. Re-run the script
when adding indexers so new entries receive the same rejection policy.

### FlareSolverr overload handling

The runner in `files/flaresolverr-runner.py` preserves upstream startup and
logging while limiting `/v1` requests to `flaresolverr.maxBrowserRequests`
(default 4). Waitress has four additional HTTP threads so `/health` does not
queue behind long browser challenges. Overload returns HTTP 503 with
`Retry-After: 5` and a FlareSolverr error body. Clients should retry later;
this does not make unsupported Cloudflare challenges solvable.

The wrapper is mounted from a checksummed ConfigMap. Both probes use the
quiet `/health` endpoint. Regression tests cover saturation, health availability,
exceptions and abandoned responses:

```sh
python3 -m unittest discover -s media/arr/tests -v
```

## Indexer and import repair (2026-10-19)

These settings live in the applications' persistent databases, not Helm values:

- AnimeTosho's generic Torznab preset used `https://feed.animetosho.org`, which
  no longer resolved. Set its base URL to `https://feed.animetosho.net`; keep
  `/api` as the API path. Prowlarr and Sonarr tests passed with that address.
- Internet Archive was initially working through the `pia` proxy, but later
  failed repeated Prowlarr tests through both direct and proxied routes. Raw
  archive.org API requests still work; the .NET indexer requests time out.
  Disabling HTTP/2 did not resolve it and was reverted. The indexer is now
  disabled, with its configuration retained, and that state is synced to
  Sonarr/Radarr. Re-enable only after an actual Prowlarr test succeeds.
- nekoBT's API returned HTTP 500, including through PIA. Disabled it while
  retaining the configuration for a later recovery test.
- Removed the already-disabled Zamunda RIP, GamesTorrents, and Torrent Oyun
  indir records: their definitions no longer exist in Prowlarr.
- KickAss passed a fresh test. Torrent Downloads passed its basic test but its
  actual TV query hit Cloudflare (HTTP 403 through PIA too), and FlareSolverr
  timed out. Disabled it with its original configuration retained. Saving a
  validated indexer clears its stale failure backoff; running a test alone can
  leave it unchanged.
- Sonarr's stuck executable release was removed from the downloader and
  blocklisted. qBittorrent had excluded its sole file (priority zero, zero bytes
  downloaded). The lower-quality duplicate was ignored in Sonarr, retaining
  its downloaded files and the existing 1080p library episode. No replacement
  download was triggered by this cleanup.

After changing Prowlarr, sync its applications and check `/api/v1/health` plus
Sonarr/Radarr `/api/v3/health`. Validate searches through the downstream app too:
a responding indexer homepage does not establish that its search API works.
Run `harden-downloads.py` afterward to verify executable rejection remains set.

### Unreliable RSS sources (2026-10-20)

TorrentByte and kickasstorrents.ws repeatedly timed out solving Cloudflare
challenges during scheduled RSS polling, causing downstream timeout and backoff
warnings. Tokyo Toshokan repeatedly returned a feed that Sonarr reported could
not cover its previous polling window. Fresh connection tests passed for all
three, so their indexers remain enabled.

Prowlarr's **Search only (unreliable RSS)** application profile disables RSS
for these three sources while retaining automatic and interactive searches and
the Standard profile's minimum of three seeders. Full application sync carries
this setting to Sonarr/Radarr. Other sources continue supplying scheduled RSS;
releases unique to these three now require a search. Searches can still encounter
upstream challenge failures. Restore the Standard profile after sustained
successful feed queries, not merely a successful connection test.

The hardening script explicitly selects each application container; the
health-exporter sidecar also mounts the config but does not contain `curl`.

Application sync is scoped by `sync-sonarr` and `sync-radarr` tags. Existing
indexers retain both memberships except Torrentsome/Torrenttip (Radarr only)
and LimeTorrents (Sonarr only): their other app's category validation repeatedly
returned no results and retried failed indexer creation on every sync. Existing
proxy tags are retained. **Give new indexers the appropriate sync tags** or they
will remain available only in Prowlarr. Restore an excluded membership after
its movie/TV category query returns usable results.
