# Distributed node storage

Status: inventory and shortlist; no provider selected or deployment authorized.

## Requirements

Replace all local-path use with portable storage using existing node disks of
unequal sizes. Preserve NAS-backed storage and keep the NAS out of this pool.
Use both spare OS filesystem capacity and dedicated disks where feasible. Avoid
losing half the aggregate capacity to replication: investigate parity, with
separate workload policies permitted. Prefer fast, unconstrained storage at
allocation and retain database locality where possible. Automatic relocation is
a bonus, not a prerequisite. No new hardware is in scope. Planned downtime and offline repartitioning are
acceptable for the eventual migration. This is a design constraint, not an
instruction to repartition nodes during planning.

## Measured inventory

Read-only SSH, lsblk, df, memory and network sysfs inspection on 2026-10-23.
All sizes below are GiB; available RAM is a momentary observation, not a safe
reservation. Names omit `zachd-ubuntu` (base means that node itself).

| Node | Physical local disks | Filesystem available | RAM available | Active link |
|---|---|---:|---:|---|
| base | 238.5 SATA Micron 1100 | 122.4 | 6.6 | 1 GbE |
| 1 | 111.8 SATA WDC + 232.9 SATA PNY | 53.2 root + 125.6 local-path | 23.5 | 1 GbE |
| 2 | 113.0 Apple NVMe | 40.0 | 14.8 | 1 GbE |
| 3 | 476.9 KIOXIA NVMe | 223.5 | 9.0 | 1 GbE |
| 4 | 931.5 Crucial NVMe | 452.3 | 16.3 | 1 GbE |
| 5 | 953.9 Kingston NVMe | 396.3 | 36.5 | 1 GbE |
| 6 | 953.9 GVY NVMe | 621.8 | 23.8 | 1 GbE |
| laptop | 119.2 Samsung SATA | 22.0 | 6.1 | 1 GbE USB adapter |
| laptop-2 | 238.5 Samsung mSATA | 94.4 | 5.9 | 1 GbE; Wi-Fi also up |
| laptop-6 | 119.2 KIOXIA NVMe | 33.0 | 6.3 | Wi-Fi only |

All nine active wired links negotiate at 1 Gb/s; none currently at 2.5 Gb/s.
This is not an end-to-end throughput measurement. At 1 Gb/s the theoretical
ceiling is 125 MB/s before overhead and competing application/recovery traffic.
No write benchmarks, SMART health qualification or sustained load profiling
were performed. Disk model alone cannot establish the fastest usable tier.

The OS filesystems are ext4 and consume essentially their disks. Node1's second
SSD is already formatted and in use for local-path. Free filesystem bytes are
not raw devices available to Ceph. Remote iSCSI devices, loop devices and zram
are excluded. The separate planned node3 SSD is not installed in this inventory;
coordinate with its existing storage-separation plan before changing layout.

Approximately 2.23 TiB is currently free across these filesystems, before OS,
application-growth, migration and rebuild reserves. It is **not** a usable-pool
capacity promise. Most spare capacity is on nodes4–6, which matters when every
stripe must span distinct hosts: smaller hosts can constrain parity layouts
well before aggregate free bytes are exhausted.

Proposed qualification order: wired nodes1–6 first; evaluate smaller wired
nodes for useful capacity versus overhead. Laptop-6 should remain a client
until its crash investigation and wired connectivity are resolved. All nodes
can consume PVCs even if some do not host redundancy fragments. This proposed
role split still needs agreement.

## Local-path migration scope

Inventory contains 29 PVs: 26 Bound and 3 Released. Allocated directory usage
was measured with bounded, low-priority du; claim sizes are not enforced disk
quotas and sparse VM files have different logical and allocated sizes.

| Data | Node | Allocated usage | Qualification |
|---|---|---:|---|
| Talaria PostgreSQL directory, Bound PV | 3 | 196.29 GiB | No matching pod reference in snapshot; verify current database ownership/mount before migration |
| Old PostgreSQL migration directory, Released PV | 1 | 76.45 GiB | Retained data; not approved for deletion |
| PostgreSQL pre-SSD local staging, Bound PV | 6 | 142.07 GiB | Referenced by completed validation job; verify recovery dependencies |
| Elasticsearch | 1 | 23.76 GiB | Claim is 20 GiB; actual usage exceeds requested size |
| BuildKit userns cache | 4 | 68.93 GiB | Active cache |
| Old BuildKit cache | 4 | Unknown | du exceeded 12-second bound; no pod reference in snapshot |
| Steam home / games | 6 | 2.34 GiB / near zero | Requests are 50 / 400 GiB, not actual allocation |
| 17 vmlab volumes | 5 | About 3.6 GiB combined | Sparse allocation; budget future guest writes |
| Authelia / Traefik | 2 / 1 | Small | Check recovery/bootstrap dependencies |
| Two Released CrowdSec volumes | laptop | Paths absent | Kubernetes records are not occupied disk capacity |

The two old/staging PostgreSQL copies total about 218.5 GiB. Do not include
that space in a capacity model until the recovery/migration owner confirms
retention requirements. No data or PVs were deleted during inventory.

