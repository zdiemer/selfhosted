# Talaria PostgreSQL storage on node3

Talaria PostgreSQL data and WAL moved onto node3's second SSD on 2026-10-22.
The OS, K3s, and etcd remain on the original Kioxia. This directory owns the
dedicated static local PV/PVC and focused Helm overrides. The endpoint, node,
PostgreSQL 15.19 image, credentials, and 20/28 GiB memory request/limit are preserved.
Node3 remains one PostgreSQL failure domain; CPU, RAM, power, and chassis cooling
are shared.

The host is an HP EliteDesk 800 G4 Desktop Mini 35W (TAA), SKU `5PA44US#ABA`,
board `83E2`.

The original claim is retained as a **stale** recovery copy. It must never be
started as a replacement primary without synchronization or recovery. Applying
the storage manifest alone does not switch a database.

| Component | Identity |
|---|---|
| Host | `zachd-ubuntu-3` |
| OS, K3s, etcd disk | Kioxia `KBG40ZNS512G`, serial `511PG3R1QXD3` |
| Root filesystem | `0f605f83-546e-4ff7-9860-bd2249ce3506` |
| PostgreSQL disk | GUDGA `GVY-1TB-2280`, serial `9P51118008271` |
| Database filesystem | ext4, UUID `d072873b-9827-4091-9b3a-c741cd29a24a` |
| Host mount | `/var/lib/talaria-postgres-ssd` |
| PV local path | `/var/lib/talaria-postgres-ssd/postgres` |
| PV / PVC | `talaria-postgres-ssd-node3` / `talaria/talaria-postgres-ssd` |
| Advertised capacity | 937 GiB, no filesystem quota |
| Application endpoint | `talaria-postgres:5432`, unchanged |

The retained original claim is `talaria/data-talaria-postgres-0`, bound to
`pvc-95514cfa-26e7-46ab-910b-bc7263dcd983` with `Retain` reclaim policy. Its old data
directory is
`/var/lib/rancher/k3s/storage/pvc-95514cfa-26e7-46ab-910b-bc7263dcd983_talaria_pgdata-migrate3/pgdata`
on the Kioxia. It is not mounted by the production StatefulSet.

Identify devices by serial and `/dev/disk/by-id`, never `nvme0n1` numbering. After
installation the original disk became `nvme1n1`; numbering can change again.
The new disk was erased only after the owner explicitly approved its serial.

The host's `/etc/fstab` entry is:

```fstab
UUID=d072873b-9827-4091-9b3a-c741cd29a24a /var/lib/talaria-postgres-ssd ext4 defaults,noatime,nofail,x-systemd.device-timeout=10s 0 2
```

`nofail` lets etcd and K3s boot if the database SSD is absent. The underlying root
mountpoint deliberately has no `postgres/` subdirectory. Create that subdirectory
and its `.talaria-volume-id` marker only after verifying the mounted UUID; never
recreate them on the root fallback. The marker contains the filesystem UUID.
`pgdata/` inside that directory holds the cluster and WAL.

The Talaria chart supports an explicit existing claim and an expected volume
identity. Every PostgreSQL container start checks the marker, `PG_VERSION`,
`global/pg_control`, and the binary's major version before invoking the normal
entrypoint. Missing/wrong disks or empty clusters must fail without initializing
a replacement database. Its `values-external.yaml` holds the deployment binding.
The focused
[`talaria-values.yaml`](talaria-values.yaml) override preserves the measured live
PostgreSQL configuration and can be combined with `--reuse-values`; it contains
no credentials.
Switching from `volumeClaimTemplates` requires stopping/recreating the StatefulSet;
do not mutate the original claim or remove the original retained PV.

Install the storage objects with:

```sh
kubectl apply -f infra/postgres-storage/node3-talaria.yaml
```

