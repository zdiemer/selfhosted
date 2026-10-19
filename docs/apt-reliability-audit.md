# APT reliability audit — 2026-10-19

The ten-node audit found daily automatic security updates enabled everywhere,
with Ubuntu base/security (and configured ESM) origins allowed. This does not
cover arbitrary third-party packages such as Tailscale. Kernel exclusions and
`Automatic-Reboot=false` were present on all nodes and remain unchanged.

Eight nodes had completed unattended-upgrade runs within the preceding day.
Laptop-2's last completed run was August 26: a metadata-refresh process had held
up `apt-daily.service` for 52 days before it was stopped during the Tailscale
upgrade. Laptop-6's last completed run was September 23; its upgrade service
held the periodic lock until October 18, then logged a segmentation fault.
A subsequent `apt-get check` also crashed in `libapt-pkg`, yet the wrapper
reported success. The original crash's underlying cause is not established.

The prior Tailscale-only repository refresh also removed other package indexes
through APT's normal list cleanup. Full repository refreshes now succeeded on
all ten hosts. Future targeted refreshes must disable list cleanup or immediately
restore full indexes. `dpkg --audit` was clean across the fleet; cached and
uncached `apt-get check` both passed on the affected laptops, and package-file
verification found no modifications to their APT binaries/libraries.

`scripts/k3s/apt-harden.sh --all` deployed network timeout/retry settings, strict
refresh errors, periodic outcome logging, a metadata-only 30-minute service
limit, and an explicit installation-service cache check. Both APT timers are
enabled and active. Installation is deliberately not subjected to a forced
runtime deadline. Manual update and version-report scripts now bound their
metadata refresh subprocesses too.

See [the operational guidance](../scripts/k3s/README.md#apt-reliability) for
verification and recovery. A successful timer/unit result alone is insufficient;
check completion stamps and unattended-upgrades logs, particularly after crashes.

Fresh-index validation found no eligible unattended updates on the other eight
nodes. The affected laptops selected 192 packages (laptop-2) and 75 packages
(laptop-6). The initial three-minute diagnostic dry-run limit was too short for
these backlogs; it stopped dry runs only. Normal systemd unattended-upgrade runs
were then started without an installation deadline and both progressed into
package installation. No `linux-image-*`, `linux-modules-*` or `linux-headers-*` upgrades were
selected; `linux-libc-dev` userspace development headers remain eligible.

The fleet still logs an outdated development-release metadata warning from
`distro-info-data`; its installed version matches the configured repository's
candidate. That warning did not prevent the successful eligibility checks or
installation. Do not suppress errors or enable third-party origins to hide it.

## Verified recovery

Both actual unattended-upgrade runs finished with `All upgrades installed`:

- Laptop-6: all 75 selected packages, completed 2026-10-19 04:38:40 UTC.
- Laptop-2: all 192 selected packages, completed 2026-10-19 04:45:49 UTC.

Both subsequently passed `dpkg --audit` and `apt-get check`, and their normal
`apt-daily.service` refreshes completed successfully with current update stamps.
The final fleet check confirmed both timers enabled/active, the 30-minute
metadata deadline effective, clean dpkg state, intact kernel/reboot guards, and
an unattended-upgrades completion stamp less than 24 hours old on all ten hosts.
All ten Kubernetes nodes remained Ready. No reboot was performed.

Shell syntax checks passed for all three changed scripts and `git diff --check`
passed. The deployment helper is idempotent and can apply these settings to new
nodes; the node join repository was not changed by this audit.
