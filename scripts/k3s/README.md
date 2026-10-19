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
| [`cleanup.sh`](cleanup.sh) | Reclaim disk on nodes. `--report` for a usage report (also flags orphaned local PVs), `--deep` to purge images, containerd snapshots and Docker state — sparing whatever running containers are standing on, see below. Every run ends by naming any pod whose image went missing |
| [`restart.sh`](restart.sh) | Restart nodes. `--all` rolls agents first, draining/uncordoning each; `--service-only` restarts k3s rather than rebooting; `--force` skips the drain |
| [`update.sh`](update.sh) | Rolling OS package updates, agents first. `--reboot` to auto-reboot when required. Warns when a reboot would change kernel, and refuses to do it unattended on a node that can't recover from a bad boot |
| [`kernel.sh`](kernel.sh) | Kernel state per node and boot-failure recovery. `--report` for running vs installed kernels and which nodes are one reboot from changing; `--no-auto-kernel` to stop unattended-upgrades installing kernels; `--protect` for panic auto-reboot and a bounded recordfail wait; `--hold`/`--unhold` |
| [`k3s-upgrade.sh`](k3s-upgrade.sh) | One-node, explicitly pinned upgrade. Default is read-only; `--prepare` stages verified binaries and off-node backups; `--execute` requires SSH and fresh recovery material. See [upgrade runbook](../../docs/k3s-upgrade.md) |
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
./k3s-upgrade.sh --version v1.35.9+k3s1 --node zachd-ubuntu # read-only checks
```

## Why `--deep` spares images

`--deep` used to run `k3s crictl rmi --all` and then offer containerd every
snapshot on the node. Both of those reach past unused images into the ones
holding up **running containers**, and the failure that produces is silent.

On 2026-08-20 a `--deep` run on `zachd-ubuntu-2` hollowed out all 19 pods on
the node. Nothing crashed and nothing restarted: each process kept running on
the inodes it already had open, every readiness probe stayed green, and
`kubectl get pods` showed `Running` for another day. What broke was everything
*new* — any exec, any subprocess, any binary looked up by path got ENOENT.
`claude-workspace` sat 7/7 Ready while its Signal/WhatsApp gateway could not
spawn the MCP server it relays approvals through (`MCP tool mcp__gw__approve
… not found. Available MCP tools: none`) and Happy could not spawn `tmux` or
`git`. The errors named the app, never the node.

So the deep steps now ask the node what it is standing on first:

- images referenced by any container (`crictl ps -a`), plus the pause image
  that backs pod sandboxes and never shows up there, are kept;
- snapshots are walked parent-first from every `Active` one — that chain is a
  running container's rootfs and the image layers under it — and only what is
  outside every chain is offered for removal. On a typical node that is ~6 of
  ~200 snapshots, which is also the measure of how much the old loop was
  relying on containerd to say no.

Every run, deep or not, ends by checking that each running container's image
still exists, and prints the `kubectl delete pod` lines for any that lost it.
kubelet does not notice this state and will not restart the pod for you — a
delete (which re-pulls) is the only fix.

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

## Secrets encryption at rest

Enabled 2026-08-13 on all three servers. Every Secret in etcd is AES-CBC
encrypted, including the ~405 that predated it.

**The procedure is not the one in the k3s docs' single-server example, and
getting it wrong takes a control-plane node down.** Each server that starts with
`secrets-encryption: true` and no existing config GENERATES ITS OWN KEY. In HA
that is fatal on contact: the first server encrypts `k3s-serving`, the second
cannot decrypt what its peer wrote, and its apiserver fails to list Secrets and
never finishes starting —

    failed to decrypt data: invalid padding on input
    cacher (secrets): unexpected ListAndWatch error ... reinitializing...

To enable it, or to bring a rebuilt server back in:

```sh
# 1. On a server that already has it, copy BOTH cred files to the new one.
#    Both. See below.
ssh root@<new-server> 'systemctl stop k3s'
sudo cat /var/lib/rancher/k3s/server/cred/encryption-config.json \
  | ssh root@<new-server> 'install -m600 /dev/stdin \
      /var/lib/rancher/k3s/server/cred/encryption-config.json'

# 2. Only then set the flag and start.
ssh root@<new-server> '
  grep -q secrets-encryption /etc/rancher/k3s/config.yaml 2>/dev/null \
    || echo "secrets-encryption: true" >> /etc/rancher/k3s/config.yaml
  systemctl start k3s'