History (`bd83ce5`, `730418e`) records earlier Longhorn/LINSTOR rejection for
small-node memory overhead and mixed-kernel DRBD concerns. It does not establish
the exact uniform-reservation setting behind the owner's Longhorn experience.
It also records intentional NAS-independent Authelia/Traefik storage and local
disposable BuildKit caches. Preserve those bootstrap and churn considerations.

## Shortlist

### 1. Ceph through Rook: block-storage reference candidate

Ceph RBD can use an erasure-coded data pool with overwrite support, plus a
replicated metadata pool. It fits writable PVCs and parity better than archival
parity schemes. Rook requires raw devices, raw partitions, unformatted LVs or
block-mode PVs: existing ext4 free space is not directly eligible. A safe pilot
therefore depends on an approved disk evacuation/repartition plan.

BlueStore defaults to a 4 GiB memory target per OSD, plus daemon and recovery
headroom. This is material on shared application nodes. Capacity weighting and
device classes are useful; they do not promise routing every write to whichever
disk is momentarily least busy. Benchmark database fsync latency, CPU/memory,
small-write amplification and recovery contention on the actual 1 GbE fabric.

Sources: [EC and overwrites](https://docs.ceph.com/en/squid/rados/operations/erasure-code/),
[Rook storage prerequisites](https://www.rook.io/docs/rook/latest/Getting-Started/Prerequisites/prerequisites/),
[Ceph memory sizing](https://docs.ceph.com/en/latest/start/hardware-cpu-memory/).
Pin a release and recheck requirements before a pilot; latest docs can describe
development releases.

### 2. MooseFS: existing-filesystem candidate, not a complete DB answer

Investigate directory-backed chunk storage to avoid repartitioning. Its parity
scheme applies to archived data; hot writes cannot simply be budgeted at the
archival parity ratio. Validate write/modify transitions, temporary full copies,
POSIX/fsync behavior and Kubernetes CSI lifecycle before considering databases.

EC4+1 stores 1.25 bytes per logical byte and needs six chunkservers to start
conversion, five to retain converted data. Community has a single active master
with metaloggers, not automatic master failover. Pro Personal advertises HA and
free use up to 20 TiB raw under a noncommercial license; eligibility for all
hosted workloads remains unresolved. Neither licensing nor metadata availability
should be assumed away.

Sources: [EC behavior and host counts](https://moosefs.com/blog/ec4-the-erasure-coding-format-we-added-in-2020),
[edition and HA differences](https://moosefs.com/pro-vs-community).

### 3. Vitastor: conditional block-storage challenger

Supports erasure coding, distributed block storage and Kubernetes integration.
Worth evaluating against Ceph if its current storage-device requirements,
operator lifecycle and recovery behavior fit this cluster. Do not apply its
published fast-network benchmarks to these nodes. Its VNPL licensing page says
closed-source services require commercial support: clarify workload eligibility
and cost before investing in a pilot. It is not an assumed free replacement.

Source: [Vitastor features, integration and licensing](https://vitastor.io/en/).

No candidate currently satisfies every requirement without qualification. Do
not select a provider on the basis of aggregate free capacity alone. Pure
replication does not satisfy the general capacity requirement; an archival
parity layer does not establish safe live database failover.

## Design gates and next steps

1. Planned downtime and offline repartitioning are accepted. Prepare a per-node
   evacuation, verified backup/restore and rollback procedure before assigning
   raw partitions to a provider. Ext4 shrinking requires the filesystem to be
   unmounted; root partitions therefore need a tested rescue/alternate-boot
   path. Preserve boot and OS capacity, and migrate one failure domain at a
   time. Do not use production loopback files as a substitute for this layout.
2. Decide whether a bounded replicated tier for hot databases is acceptable while
   bulk data uses parity. Otherwise the winning provider must pass direct EC
   database benchmarks and recovery tests. Small replicated metadata still needs
   a budget even when application data is parity protected.
3. Select host failure tolerance and minimum free headroom. For illustration,
   4+1 gives 80% coding efficiency and 4+2 gives 66.7%, before metadata, reserves,
   unequal-host placement limits and migration overlap. Parity fragments should
   be distributed across hosts, not placed on designated single parity disks.
4. Produce a per-host allocation model with hard OS reserves, future workload
   growth, one-host-loss rebuild capacity and actual failure-domain placement.
   Choose a repair bandwidth budget and acceptable application downtime.
5. Qualify disks and network without production write stress; obtain a baseline
   of existing DB latency and node pressure before a disposable-volume pilot.
6. Pilot provisioning, expansion, snapshots/restore, rescheduling, host loss,
   fencing against dual writers, degraded writes, rebuild and full-disk handling.
   Measure application latency during recovery, not only healthy throughput.
7. Migrate disposable workloads first, then ordinary files and VM disks, with
   databases last. Keep verified backups and a rollback copy per migration.
   Audit hostPath consumers as well as PVCs before declaring local-path retired.

No storage software, repartitioning, cleanup or workload migration is part of
this inventory/shortlist step. Existing NAS classes remain unchanged.
