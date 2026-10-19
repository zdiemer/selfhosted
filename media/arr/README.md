
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
