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

- SSD model, capacity, thermal hardware, and physical maintenance window.
- Exact mount, PV/PVC names, and Helm support for an explicitly selected claim.
- Temporary standby memory budget, replication access, and WAL retention bound.
- Confirmed downtime window and measured copy rate.
