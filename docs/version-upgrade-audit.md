# Version upgrade audit — 2026-10-19

This audit compares deployed images, direct host checks, upstream releases and
GitHub dependency dashboard issue #4. The initial audit made no runtime changes;
the subsequent authorized rollout is recorded below. K3s remains at the freshly
upgraded 1.35.9 while it is observed.

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

## Authorized rollout — 2026-10-19

- All ten hosts: Tailscale 1.104.1, preserving node identities. Each host passed
  Running/online, direct root SSH and Kubernetes Ready checks before advancing.
  Only the Tailscale package was upgraded; no OS/kernel upgrades or reboots.
  Laptop-2's apt-daily metadata refresh had been stuck for 52 days; stopping that
  stale service released its lock. The apt timer remains enabled.
- Both Cloudflared deployments: 2026.10.1, two Ready replicas each. Tunnel
  connections registered and public requests to kelsey.green and talaria.deals
  returned 200.
- Prowlarr 2.6.5, Sonarr 4.0.20 and Radarr 6.4.4: sequential upgrades, with
  authenticated version API checks after each. FlareSolverr v3.5.2 completed
  a browser request to example.com with HTTP 200. Decluttarr v2.2.0 connected
  successfully to Sonarr, Radarr and qBittorrent.
- qBittorrent 5.2.4: version API passed and traffic exited through the Vancouver
  VPN. A startup PIA token request timed out; restarting only the VPN
  connection recovered port forwarding, and its assigned port matched the
  qBittorrent listening port. Gluetun stays on the existing stable v3.41.3.
- Recyclarr 8.8.0: CronJob image updated and a one-off `sync --preview` completed
  against both services. The preview does not apply quality-profile changes.

### Recovery material

Both `media/pre-app-upgrades-20261019` and
`games/pre-app-upgrades-20261019` K8up backups completed. Native arr backups
were requested through each application's API, then archived and checked.
RomM has a verified SQL dump (1,432,061,453 uncompressed bytes) and three Ready
CSI snapshots: `romm-data-pre-540-20261019`, `romm-db-pre-540-20261019` and
`romm-webstation-pre-540-20261019` in `games`. These recovery snapshots are
retained deliberately. The database volume snapshot is crash-consistent; use
the logical dump for an application-consistent database restore.

Private archives and checksums are under
`/home/node/.local/state/app-upgrades/20261019/` (directory 0700, files 0600).
Tailscale state backups are host-local under `/var/backups/tailscale-pre-1.104.1/`.
Do not commit these files. No full restore drill was performed.

RomM's deployment progress deadline is now 2400 seconds, and its deploy script
waits up to 2700 seconds, allowing its existing 30-minute startup probe budget
plus image preparation time. Its MariaDB patch to 11.4.13 was applied separately
and passed version and query checks before starting the 5.4.0 application.

### Final RomM verification

RomM 5.4.0 completed migrations between 04:13:01 and 04:19:44 UTC without a
container restart. RomM, MariaDB and Webstation are Ready. Its local heartbeat
and external HTTPS ingress returned 200. Database counts exactly matched the
pre-upgrade baseline: 186,964 ROMs, 69 platforms and two users. The MariaDB
backup-helper image was aligned to 11.4.13 for future scheduled dumps. No new
CORS allowlist was needed for the configured same-origin frontend; interactive
login, emulator launches and save/sync client flows were not exercised.

All ten Kubernetes nodes remained Ready. All four changed Helm charts passed
lint using their deployed values, and version pins match the running workloads.
The temporary Recyclarr preview Job was removed; backup resources and recovery
snapshots remain intentionally.

### Remaining application health warnings

Prowlarr reports unavailable upstream indexers and three missing definitions:
Zamunda RIP, GamesTorrents and Torrent Oyun indir. Sonarr/Radarr also report
upstream indexer failures, some lasting more than six hours. These definitions
were retained rather than deleting user configuration during an upgrade. Their
repair/removal is separate from the completed version rollout.
