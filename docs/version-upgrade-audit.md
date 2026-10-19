# Version upgrade audit — 2026-10-19

This audit compares deployed images, direct host checks, upstream releases and
GitHub dependency dashboard issue #4. No runtime versions were changed during
this audit. K3s remains at the freshly upgraded 1.35.9 while it is observed.

## Coverage findings

Cloudflared and RomM are already detected by Renovate. Their updates are queued
under Rate-Limited, with the shared preset allowing five concurrent PRs. The
workspace Tailscale binary is also covered in the open tools PR #26. Changing
PR limits would increase the queue presented for review, not deploy anything.

The arr chart uses scalar `image` plus sibling `tag`, which helm-values did not
extract. Only Sonarr was visible through Chart.yaml's appVersion annotation.
The new regex manager detects all eight arr image/tag pairs, including Radarr
and Recyclarr's intervening comments. A second manager detects vmlab's annotated
standalone QEMU tag. Its two architecture images intentionally share one tag.
Existing arr grouping and major-version approval rules remain in force.

Validated with the deployed Renovate 44.30.3 config validator, plus extraction
checks for all eight arr pins, vmlab and nonmatching repository/tag objects and
combined image:tag strings. The next Renovate run must perform registry lookups;
source releases below are candidates, not proof that every short container tag
exists. No full Renovate run or additional PR creation was triggered.

## Candidates and rollout order

| Component | Deployed | Candidate | Handling |
| --- | --- | --- | --- |
| Host Tailscale | 1.102.2–1.102.4 | 1.104.1 | Outside Renovate; one worker canary, then one host at a time |
| Cloudflared (infra and kelsey-green) | 2026.8.2 | 2026.10.1 | Already queued; roll one connector release at a time, verify tunnels and public HTTP |
| Prowlarr | 2.5.2 | upstream 2.6.5.5623 | Newly tracked; verify LinuxServer tag and back up config DB |
| Sonarr | 4.0.19 | upstream 4.0.20.3014 | Previously appVersion-only; now image pin also tracked |
| Radarr | 6.3.0 | upstream 6.4.4.10685 | Newly tracked; review trusted-network/allowed-host changes |
| FlareSolverr | v3.5.0 | v3.5.2 | Newly tracked; retain OOM mitigations, verify browser requests and memory |
| Recyclarr | 8.7.1 | 8.8.0 | Newly tracked; review config/profile sync before running |
| Gluetun | v3.41.3 | v3.41.3 | Current stable release; no bump needed |
| RomM | 5.3.0 | 5.4.0 | Already queued; separate migration window |
| RomM MariaDB | 11.4.12 | 11.4.13 (dashboard) | Already queued; remain on 11.4 LTS |

qBittorrent and Decluttarr are now tracked too. Their target image tags were not
independently verified in this audit. Other queued infrastructure includes
registry 3.1.2, KubeVirt/CDI, CrowdSec, Alloy, CSI snapshot-controller and node
exporter; review compatibility rather than treating them as one bulk upgrade.
KubeVirt needs special attention to the handler certificate workaround added
in this session when selecting its next version.

## Host Tailscale

Direct checks found 1.102.2 on node0, node1, node2 and all three laptops;
1.102.3 on node3, node4 and node5; 1.102.4 on node6. Node5's apt candidate is
1.104.1. The release includes Linux connectivity and memory improvements.

Use a privileged pod/host-owned unit to upgrade only the tailscale package so
its service restart cannot terminate its own updater. Preserve existing state,
watchdog configuration and identity; verify direct root SSH, Running/online
status and node Ready before advancing. No OS/kernel updates or reboots need
to accompany it. Do not use the general apt-upgrade script for this narrow task.

`scripts/k3s/versions-report.sh` already supports a read-only `--dry-run` report.
The `infra/versions-report` ConfigMap was absent, and node0 had no versions-report
timer. Check the intended scheduler/owner before enabling publication: the
normal script also posts a GitHub issue comment. This audit did not publish
messages or install a timer.

## RomM migration prerequisites

The 5.4.0 notes warn that first-start migrations rewrite/backfill the ROM table
and can take 15–20 minutes. Take and verify a fresh MariaDB dump and relevant
PVC backup first. The current startup probe allows 30 minutes, but the deployment
script's rollout timeout must also accommodate the migration. Do not restart a
healthy migration because a short rollout wait expires. A database migration
rollback requires restoring the matching data, not just selecting the old image.

Review the changed CORS default (empty now denies cross-origin clients) and
proxy trust settings against actual Webstation/sync clients and Traefik routing.
Verify library counts, authentication, artwork and save/sync operations after
migration. Keep the database patch separate from the application migration.

## Sources

- [Dependency dashboard](https://github.com/zdiemer/selfhosted/issues/4)
- [Tailscale changelog](https://tailscale.com/changelog#2026-10-07-client)
- [Cloudflared 2026.10.1](https://github.com/cloudflare/cloudflared/releases/tag/2026.10.1)
- [RomM 5.4.0](https://github.com/rommapp/romm/releases/tag/5.4.0)
- [Sonarr releases](https://github.com/Sonarr/Sonarr/releases)
- [Radarr releases](https://github.com/Radarr/Radarr/releases)
- [Prowlarr releases](https://github.com/Prowlarr/Prowlarr/releases)
- [FlareSolverr v3.5.2](https://github.com/FlareSolverr/FlareSolverr/releases/tag/v3.5.2)
- [Recyclarr v8.8.0](https://github.com/recyclarr/recyclarr/releases/tag/v8.8.0)
