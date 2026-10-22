# k8up — the cluster's backups

Every PVC copied to a restic repository on the NAS, weekly, with the databases
dumped rather than snapshotted mid-write.

Until this existed the only backup in the cluster was Minecraft's `mc-backup`
sidecar, and it wrote to a volume on the same node and the same disk as the
world it protected. Everything else had nothing.

## What protects what

Three layers, different failure modes, none replacing another:

| Layer | Where | Protects against | Useless against |
|---|---|---|---|
| ZFS snapshots (`infra/democratic-csi`) | same pool | a mistake, minutes ago | pool loss |
| **restic (here)** | different dataset, same NAS | logical corruption, deletion, a bad upgrade | loss of the NAS/pool/site |
| Offsite cluster backup (not enabled) | — | — | NAS/site loss remains an accepted risk |

Owner decision, 2026-10-22: do not add paid offsite storage for cluster data.
`vault/k8s/restic` shares the NAS failure domain with the source volumes; loss
of the NAS/pool/site can lose both. That risk is accepted given the cost, so
offsite cluster backup is not an outstanding implementation task. The owner
reports droplet backups are enabled; those do not cover cluster data, and a
droplet recovery rehearsal remains a separate task.

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

Four `PreBackupPod`s run a real dump first; k8up captures stdout as a file in
the repository alongside the volumes. Talaria uses the separate atomic backup
Job described below. The selected latest dumps for all five databases have now
passed full isolated restores; [results](recovery/README.md) cover the four
smaller databases and the Talaria section records its earlier drill:

| Namespace | Database | Command | File | Working |
|---|---|---|---|---|
| `docs` | Postgres 16 | `pg_dump -F c` | `docs-dump.pgdump` | yes |
| `games` | MariaDB | `mariadb-dump --single-transaction` | `games-dump.sql` | yes |
| `discord` | MongoDB | `mongodump --archive --gzip` | `discord-dump.archive.gz` | yes |
| `rachel` | Postgres 17 | `pg_dump -F c` | `rachel-dump.pgdump` | yes |
| `talaria` | Postgres 15 | staged and validated `pg_dump -F c` | `/dump/talaria-dump.pgdump` | yes — separate atomic Job |

For the RWO databases the dump is not a supplement, it IS the backup: their
volume snapshots come out 0 B whenever the backup pod lands on a node that
cannot attach the volume.

They are clients over the service, not sidecars — nothing here modifies the
charts that own the databases. Credentials are *referenced* from those charts'
own secrets, so a rotation is picked up with no changes here.

The dump is what you restore from; the volume copy is what you have if the dump
turns out unusable.

Minecraft has no dump hook deliberately: the itzg sidecar already writes
consistent world tarballs (RCON `save-off` → `save-all flush` → tar → `save-on`)
into `mc-backups`, and k8up backs that volume up like any other.

## Schedules

Weekly, one workload per night rather than everything every night. Nightly was
more than this cluster needs and more than the NAS enjoys; spread across the week
each run gets the repository to itself and never bleeds into the morning. A bad
night costs a week of history at worst.

Heaviest first: `default` Monday (~85G), `games` Tuesday (~24G), `minecraft`
Wednesday (~11G), then the small ones Thursday through Sunday.

`kube-system` and `auth` are included even though those volumes deliberately
stayed on `local-path`. That decision was about **availability** — the NAS being
down must not stop you logging in to fix the NAS — and says nothing about
durability.

Maintenance runs Sunday after the last backup: prune at 06:00, check at 08:00.

Backups default to a 1Gi memory limit. A schedule can override it with
`memoryLimit`; the workspace uses 2Gi because restic approached the old ceiling
while indexing its growing source and dependency trees during the October 17
recovery run. This changes backup-process headroom, not which files are protected.

Backups are **opt-out**, not opt-in (`skipWithoutAnnotation: false`). A new
service is protected the day it lands rather than the day someone remembers. The
cost is that anything enormous and re-acquirable must be excluded explicitly with
`k8up.io/backup=false`, declared by the chart that OWNS the volume:

| Volume | Why not |
|---|---|
| `games/romm-library` | ~78TB ROM corpus, read-only, re-acquirable. The first run spent nine hours reaching 1.9% of it. |
| `discord/smitele-bot-matchdata` | ~500Gi, re-derivable from the Hi-Rez API |
| `*/k8up-restic-repo` | the repository itself — without this it backs up into itself, nightly, per namespace |

`games/romm-saves` is deliberately **not** excluded: small, and save states exist
nowhere else.

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

## Talaria PostgreSQL recovery

The old missing-backup warning was stale: K8up has been capturing `.pgdump`
streams. However, the October 21 isolated restore drill found that snapshot
`8d30cf3962546d1ca9a85f91072d74cc4643be14340f4e17d44cb2b5aa13d037`
(October 19, 2.804 GiB) fails with `pg_restore: unexpected end of file`, despite
a successful Backup status. A readable archive table of contents is insufficient.
Do not rely on that snapshot for a full recovery.

`talaria-postgres-backup` now stages `pg_dump` on a dedicated scratch PVC and
runs `pg_restore --file=/dev/null` across the complete archive. Only a successful
init container permits Restic to publish `/dump/talaria-dump.pgdump`, tagged
`validated-postgres`. Dump/validation and snapshot errors fail the Job. The
weekly schedule runs Monday 07:00 UTC; existing CronJob/job monitoring covers it.
The repository password is mounted only in the snapshot container, and database
credentials only in the dump container. The pod has no Kubernetes API token.

