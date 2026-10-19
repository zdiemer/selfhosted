# K3s upgrade runbook

Prepared 2026-10-19. No K3s version upgrade has been executed.

## Target and order

Pin `v1.35.9+k3s1` for the first rollout. Node2 already runs 1.35.4, ahead of
all three 1.34 servers. Upgrade servers one at a time: `zachd-ubuntu`,
`zachd-ubuntu-5`, `zachd-ubuntu-3`. After all servers pass, use laptop-6 as the
worker canary, then laptop, laptop-2, node4, node2, node1, node6.

Observe for 24–48 hours before separately planning 1.36.5. Do not follow the
moving stable channel or skip a minor. Do not combine OS/kernel updates,
node3 storage migration, large database maintenance, or bulk image builds
with this rollout.

Use cordon plus an in-place K3s binary replacement/service restart. Running
containers normally survive this; brief API interruptions remain possible.
A full drain would strand node-pinned Postgres, Elasticsearch, Steam Headless,
and GPU workloads and move many single-attach iSCSI volumes unnecessarily.
No reboot or killall script is part of this workflow.

## Preparation evidence

- All ten nodes Ready; all three local API `/readyz` and etcd health checks pass.
- Node5 IPv6 pin installed, same-version service restart completed, stable ULA
  advertised, stale secondaryNodeIP warnings stopped, node uncordoned.
- Node5 registry config matches the managed Secret. Authenticated `/v2/`
  returned 200; `k3s crictl pull` of the existing Claude v38 image succeeded.
  No credential rotation was needed. A manual manifest HEAD returned 404;
  the actual CRI pull is the successful end-to-end check.
- KEDA upgraded through Helm from 2.19.0 to 2.20.0 (release revision 2).
  All three components Ready, external metrics API available, all five Talaria
  ScaledObjects Ready, Redis metrics populated and reconciliation active.
  The chart now has a reproducible pin in `infra/keda/upgrade.sh`.
- KubeVirt 1.9 supports Kubernetes 1.34–1.36. CDI 1.66 release notes include
  Kubernetes 1.36 CI lanes. These do not prove VM migration health: existing
  virt-handler certificate warnings still need investigation before relying
  on live migration. Keep Windows on node1 for the in-place restart.
- Democratic CSI 1.9.5 and its older sidecars do not provide a verified 1.35
  support matrix in the reviewed release notes. A disposable 1Gi iSCSI PVC
  was provisioned, written on node2 (already 1.35), detached and reattached
  on node4; the marker survived. The test pod, PVC and backing PV were then
  removed. Keep versions unchanged and repeat storage checks after upgrading.

Private recovery data lives outside Git:

- `/home/node/.local/state/k3s-upgrade/20261019-prerequisites/`: ten verified
  tar archives of the existing binaries, `/etc/rancher/k3s`, systemd units and
  drop-ins, server tokens/credential files, plus the fresh node0 snapshot
  `pre-upgrade-20261019-zachd-ubuntu-1792373615`; SHA256SUMS records integrity.
- `/home/node/.local/state/k3s-upgrade/20261019-staged/`: per-server preparation
  manifests and fresh snapshot/token/config/binary archives. Each manifest
  identifies its host staging directory and binary/archive checksums.

These paths are on the NAS-backed workspace PVC, off the nodes' local disks;
files/directories are private (0600/0700). They contain credentials and must
never be committed. Confirm that the NAS recovery copies can be retrieved
without a working cluster before a disaster restore. Preparation archives
are not a substitute for database/PVC backups or a tested restore. Existing
Talaria and Paperless backup jobs report success, but their application data
has not been restored as part of this preparation; refresh database backups
at the actual maintenance window.

## Remaining execution gate

Tailscale SSH timed out on all ten nodes from this workspace. Existing
privileged registry configuration pods provide working preparation access,
but depend on Kubernetes. Establish direct root SSH access from the execution
machine before starting an upgrade. The script deliberately does not accept
`--execute --transport pod` and checks direct access to all control-plane
members. A working host console remains useful if networking fails.