# 3. Every server must agree before any rotation will run.
sudo k3s secrets-encrypt status      # expect: All hashes match
sudo k3s secrets-encrypt reencrypt --force
# then restart the OTHER servers so they pick up the finished stage.
```

**`encryption-state.json` is the one that bites.** It is a separate file next to
the config, it is not JSON despite the name, and it records the hash k3s
publishes as the `k3s.io/encryption-config-hash` node annotation. Replace the
config alone and the node keeps advertising the OLD hash forever — `status` says
`hash does not match`, every rotation is refused, and nothing in the logs
explains why. Restarting does not refresh it; deleting the annotation does not
either, because k3s rewrites it from that file.

The fix is to remove `encryption-state.json` (keeping the config) and restart:
k3s regenerates the state from the key it already has rather than minting a new
one. Verified — the config hash was unchanged across the restart.

Reading the truth without sudo, which is often faster than logging in:

```sh
kubectl get node <server> -o jsonpath='{.metadata.annotations.k3s\.io/encryption-config-hash}'
```

The stage prefix is part of the value (`start-<hash>`,
`reencrypt_finished-<hash>`), so servers mid-rotation differ legitimately. Compare
the hash AFTER the prefix; if those match, only the stage is lagging and a
restart settles it.

### Node3 IPv6 address selection

`node-configs/zachd-ubuntu-3/20-node-ip.yaml` is installed on node3 at
`/etc/rancher/k3s/config.yaml.d/20-node-ip.yaml`. It pins K3s to its existing
IPv4 address and eno1's non-temporary ULA IPv6 address. Automatic selection had
chosen a rotating privacy address; after expiry, kubelet logged
`failed to validate secondaryNodeIP` every ten seconds.

Apply changes to this node-specific file with a K3s service restart on node3
only, after checking the other control-plane nodes are healthy. Verify the
Node's InternalIP addresses and fresh kubelet logs afterward. If the LAN ULA
prefix or NIC changes, update this pin to a non-temporary address present on
the host. Removing the file and restarting K3s restores automatic selection.

K3s configuration reference: https://docs.k3s.io/installation/configuration

### Node5 IPv6 address selection

`node-configs/zachd-ubuntu-5/20-node-ip.yaml` applies the same fix to node5,
using enp4s0's stable ULA `fd35:ace4:a929:1:3a05:25ff:fe3a:5204` and IPv4
`192.168.4.21`. Installed and verified on 2026-10-19 with a same-version K3s
restart; the stale secondaryNodeIP warnings stopped. Follow the same
control-plane health and address checks described above.

Rechecked after the 1.35.9 upgrade on 2026-10-19: the configured ULA remains
present and non-temporary, matches the Node InternalIP, and is reachable over
IPv6 from node0. No secondaryNodeIP warnings appeared in the recent journal;
no additional network changes or service restart were necessary.

## Bounded pod DNS upstreams

`k3s-resolv-conf.py` generates `/etc/rancher/k3s/resolv.conf` from
`/run/systemd/resolve/resolv.conf`, keeping at most three unique usable
nameservers and preserving search domains/options. Loopback and link-local
resolvers are excluded because ordinary pods cannot use the host's stub.
Empty/invalid input leaves the previous good file intact; updates are atomic.
Host DNS and Tailscale's resolver integration remain managed by systemd-resolved.

Installed on `zachd-ubuntu`, `zachd-ubuntu-laptop`, `zachd-ubuntu-laptop-2`, and
`zachd-ubuntu-laptop-6` after DHCP supplied four upstreams (eight entries on
machines with two interfaces), causing recurring `DNSConfigForming` warnings.

Install the Python script as `/usr/local/sbin/k3s-resolv-conf.py` (executable),
and the matching `.service` and `.timer` files in `/etc/systemd/system/`.
Install `k3s-resolv-conf-dependency.conf` as
`/etc/systemd/system/k3s.service.d/30-resolv-conf.conf` on servers, or
`/etc/systemd/system/k3s-agent.service.d/30-resolv-conf.conf` on agents.
Copy `node-configs/common/30-resolv-conf.yaml` into
`/etc/rancher/k3s/config.yaml.d/30-resolv-conf.yaml`.
Run `systemctl daemon-reload`, start `k3s-resolv-conf.service`, and enable/start
`k3s-resolv-conf.timer`. Restart the affected k3s services one node at a time
and verify readiness and DNS before continuing. The timer follows DHCP/resolver
changes every minute without another k3s restart.

Verify with `systemctl status k3s-resolv-conf.timer`, the generated resolver
file, and DNS queries from both host-network and ordinary cluster pods.
Old warning events remain until Kubernetes expires them; check their latest
occurrence times rather than merely their presence.

Parser/update regression checks:
`python3 -m unittest discover -s scripts/k3s/tests -p test_resolv_conf.py`.

## Laptop-6 crash-reporting memory guard

On 2026-10-18, kubelet replayed a `SystemOOM` warning after restarting. The
kernel log dates the actual kill of Apport PID 1502507 to September 24:
Apport used about 6 GiB while processing a Celery/Python crash on this 8 GiB
host. There were no new kernel OOM records on October 18. A separate orphaned
Apport process had consumed one CPU core for 23 days; SIGTERM stopped it.

On `zachd-ubuntu-laptop-6`, `/etc/default/apport` now has `enabled=0` and
`apport.service` is disabled/stopped. Install
`node-configs/zachd-ubuntu-laptop-6/90-no-host-coredumps.conf` into
`/etc/sysctl.d/` and apply it with `sysctl -p` against that file. It discards
core dumps rather than launching an unbounded host crash reporter or writing
large core files. Kernel crash messages and container logs remain available;
full core-dump debugging requires deliberately restoring a bounded handler.
