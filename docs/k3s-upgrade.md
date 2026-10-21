# K3s upgrade runbook

Upgrade one node at a time to an explicitly reviewed release. Discover the live
versions first; never use a moving channel, skip a minor, or upgrade agents ahead
of the oldest server. Check target-version compatibility for KEDA, KubeVirt/CDI,
storage drivers, and bundled add-ons before preparing binaries.

## Preparation and order

1. Verify direct root SSH to every control-plane member. Tailscale authorization
   can expire; a privileged pod is useful for preparation but cannot be the sole
   recovery path while K3s itself restarts. Preserve a host-console option.
2. Verify etcd quorum, local API health, all nodes Ready without pressure, and
   settled workloads. Inventory node-pinned workloads and local volumes.
3. Verify fresh off-node etcd/token archives and application backups. Test
   disposable storage provisioning, writing, detaching, and cross-node reading.
4. Upgrade servers sequentially, then a worker canary, then remaining workers.
   Recheck gates after each node. Prepare workers only after all servers reach
   the target version.

Use cordon plus an in-place binary replacement/service restart. A full drain can
strand pinned databases, VMs, and GPU workloads or move single-attach volumes
unnecessarily. This workflow does not include a reboot or killall. Keep OS/kernel
updates, storage migration, and heavy database maintenance separate.

During mixed-version server operation, bundled chart downloads can reach a server
that lacks the target archive. Check Helm controller reconciliation and chart
availability on all servers before advancing; verify checksums if staging exact
archives between servers. Do not mistake an unfinished add-on rollout for success.

Set these variables to the reviewed target and a private off-node directory:

```sh
K3S_TARGET='vX.Y.Z+k3sN' # Replace with the exact reviewed release.
K3S_RECOVERY_DIR="$HOME/.local/state/k3s-upgrade/$(date -u +%Y%m%dT%H%M%SZ)"
```

## Commands

The entry point is `scripts/k3s/k3s-upgrade.sh` (Python 3 standard library
implementation in `k3s_upgrade.py`). An exact version and one node are required;
there is no automatic whole-cluster rollout or implicit execution.

```sh
# Read-only health and version-skew check using existing privileged pods.
scripts/k3s/k3s-upgrade.sh --version "$K3S_TARGET" \
  --node zachd-ubuntu --transport pod

# Download/check the release, snapshot etcd, and verify an off-node archive.
# This does not change the installed binary or restart K3s.
scripts/k3s/k3s-upgrade.sh --version "$K3S_TARGET" \
  --node zachd-ubuntu --transport pod --prepare \
  --recovery-dir "$K3S_RECOVERY_DIR"

# Only in the upgrade window, from a machine with working direct SSH:
scripts/k3s/k3s-upgrade.sh --version "$K3S_TARGET" \
  --node zachd-ubuntu --execute \
  --recovery-dir "$K3S_RECOVERY_DIR"
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

- [Manual upgrades](https://docs.k3s.io/upgrades/manual)
- [Rollback](https://docs.k3s.io/upgrades/roll-back)
- [Kubernetes version skew](https://kubernetes.io/releases/version-skew-policy/)
- [K3s releases](https://github.com/k3s-io/k3s/releases)
