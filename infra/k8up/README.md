# k8up — the cluster's backups

Every PVC copied to a restic repository on the NAS, nightly, with the databases
dumped rather than snapshotted mid-write.

Until this existed the only backup in the cluster was Minecraft's `mc-backup`
sidecar, and it wrote to a volume on the same node and the same disk as the
world it protected. Everything else had nothing.

## What protects what

Three layers, different failure modes, none replacing another:

| Layer | Where | Protects against | Useless against |
|---|---|---|---|
| ZFS snapshots (`infra/democratic-csi`) | same pool | a mistake, minutes ago | pool loss |
| **restic (here)** | different dataset, same NAS | pool corruption, deletion, a bad upgrade | the NAS burning down |
| *(not built)* offsite | — | — | — |

The obvious gap is offsite. `vault/k8s/restic` is on the same box as the data it
protects. A `restic copy` or `zfs send` to a second target is the next thing
worth doing.

## The repository

One restic repository at `vault/k8s/restic`, shared by every namespace over NFS.

Shared rather than per-namespace because restic deduplicates **within** a
repository and much of this cluster is the same base images and the same
Postgres pages — separate repos would store all of it many times over. The cost
is restic's exclusive prune lock, so exactly one Schedule (`k8up/maintenance`)
prunes and checks, and every other Schedule only writes.

The export was created by hand on the TrueNAS: democratic-csi only exports
datasets it provisioned, and this one is not a CSI volume. It maps all clients
to root, because k8up's backup container runs as uid 65532 and the dataset is
root-owned — without that, `restic init` fails with `mkdir
/restic-repo/snapshots: permission denied`.

Each namespace gets a static PV/PVC pair pointing at that one export. Static PVs
are the one legitimate use of `storageClassName: ""` — these genuinely have no
class, unlike a chart-managed PVC where `""` leaves the claim Pending forever.
All are `Retain`: uninstalling this chart must never be able to delete the
backups.

## 🔴 The repository password

`restic.password` in `values.local.yaml` derives the repository's master key.
**There is no recovery path.** Without it every backup on the NAS is unreadable
ciphertext, however intact the files are — which is strictly worse than having
no backups, because you will believe you are covered.

It is in 1Password (`infra-k8up`) via `scripts/secrets.sh`. Keep a copy
somewhere that is not this cluster.

Changing it does not re-key anything. It silently starts a **second** repository
at the same path and orphans every existing snapshot. `upgrade.sh` compares
against the live secret and refuses if they differ.

## Database dumps

A file-level copy of a running database is a copy of a database mid-write. It
usually restores — which is worse than never restoring, because you find out
during a recovery.

Three `PreBackupPod`s run a real dump first; k8up captures stdout as a file in
the repository alongside the volumes:

| Namespace | Database | Command | File |
|---|---|---|---|
| `docs` | Postgres 16 | `pg_dump -F c` | `docs-dump.pgdump` |
| `games` | MariaDB | `mariadb-dump --single-transaction` | `games-dump.sql` |
| `discord` | MongoDB | `mongodump --archive --gzip` | `discord-dump.archive.gz` |

They are clients over the service, not sidecars — nothing here modifies the
charts that own the databases. Credentials are *referenced* from those charts'
own secrets, so a rotation is picked up with no changes here.

The dump is what you restore from; the volume copy is what you have if the dump
turns out unusable.

Minecraft has no dump hook deliberately: the itzg sidecar already writes
consistent world tarballs (RCON `save-off` → `save-all flush` → tar → `save-on`)
into `mc-backups`, and k8up backs that volume up like any other.

## Schedules

Nightly, staggered across 01:00–05:00 local. They share one repository and one
NAS — three namespaces starting at 02:00 would contend on the restic lock and
the slowest would decide when they all finished.

`kube-system` and `auth` are included even though those volumes deliberately
stayed on `local-path`. That decision was about **availability** — the NAS being
down must not stop you logging in to fix the NAS — and says nothing about
durability.

Maintenance runs Sunday: prune at 06:00, check at 07:00.

Backups are **opt-out**, not opt-in (`skipWithoutAnnotation: false`). A new
service is protected the day it lands rather than the day someone remembers.
Exclude a volume with `k8up.io/backup=false` on the PVC.

## Proving a restore

A backup that has never been restored is a belief, not a backup.

```bash
# 1. take one now
kubectl -n docs create -f - <<'YAML'
apiVersion: k8up.io/v1
kind: Backup
metadata: { name: proof, namespace: docs }
spec:
  backend:
    repoPasswordSecretRef: { name: k8up-restic-password, key: password }
    local: { mountPath: /restic-repo }
    volumeMounts: [{ name: restic-repo, mountPath: /restic-repo }]
  volumes: [{ name: restic-repo, persistentVolumeClaim: { claimName: k8up-restic-repo } }]
YAML

# 2. find the snapshot
kubectl -n docs get snapshots.k8up.io

# 3. restore it into a scratch PVC with a Restore whose restoreMethod.folder
#    points at that claim, then diff the result against the live volume.
```

Done on 2026-08-08 for `stirling-configs`: 11 files, `diff -r` identical to
live, JWT signing keys included.

## Gotchas that cost time here

**`backupCommand` is a string, not a list.** It becomes the pod's
`k8up.io/backupcommand` annotation and is shell-word-split, so a whole pipeline
has to sit inside one quoted `-c` argument.

**PreBackupPods are realised as Deployments.** A Deployment's pod template only
accepts `restartPolicy: Always`; `Never` is rejected by the API server, the
operator retries forever, and the Backup sits at `PreBackupPodReady=True` with
no job ever created and no error surfaced on the Backup itself.

**Helm installs `crds/` on first install and never upgrades them.** Bumping the
chart version does not update the CRDs — apply them by hand when upstream
changes them.

## Verify

```bash
kubectl get schedules -A
kubectl get backups -A
kubectl get snapshots.k8up.io -A
kubectl -n k8up logs deploy/k8up --tail=50
```

Every namespace's `k8up-restic-repo` PVC must be `Bound` — an unbound one means
that namespace silently backs up nothing. `upgrade.sh` checks this and fails.
