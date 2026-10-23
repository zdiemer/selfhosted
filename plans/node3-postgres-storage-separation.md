# Node3 PostgreSQL storage separation plan

Planned work: install a second physical NVMe SSD in `zachd-ubuntu-3` and move
Talaria PostgreSQL data and WAL onto it. Keep the OS, K3s, and etcd on the
existing SSD, preserving the database endpoint and node placement.

This separates database maintenance I/O from etcd. It does not provide database
high availability or protect against node loss. CPU, memory, power, and chassis
cooling remain shared.

## Hardware and sizing

The host is an HP EliteDesk 800 G4 Desktop Mini 35W (TAA), SKU `5PA44US#ABA`,
board `83E2`. Firmware reports two M.2 PCIe 3.0 ×4 SSD slots; one is occupied
by a Kioxia `KBG40ZNS512G`. Confirm the empty slot, mounting screw, thermal pad,
and clearance physically using the
[HP service guide](https://h10032.www1.hp.com/ctg/Manual/c06063157.pdf).

Select a compatible M.2 2280 NVMe drive after checking live database size,
maintenance headroom, endurance, and cooling. A 1 TB drive is the planning
candidate, not a purchased or validated device. Do not assume an aftermarket
heatsink fits. Identify disks by model, serial, and `/dev/disk/by-id`; mount by
filesystem UUID because device numbering can change.

Read the live `talaria/data-talaria-postgres-0` binding and PV path before copying.
Do not infer current usage from the claim's declared capacity. Confirm the source
PV uses `Retain` and inventory any tablespaces outside its data directory.

Moving to node6 would also require relocating GPU-dependent workloads or changing
the PostgreSQL memory budget. Keep this plan focused on a second disk on node3;
reassess scheduling and memory if considering another host.

## Execution sequence

### 1. Prepare and protect the current state

- Verify all three etcd members are healthy and the other two control-plane
  nodes can maintain quorum while node3 is powered off. Test an API endpoint
  independent of node3 and check agents' API connectivity.
- Inventory node3 workloads and local-volume dependencies; identify all service
  interruptions and maintenance jobs that must be paused.
- Create a fresh PostgreSQL backup, verify restoration in an isolated target,
  and record its location. Take an etcd snapshot and preserve it off node3 with
  the corresponding K3s server token through the existing secret-storage flow.
- Capture the live Helm values, StatefulSet, services, PVC/PV bindings, image
  digest, PostgreSQL version, configuration, ownership, and any tablespaces.
  The chart defaults and live storage configuration currently differ: do not
  deploy chart defaults as part of this work.
- Confirm the source PV remains `Retain`. Do not delete or repurpose it.

### 2. Install and validate the SSD

- Schedule the physical maintenance window. Cordon node3; quiesce Talaria's
  writers and stop PostgreSQL cleanly. Drain only workloads confirmed movable;
  do not force-delete local data or bypass disruption budgets indiscriminately.
- Shut down node3, install the SSD following HP's service procedure, then boot.
- Verify both drives by identity, SMART health, cooling, and cluster recovery.
  Resume the existing database on the original SSD while preparing migration.
- Partition and format **only the verified new drive**. Mount it by UUID at a
  dedicated path such as `/var/lib/talaria-postgres-ssd`.
- Run a bounded disposable-file test covering direct random I/O, synchronous
  writes, and sustained writes. Watch both SSD temperatures and etcd latency;
  stop on errors or thermal problems. Remove test files afterward.
- Verify mount persistence and fail-closed behavior. An absent SSD must never
  cause PostgreSQL to initialize a new empty database on the root filesystem.
  Use an explicit volume-identity/startup check, not directory existence alone.

### 3. Prepare the Kubernetes target

- Create a separate static local PV and PVC for the new mounted filesystem,
  with hostname affinity to node3 and reclaim policy `Retain`.
- Record usable capacity accurately and set capacity/temperature alerts. A
  local PV's advertised capacity is not a filesystem quota.
- Prepare the Helm/StatefulSet change to use the new claim explicitly. Account
  for immutable `volumeClaimTemplates`; do not patch the existing claim's
  storage class or assume a new size changes the backing device.
- Preserve the existing service name, credentials, PostgreSQL major version,
  and application connection settings. Keep this separate from memory tuning
  or a database-version upgrade.

### 4. Copy and cut over

Preferred approach: a rate-limited physical base backup into a temporary
standby on the new SSD, followed by WAL streaming. Use PostgreSQL's supported
[pg_basebackup workflow](https://www.postgresql.org/docs/15/app-pgbasebackup.html).

- Use restricted, temporary replication access. Bound WAL retention and monitor
  source free space; an abandoned replication slot must not fill the old disk.
- Budget memory for the temporary standby. It must not duplicate the primary
  memory allocation on node3; use a small standby configuration and check
  scheduling headroom before starting it.
- Start conservatively, for example at 30 MiB/s, and adjust only while etcd
  flush latency and temperatures remain healthy. Pause if API/lease or disk
  latency problems recur. Avoid overlapping backup, vacuum, or index jobs.
- Before cutover, pause API writes, all database-writing workers, scheduled
  jobs, and relevant autoscalers. Drain connections and confirm there are no
  remaining writers.
- Record the primary's final WAL position and verify the standby has replayed
  it. Stop and fence the original primary before promotion.
- Promote the new copy, deploy the final StatefulSet/claim binding, and ensure
  the application service selects exactly one writable primary. Remove
  temporary standby settings and replication resources when safe.
- First test connectivity and read-only queries, then a controlled write/read
  check, then resume writers in stages. Keep the original data untouched.

Alternative: a clean-shutdown physical copy if standby setup is unsuitable.
The database must remain stopped for the consistent final copy; a live file
copy alone is not a valid backup. Follow PostgreSQL's
[filesystem backup requirements](https://www.postgresql.org/docs/15/backup-file.html).

## Downtime and success criteria

There are two potential interruptions: the physical SSD installation and the
database cutover. Hardware downtime depends on hands-on access and boot checks.
Estimate seed time from the current data size and a measured safe copy rate;
applications can remain online during standby seeding. Target a
10–20-minute cutover window, but refine it after measuring replay and startup.
A shutdown-copy approach requires a substantially longer database outage.

Before declaring success:

- PostgreSQL data and WAL resolve to the new physical SSD; etcd remains on the
  original SSD. Application reads, writes, workers, and scheduled tasks succeed.
- All nodes and relevant deployments are healthy; no new K3s exits, lease losses,
  disk errors, or OOMs occur.
- Representative database work runs without the previous multi-second etcd
  flush stalls. Compare etcd latency, query latency, and both SSD temperatures
  with the baseline; do not deliberately reproduce an unbounded saturation test.
- A new backup from the migrated database succeeds and an isolated restore is
  checked. Verify backup discovery/annotations follow the new claim.
- Helm values and repository documentation match the live placement and storage.

Observe at least one normal backup/maintenance cycle before retiring the old
copy. Storage separation follows etcd's
[hardware guidance](https://etcd.io/docs/v3.5/op-guide/hardware/), but shared
chassis thermals still need monitoring.

## Rollback

Before accepting writes on the new primary, stop/fence it, restore the original
claim/service binding, and restart the original database. Never expose both
copies as writable primaries.

Once the new primary has accepted writes—including validation writes—the old
copy is stale. Do not simply switch back. Keep writers paused and repair forward,
or synchronize/reseed the original from the new primary using an appropriate
PostgreSQL recovery procedure before switching. Preserve WAL and backups needed
for recovery. Retire the original copy only after validation and explicit cleanup.

## Outstanding decisions

- Purchased SSD: GUDGA 1 TB M.2 NVMe PCIe 3.0, P/N `GVY-1TB-2280`.
  Awaiting installation; owner available on 2026-10-22. Verify physical fit,
  serial number, health, actual capacity, and thermal hardware after installation.
- Proposed mount `/var/lib/talaria-postgres-ssd`, PV `talaria-postgres-ssd-node3`,
  PVC `talaria/talaria-postgres-ssd`; finalize after measuring usable space.
- Temporary standby memory budget, replication access, and WAL retention bound.
- Confirmed downtime window and measured copy rate.

## Execution checkpoint — 2026-10-22

Preparation is in progress. Node3 remains online and the database still uses the
original SSD. No storage migration, cordon, drain, or shutdown has occurred.

Live inventory:

- All ten Kubernetes nodes are Ready. All three control-plane members' local
  etcd health checks pass. API readiness checks pass directly at the two
  surviving addresses, `192.168.4.26:6443` and `192.168.4.21:6443`.
- Source claim `talaria/data-talaria-postgres-0` binds to
  `pvc-95514cfa-26e7-46ab-910b-bc7263dcd983`, a `Retain` local PV at
  `/var/lib/rancher/k3s/storage/pvc-95514cfa-26e7-46ab-910b-bc7263dcd983_talaria_pgdata-migrate3`.
  The claim declares 200 GiB; the StatefulSet template still declares 100 GiB.
- `auction_watcher` uses 200,990,915,943 bytes (about 187.2 GiB). There are no
  external tablespaces; WAL resolves within `/var/lib/postgresql/data`.
- PostgreSQL is 15.19, image digest
  `sha256:f7d23353e1b15400d22ebe31189f4d314b87a4c129cc400c8c2d8d4ca127bf81`.
  Preserve the live settings, including 18 GB shared buffers and 20/28 GiB
  requested/limited memory. The chart defaults differ substantially.
- Original disk: Kioxia `KBG40ZNS512G`, serial `511PG3R1QXD3`, root filesystem
  UUID `0f605f83-546e-4ff7-9860-bd2249ce3506`. PostgreSQL and etcd both resolve
  to this disk. SMART passes with zero media/data-integrity errors, but the
  measured temperature is 71°C and thermal throttling has occurred historically.
  Recheck cooling before adding sustained migration I/O.
- Drain preflight identifies PostgreSQL as pinned to node3. Authelia Redis,
  Paperless PostgreSQL, ntfy, Jellyseerr, and virt-exportproxy are singleton
  workloads that can experience gaps; three have iSCSI volume handoffs.

Fresh backup Job `talaria-postgres-pre-ssd-20261022` was created from the existing
`talaria-postgres-backup` CronJob. It is still running at this checkpoint. Require
successful completion, record the resulting full Restic snapshot ID, and perform
the isolated restore before proceeding. The previous backup took 43 minutes;
restore time is additional. Do not overlap a migration seed with this backup.
An active workstation process waits for this exact Job to complete, records its
full snapshot ID, then invokes the existing isolated restore workflow as
`talaria-postgres-pre-ssd-restore-20261022`. This creates a separate 400 GiB NAS
scratch claim, with no production data-volume mount. It requires Job completion
and `RESTORE_VERIFIED`, and records success privately. A failure stops preparation;
it never shuts down node3 or performs a cutover. Progress is in
`secrets/node3-postgres-20261022/preparation.log` (Git-ignored).

The Talaria submodule has an **undeployed** chart change adding
`postgres.existingClaim` and `postgres.expectedVolumeId`. Existing defaults retain
their current claim-template behavior. Setting the identity requires an explicit
claim; every container start checks the volume-root `.talaria-volume-id`, existing
`PG_VERSION` and `global/pg_control`, and matching PostgreSQL major version before
calling the normal image entrypoint. Lint, both rendered claim modes, invalid
configuration rejection, and startup failure cases pass with synthetic values.

At cutover, populate the marker only on the verified mounted SSD, using its
filesystem UUID as the expected value. Never create the marker on the underlying
root-filesystem directory. Also verify the host mount's UUID and missing-mount
behavior; the marker does not replace that check. Copy data into the volume's
`pgdata/` subdirectory to preserve the existing chart mount layout. Recreate the
stopped StatefulSet to remove its immutable claim template, then deploy using
captured live values, the explicit claim, and the expected identity. Keep the
original PV/PVC intact. Do not run the normal whole-application upgrade script
with chart defaults for this cutover.

Live Helm values/manifests and workload bindings are captured privately in
`secrets/node3-postgres-20261022/`. After explicit owner approval, a fresh etcd
snapshot and matching server token/configuration were copied off node3 into this
directory with owner-only access. The archive checksum and required contents were
verified. Do not commit or disclose these credentials. The private migration
override also corrects saved Helm resource values to the actual live 20/28 GiB
memory and 1/3 CPU request/limit, and pins the running image digest. Its expected
volume identity is deliberately a placeholder until the new filesystem exists.

All seven agents can establish TCP connections to both surviving API servers.
Six bootstrap through node0's LAN or Tailscale address. Node4 bootstraps through
node3, but its saved K3s agent load-balancer configuration includes both surviving
servers (`192.168.4.26:6443`, `192.168.4.21:6443`). Avoid restarting agents during
the window; verify continued API connectivity once node3 is offline.

Remaining pre-shutdown gates: finish the backup and isolated restore, recheck
health, then pause writers and movable dependencies before cleanly stopping
PostgreSQL and shutting down node3. The physical installation remains outstanding.