Run an isolated restore with an explicit full snapshot ID:

```sh
python3 infra/k8up/recovery/talaria-postgres.py SNAPSHOT_ID --name postgres-drill-YYYYMMDD --apply
kubectl -n talaria logs -f job/postgres-drill-YYYYMMDD -c postgres
```

Use `--archive /talaria-dump.pgdump` only for the older streamed snapshots.
The drill mounts the repository read-only, has no network access or production
credentials, starts PostgreSQL on a Unix socket only, and requires `pg_restore
--exit-on-error` followed by table/index checks. It uses a new 400 GiB scratch
PVC and refuses to overwrite an existing drill. Allow up to six hours. Read-only
Restic uses `--no-lock`; schedule outside the Sunday 06:00 UTC repository prune.

After recording the result, delete only that drill's Job, NetworkPolicy and PVC.
Never substitute the production database PVC. The repository and primary database
are retained. Archive validation is not a substitute for periodically running
this full restore drill. NAS-hosted backups still share the NAS failure domain.

## Gotchas that cost time here

- **Cordoned PVC consumers can block backups.** K8up pins filesystem backup
  jobs to the node hosting the PVC consumer. Happy, Rachel, CrowdSec and kube-system hit the four-hour
  deadline after node1 was cordoned in October 2026. Their schedule entries now
  set a scoped `node.kubernetes.io/unschedulable:NoSchedule` toleration through
  a namespaced PodConfig, allowing backups while the node remains cordoned.
  PodConfig also carries the root security context and resource limits because
  it takes precedence over those fields on the Schedule. Remove the exception
  when these workloads no longer need backups on a cordoned node. Media and
  Talaria also have backed-up PVC consumers on node1, so their schedules carry
  the same exception to prevent the next weekly run from failing.

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

## Network isolation (2026-10-21)

This chart owns 22 policies: one for the operator, worker policies in all 17
managed backup/maintenance namespaces, and dump-client policies in docs, games,
discord and rachel. Operator/workers get only cluster DNS and the Kubernetes
API VIP/control-plane endpoints. Only Alloy collectors can enter the operator's
metrics port; worker and dump pods deny ingress. Dump clients get DNS and their
specific database port, with no API or public-network access.

The local restic repository is a node-mounted NFS volume; no pod-network NAS
exception is needed. [K8up runs backup commands through the Kubernetes API](https://docs.k8up.io/k8up/2.16/explanations/backup.html),
so denying direct ingress to dump pods does not prevent command execution.
New namespaces, control-plane addresses, database targets, remote repositories,
or stats/pushgateway destinations need corresponding policy updates. These
rules do not reduce backup filesystem access, repository credentials or RBAC.
The independently managed atomic Talaria backup and isolated restore drill
retain their own policies and are not selected by `k8upjob=true`.

Validation covered all 17 worker namespaces and all four dump clients. It
exposed a pre-existing Paperless PostgreSQL ingress rule that excluded K8up;
the Paperless chart now admits only the same-namespace dump label on 5432.
No Redis/parser access was added. A real, label-selected K8up Backup ran a
28-byte PreBackupPod command, saved it, and a separate read-only restic Job
verified the exact restored payload. The small snapshot is retained with tag
`network-policy-verification`; all temporary test resources were removed.
This smoke test does not replace full database-restore verification.

## Continuing the in-flight Talaria drill

The 2026-10-21 drill was launched before the final SQL quoting fix. Its running
Job cannot be edited. `recovery/verify-existing-restore.py JOB --name NAME`
prepares a validation-only Job only after the original has failed with evidence
that archive replay and ANALYZE succeeded and the legacy quoted SQL failed.
It refuses active Jobs, unrelated claims and restore failures. Add `--apply`
to create the network-denied Job; it mounts only the existing scratch database
and temporary storage, with no backup credentials, and never runs initdb or
replays the archive. It checks index/constraint validity, table and auction
counts, and database size before printing `RESTORE_VERIFIED`.

The drill completed on 2026-10-21 at 22:53 UTC. Snapshot
`e9eca426670ff22da2eb55a6feadd9de6bce60a4fa76289b7b8689369d593b9a`
was replayed with `pg_restore --exit-on-error`, followed by ANALYZE. The final
validation Job `talaria-restore-20261021-verified` completed and printed
`RESTORE_VERIFIED`: 50 public tables, 37,542,982 auction_items rows, 117 GB,
no invalid indexes and no unvalidated constraints. This proves that snapshot's
restore, not the validity of every historical or future backup.

The continuation needed two fixes: fsGroup setup on remount widened PGDATA's
permissions, so the owner now restores mode 0700 on the verified scratch path;
and Kubernetes collapsed paired dollar signs in the command before the shell
ran the SQL. The checks now use plain SQL assertions that fail with division
by zero if invalid objects exist, avoiding dollar quoting. Startup failures
also print the PostgreSQL log. Six guard/regression tests pass.

The scratch Jobs, network policies and 400 GiB claim are cleaned up after
recording the result. The restic snapshot and backup staging volume remain.
The other four logical database dumps have their own
[isolated restore drill](recovery/README.md) in `recovery/database.py`. Offsite cluster storage is an
accepted risk rather than a planned purchase; see the decision above.
