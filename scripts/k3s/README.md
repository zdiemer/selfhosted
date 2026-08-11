# scripts/k3s — cluster node operations

Node-level operations for the k3s cluster: health checks, disk cleanup, rolling
restarts, OS updates, k3s version upgrades.

These lived in the sibling talaria project's `scripts/k3s/`, with **zero talaria
references across all 1,561 lines** — they were always cluster ops, not app ops.
`restart.sh` even hardcodes node names, which rather makes the point about where
they belong. Moved here unchanged.

Unlike everything else in this repo, these aren't per-project: they operate on the
nodes themselves, which is why they sit at the repo root rather than under a
chart directory.

| Script | What it does |
|---|---|
| [`drain-preflight.sh`](drain-preflight.sh) | Say what draining a node will cost, without draining it: what gaps, what cannot move at all, what a PDB will block, and which volumes face a slow iSCSI detach/attach handoff. `--node <name>`, `--all`. Exits 0 / 2 (something gaps) / 3 (something is stuck or blocked). Every drain path below calls it automatically — `DRAIN_PREFLIGHT=off` skips it, `=strict` refuses to drain on a 3 |
| [`debug.sh`](debug.sh) | Diagnose node health — tailscale, k3s service, drive/memory health, CPU temp, pending reboots, failed units, resource usage, pods. `--node <name>`, `--all`, `--json` |
| [`cleanup.sh`](cleanup.sh) | Reclaim disk on nodes. `--report` for a usage report (also flags orphaned local PVs), `--deep` to purge images, containerd snapshots and Docker state |
| [`restart.sh`](restart.sh) | Restart nodes. `--all` rolls agents first, draining/uncordoning each; `--service-only` restarts k3s rather than rebooting; `--force` skips the drain |
| [`update.sh`](update.sh) | Rolling OS package updates, agents first. `--reboot` to auto-reboot when required. Warns when a reboot would change kernel, and refuses to do it unattended on a node that can't recover from a bad boot |
| [`kernel.sh`](kernel.sh) | Kernel state per node and boot-failure recovery. `--report` for running vs installed kernels and which nodes are one reboot from changing; `--no-auto-kernel` to stop unattended-upgrades installing kernels; `--protect` for panic auto-reboot and a bounded recordfail wait; `--hold`/`--unhold` |
| [`k3s-upgrade.sh`](k3s-upgrade.sh) | Rolling k3s version upgrade, agents then server. `--version <ver>`, or latest stable |
| [`versions-report.sh`](versions-report.sh) | Weekly staleness report: pending apt/security updates, kernel drift, k3s vs stable channel, tailscale spread, reboot-guard coverage. Publishes to the `versions-report` ConfigMap (the status.diemer.codes "Pending Upgrades" panel) and the pinned GitHub issue; run by `scripts/systemd/selfhosted-versions-report.timer`. `--dry-run` prints the markdown. Report-only — acting on it is `update.sh`/`k3s-upgrade.sh`/`kernel.sh`. A tailscale package upgrade restarts tailscaled and can drop the very ssh session driving `update.sh`; apt is idempotent, re-run |
| [`_common.sh`](_common.sh) | Shared helpers. Sourced, not executed |

## How they reach the nodes

`SSH_CMD="tailscale ssh"` — every node is reached over Tailscale as root, no
password. So they work from anywhere on the tailnet, and they need `tailscale`
and `kubectl` on your machine, not on the nodes.

That includes the claude-workspace pod (`dev/claude-workspace`): it carries
kubectl (in-cluster cluster-admin SA) and a userspace tailscaled, so these
scripts run from `/term` unmodified. One behavioral note: `_common.sh`'s
local-node shortcut (`is_local_node`) never matches the pod's hostname, so
from the pod *every* node — including the one hosting the pod — goes over
tailscale ssh. Expected and fine.

Anything touching `--all` is slow by nature: it's ten sequential SSH sessions, and
the drain/uncordon variants wait for pods to move. `debug.sh --all` takes a couple
of minutes; that's normal.

## Examples

```bash
./debug.sh --node zachd-ubuntu     # one node, full health readout
./debug.sh --all                   # every node (slow — 10 SSH sessions)
./cleanup.sh --all --report        # what's eating the disks?
./cleanup.sh --node zachd-ubuntu-3 --deep
./restart.sh --all --service-only  # bounce k3s everywhere, no reboots
./k3s-upgrade.sh --version v1.31.4+k3s1
```

## Kernels are not upgraded automatically

`unattended-upgrades` installed a 7.0 kernel that one laptop could not boot; it
panicked on the next restart — days later, for an unrelated reason — and needed
a physical power cycle. The fleet mixes server hardware with old laptops, and
the same 7.0.0-28 that runs fine on `zachd-ubuntu` and `zachd-ubuntu-2` is what
killed the laptop, so no kernel can be called good fleet-wide.

Kernel packages are therefore blacklisted from unattended-upgrades on every
node, and in [`k3s-cluster/join-cluster.sh`](https://github.com/zdiemer/k3s-cluster)
for nodes joining later. Kernels change only through `update.sh`, which drains
one node at a time and waits for it to come back. Everything else still updates
unattended.

`kernel.sh --report` shows which nodes are one reboot away from a different
kernel — the moment the risk actually lands, which is otherwise invisible.

Drain-based operations move pods around. Per the root README's convention, don't
roll nodes hosting the Minecraft server while players are on — use
[`minecraft/upgrade.sh`](../../minecraft/upgrade.sh) to flush the world first, or
pick an offline window.