## Commands

The entry point is `scripts/k3s/k3s-upgrade.sh` (Python 3 standard library
implementation in `k3s_upgrade.py`). An exact version and one node are required;
there is no automatic whole-cluster rollout or implicit execution.

```sh
# Read-only health and version-skew check using existing privileged pods.
scripts/k3s/k3s-upgrade.sh --version v1.35.9+k3s1 \
  --node zachd-ubuntu --transport pod

# Download/check the release, snapshot etcd, and verify an off-node archive.
# This does not change the installed binary or restart K3s.
scripts/k3s/k3s-upgrade.sh --version v1.35.9+k3s1 \
  --node zachd-ubuntu --transport pod --prepare \
  --recovery-dir /home/node/.local/state/k3s-upgrade/20261019-staged

# Only in the upgrade window, from a machine with working direct SSH:
scripts/k3s/k3s-upgrade.sh --version v1.35.9+k3s1 \
  --node zachd-ubuntu --execute \
  --recovery-dir /home/node/.local/state/k3s-upgrade/20261019-staged
```

For an external execution machine, copy the private recovery directory
securely and update the manifest's `archive` path to its new absolute location.
The checksum must still match. Preparation expires after 24 hours; rerun
`--prepare` before execution if needed. Prepare workers after all servers have
reached the target, because the same version-skew guard applies to all modes.

The workflow supports the standard `/usr/local/bin/k3s`, systemd and
`/var/lib/rancher/k3s` layout used here. Custom data directories need a separate
snapshot/restore procedure. Binaries are verified against upstream SHA256
checksums and their reported version before any restart. The existing unit,
environment, config and drop-ins are preserved by replacing only the binary.

## Execution gates and recovery

- Reject malformed versions, downgrades, skipped minors, and agents ahead of
  the oldest server. Require all nodes Ready and no node pressure, healthy
  local API/etcd checks on every server, and settled deployments/StatefulSets.
- Require a fresh, checksum-verified off-node recovery archive and recheck the
  staged binary. Atomically create `kube-system/k3s-upgrade-lock` so two
  invocations cannot restart different members concurrently.
- Cordon the node; a host-owned transient systemd unit replaces the binary
  atomically and restarts the original service independently of the caller.
- Require the target kubelet version, a new service invocation, a fresh node
  lease and Ready condition, then two successful all-server health checks
  separated by 15 seconds and settled deployments/StatefulSets.
- Restore the previous cordon state only after success. Release the lock.
  On failure, stop, keep the node cordoned and keep the lock for investigation.

After **every** node, inspect new restarts/events and test DNS, ingress/TLS,
CrowdSec behavior, registry pulls and volume access. After the servers, also
check Talaria queries, Paperless, KEDA metrics, and Windows. Node3 needs an
etcd latency/disk-stall review. Do not proceed on new leader instability,
recurring fsync stalls, failed mounts, or application regressions.

A failure is not permission for automatic downgrade. Inspect the host's
`k3s-upgrade-<timestamp>` and `k3s`/`k3s-agent` journal first. Minor-version
rollback may require restoring the pre-upgrade etcd snapshot with its matching
server token and coordinating all server members. Use the official rollback
procedure; PVC data is a separate recovery concern. After recovery and health
verification, an operator can clear the retained lock with:

```sh
kubectl -n kube-system delete configmap k3s-upgrade-lock
```

## References

- https://docs.k3s.io/upgrades/manual
- https://docs.k3s.io/upgrades/roll-back
- https://kubernetes.io/releases/version-skew-policy/
- https://github.com/k3s-io/k3s/releases/tag/v1.35.9%2Bk3s1
- https://keda.sh/docs/2.20/operate/cluster/
- https://github.com/kedacore/keda/releases/tag/v2.20.0
- https://kubevirt.io/user-guide/release_notes/
- https://github.com/kubevirt/containerized-data-importer/releases/tag/v1.66.0
- https://github.com/democratic-csi/democratic-csi/releases/tag/v1.9.5