Backups continue through `talaria/talaria-postgres-backup`, which makes an atomic
logical dump through the service. Both source and target raw PostgreSQL claims
have `k8up.io/backup: "false"`; copying a live data directory is not a database
backup. Restore-drill scratch claims are also excluded from raw backup discovery.

Post-migration backup Job `talaria-postgres-post-ssd-20261022` completed in 41
minutes, validated every archive payload, and created Restic snapshot
`d0565a4a5b23a4a2ea191ae4959f3dff93d601b7be46159e02efd39c44864660`.
Its isolated restore, `talaria-postgres-post-ssd-local-20261022`, passed on node6
on 2026-10-23: 50 tables, 38,233,165 auction items, 123 GiB, valid indexes and
constraints. Bulk-load settings were confined to scratch storage; PostgreSQL was
stopped, `initdb --sync-only` flushed the files, and it restarted with `fsync=on`
and `synchronous_commit=on` before verification and CHECKPOINT. Both
`RESTORE_VERIFIED` and `DURABILITY_VERIFIED` were recorded with Job completion.
The original schedule flags were restored and only owned restore-drill Jobs,
scratch claims, and isolation policies were removed. Restic snapshots and both
production claims remain retained. Recovery evidence is owner-private under the
Git-ignored `secrets/node3-postgres-20261022/` directory.

An etcd snapshot containing the SSD binding was saved on 2026-10-23 and copied
off node3 with the matching server token and K3s configuration. The approved
private archive is `node3-etcd-post-ssd-20261023.tgz` in that same directory;
its checksum and required contents were verified and its mode is 0600. The
temporary read-only host-access pod was removed. An isolated etcd restore test
remains separate work.

Node-exporter discovers this mount and both NVMe temperature sensors. Grafana
rules `sh-postgres-ssd-disk` and `sh-node-nvme-temperature` alert above 80% capacity
for ten minutes and 75°C for five minutes respectively. Temperature rules group
by node/chip, since NVMe numbering can change. Migration monitoring additionally
checked etcd health, latency, source free space, and temperatures. Both drives had
zero media errors throughout seeding and cutover.

In sampled five-minute windows, etcd WAL fsync averaged 4.35 ms during the
original-disk seed and 2.33 ms during the SSD primary's logical backup. Their
99th-percentile histogram upper bounds were 128 ms and 32 ms respectively;
backend-commit bounds were 256 ms and 64 ms. Neither window contained a sample
above 1.024 seconds. These are observations under the seeding and backup
workloads, respectively. Node3's etcd process continued running through cutover.

Retain the original PV/PVC and data through validation and at least one normal
backup/maintenance cycle. Once the new primary accepts any writes, the old copy
is stale: returning to it requires synchronization or recovery, never a direct
claim flip. Retirement requires explicit cleanup approval.

Online seeding used a temporary admission policy and binding,
`node3-postgres-maintenance`, after concurrent Helm upgrades interrupted a seed.
It fenced source StatefulSet spec changes/deletion and source-pod deletion while
allowing other workloads and metadata updates. It was removed during the controlled
cutover, after writers drained, pod creation was frozen, and final WAL position
`9B1/124067E0` replayed. The original primary was stopped before promotion; the
service then selected exactly one writable SSD primary. Temporary quotas,
replication access, and migration-only PostgreSQL settings were removed.

If recreating this fence for future maintenance, stop the seed deliberately before
removing it on an aborted migration:

```sh
kubectl delete validatingadmissionpolicybinding node3-postgres-maintenance
kubectl delete validatingadmissionpolicy node3-postgres-maintenance
```

The owner directed continuing despite temperature. Seeding used a small standby
(768 MiB request, 2 GiB limit, 256 MiB shared buffers), a pre-reserved replication
slot bounded at 32 GiB, and a 60 MiB/s copy rate after a healthy sustained
30 MiB/s baseline. Disk faults, source free space, and etcd health remained guarded.
These were temporary migration settings; the final primary retains its original
configuration, including 18 GiB shared buffers and asynchronous commit.
