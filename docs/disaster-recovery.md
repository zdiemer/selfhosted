# Disaster recovery

Use this as a recovery dependency checklist. Discover current node roles, volume
bindings, replica counts, and backup availability before acting; a past health
check or capacity inventory is not evidence that a restore will work.

## Recovery dependencies

- Embedded etcd needs a majority of its members. A three-member control plane
  tolerates one member failure. Confirm membership and healthy API endpoints.
- NAS-backed application volumes, registry storage, and restic repositories
  share the TrueNAS failure domain. A backup on the same NAS does not protect
  against losing it. Verify an independent copy before relying on NAS recovery.
- Local-path volumes, hostPath data, node selectors, and GPU assignments can
  prevent rescheduling. Inspect each affected workload and its PV node affinity;
  free aggregate cluster capacity does not guarantee that a particular pod fits.
- Agent bootstrap addresses must reach a surviving server. Existing cluster
  membership does not guarantee that a new join or agent restart can connect.

Useful inventory commands:

```sh
kubectl get nodes -o wide
kubectl get deploy,statefulset,daemonset -A
kubectl get pvc -A
kubectl get pv -o wide
kubectl get backups.k8up.io -A
scripts/k3s/drain-preflight.sh --all
```

## Recovery material

Keep server configuration, matching K3s server tokens, encryption configuration,
etcd snapshots, and application backups outside the failed node. Credentials
must remain private and outside Git. Verify checksums and retrieve the archives
without depending on the Kubernetes API before maintenance.

The workspace's `~/.local/state/k3s-upgrade/` holds upgrade recovery manifests
and archives; `~/.local/state/app-upgrades/` and `~/.local/state/renovate-backups/`
hold application recovery material. Workspace-specific backups also live under
`~/.local/state/upgrade-backups/`. These locations are discovery aids, not a
retention guarantee or a substitute for fresh backups. Inspect the actual
archive contents, age, and matching application version before use.

Chart secrets are resolved from 1Password through `values.local.tpl.yaml`.
Use [the shared secrets workflow](code-tree-CLAUDE.md). A database schema rollback
may require its matching data backup; reverting only an image is insufficient.

For PostgreSQL, verify a logical dump or supported physical backup by restoring
into an isolated target. A filesystem snapshot of a running database can be
crash-consistent rather than application-consistent. Check which PVCs are
explicitly excluded from backup; neither a successful backup Job nor a Ready
application proves all required data is recoverable.

## Rebuild order

1. Recover NAS datasets, exports, API credentials, and independent backup access.
2. Recover or initialize the K3s control plane using its matching configuration,
   token, and etcd restore procedure. The sibling cluster join helper requires
   a surviving server; do not assume it bootstraps the first server.
3. Restore node access, networking, required labels, GPU permissions, and host
   settings. Verify API connectivity before joining additional nodes.
4. Install [snapshot-controller](../infra/snapshot-controller/), then
   [democratic-csi](../infra/democratic-csi/) and required storage classes.
5. Install priority classes, DuckDNS, Traefik certificates, and Traefik.
6. Restore the registry and node registry authentication, then BuildKit and its
   required host settings. Rebuild unavailable first-party images as needed.
7. Restore application data and secrets, then deploy dependencies and apps with
   their own upgrade scripts. Keep database writers stopped until data is ready.
8. Test actual reads/writes, authentication, ingress, image pulls, volume
   reattachment, and notifications. Resume schedules and writers in stages.

## Improvements to plan

- Independent/offsite copies of etcd, application, and secrets backups.
- A tested first-server bootstrap procedure and stable API entry point.
- Recovery tests for an application backup and an etcd snapshot in isolation.
- Enough schedulable headroom and redundant placement for critical services.
- [Node3 storage separation](node3-postgres-storage-separation.md); this reduces
  disk contention but does not provide PostgreSQL high availability.

Use [K8up's documentation](../infra/k8up/README.md) for backup/restore tooling and
[the K3s runbook](k3s-upgrade.md) for upgrade archives and rollback constraints.
