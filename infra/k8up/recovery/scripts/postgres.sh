#!/bin/sh
set -eu
export PGDATA=/work/pgdata PGHOST=/tmp PGUSER=postgres
initdb -U postgres --auth-local=trust --auth-host=reject >/tmp/init.log
pg_ctl -l /tmp/postgres.log -o "-c listen_addresses='' -c unix_socket_directories=/tmp" start
trap 'pg_ctl -m fast stop >/dev/null' EXIT
createdb recovery
pg_restore --list "$ARCHIVE" >/tmp/archive.list
# Do not print dump contents or potentially sensitive failing SQL into Job logs.
pg_restore --exit-on-error --no-owner --no-acl -d recovery "$ARCHIVE" >/tmp/restore.log 2>&1 || { echo RESTORE_FAILED; exit 1; }
psql -X -v ON_ERROR_STOP=1 -d recovery <<'SQL'
ANALYZE;
SELECT 1 / (count(*) > 0)::int AS tables_present FROM pg_tables WHERE schemaname='public';
SELECT 1 / (count(*) = 0)::int AS indexes_valid FROM pg_index WHERE NOT indisvalid;
SELECT 1 / (count(*) = 0)::int AS constraints_valid FROM pg_constraint WHERE NOT convalidated;
CREATE TEMP TABLE recovery_counts (rows bigint);
SELECT format('INSERT INTO recovery_counts SELECT count(*) FROM %I.%I;', schemaname, tablename) FROM pg_tables WHERE schemaname='public'
\gexec
SELECT count(*) AS restored_tables, sum(rows) AS restored_rows FROM recovery_counts;
SELECT 1 / (sum(rows) > 0)::int AS rows_present FROM recovery_counts;
SELECT pg_database_size('recovery') AS restored_bytes;
SQL
echo RESTORE_VERIFIED
