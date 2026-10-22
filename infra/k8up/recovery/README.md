# Isolated database restore drills

`database.py` restores stored Restic archives for Paperless, Rachel's site, RomM
and Vōcard. Talaria uses the separate `talaria-postgres.py` drill, whose complete
restore was verified on 2026-10-22. Bitmagnet is intentionally not backed up: its
PostgreSQL data is a rebuildable public-DHT index.

Each run requires a **full snapshot ID** and a new `db-restore-…` name. The default
prints manifests; `--apply` creates a fresh scratch PVC, a deny-all NetworkPolicy,
and a deadline-limited Job. It refuses existing PVC/Job/policy names. It never
mounts a production database volume or connects to a production database.

```sh
python3 infra/k8up/recovery/database.py paperless FULL_SNAPSHOT_ID \
  --name db-restore-paperless-YYYYMMDD
# Review the rendered resources, then add --apply.
kubectl -n docs logs job/db-restore-paperless-YYYYMMDD -c restore-archive
kubectl -n docs logs job/db-restore-paperless-YYYYMMDD -c database
```

The targets select fixed namespaces, archive paths and matching engine majors:

| Target | Namespace | Engine | Archive |
|---|---|---|---|
| `paperless` | docs | PostgreSQL 16 | `/docs-dump.pgdump` |
| `rachel` | rachel | PostgreSQL 17 | `/rachel-dump.pgdump` |
| `romm` | games | MariaDB 11.4 | `/games-dump.sql` |
| `vocard` | discord | MongoDB 8 | `/discord-dump.archive.gz` |

The Restic init container has a **read-only** repository mount, uses `--no-lock`,
and holds the repository password only in a Secret volume. The non-root database
container mounts only scratch storage and `/tmp`; it receives no backup or
production credentials. There is no Service, service-account token, host mount,
or external network access. PostgreSQL/MariaDB use only Unix sockets; MongoDB
binds only pod loopback. Errors that might quote private SQL are kept inside the
scratch pod instead of printed into cluster logs. Only counts and validation
results are emitted. A failed drill is retained for scoped inspection.

PostgreSQL requires a complete error-free `pg_restore`, ANALYZE, valid indexes
and constraints, and nonempty public tables. Ownership/ACLs are deliberately
omitted in the scratch database; this does not test production roles or grants.
MariaDB requires the SQL completion footer (a valid truncated SQL prefix could
otherwise succeed), successful import, `mariadb-check`, and exact table row
counts. The import uses a 512MiB buffer pool and relaxed per-transaction fsync in
the **scratch** server; it is cleanly stopped and restarted before validation.
Its restored users/grants are ignored with `--skip-grant-tables` and networking
is disabled, so no production root credential is required. Events and replication
autostart are disabled. MongoDB requires `mongorestore --stopOnError`, full
validation of every restored collection, and nonempty application data.

Require both Job completion and `RESTORE_VERIFIED`. Preserve the aggregate
results and snapshot IDs, then remove **only that drill's** Job, wait for its pods
to disappear, and remove its scratch PVC and NetworkPolicy. Never substitute a
production claim or delete the shared `k8up-restic-repo` claim. The retained
repository snapshot is the source of truth; deleting scratch storage is not
permission to prune backups.

```sh
kubectl -n docs delete job db-restore-paperless-YYYYMMDD --wait=true
kubectl -n docs wait pod -l recovery-drill=db-restore-paperless-YYYYMMDD --for=delete --timeout=60s
kubectl -n docs delete pvc,networkpolicy db-restore-paperless-YYYYMMDD
python3 -m unittest discover -s infra/k8up/recovery/tests -v
```

These drills prove the selected logical dumps can be replayed and queried.
They do not prove all historical/future snapshots, uploaded files/media, SQLite
volume copies, application startup/authentication, point-in-time recovery or
recovery after losing the NAS. Repeat after backup or database-major changes.
Offsite cluster storage is explicitly deferred at the owner's request due to
cost; droplet backups do not cover this data.

## Verified 2026-10-22

These were the newest stored logical-dump snapshots at the start of the drill.
Full snapshot IDs and timestamps are in [verified-20261022.json](verified-20261022.json).

| Database | Snapshot (UTC) | Restored tables/collections | Exact restored rows/documents |
|---|---|---:|---:|
| Paperless | October 19 | 72 tables | 2,520 rows |
| Rachel | October 18 | 13 tables | 91 rows |
| RomM | October 20 | 40 application tables | 4,209,379 rows |
| Vōcard | October 16 | 2 application collections | 3 documents |

All four stored dumps completed a full replay and passed their engine-specific
checks. The 1.366GiB RomM SQL dump also had its completion footer verified.
Talaria's earlier restore recovered 37,542,982 auction items across a 50-table
database; see the parent runbook for that separate drill.

The initial repository-inspection pod exceeded its 1GiB limit when four Restic
index readers ran together. It was replaced; subsequent readers were bounded
with `GOMEMLIMIT=512MiB` and a 2GiB container limit. The repository remained
read-only throughout. This was an inspection resource issue, not dump corruption.

RomM was then started in a second Job from the persisted scratch database,
with no archive/repository/password mounts. Table checks and the exact row
counts passed again after the clean restart. All drill Jobs, policies and scratch
claims were deleted afterwards; the four production database pods remained Ready.
Nine recovery-tool tests passed. No backup snapshots were pruned or rewritten.
