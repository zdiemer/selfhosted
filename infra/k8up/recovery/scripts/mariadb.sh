set -eu
# A syntactically valid prefix of a truncated SQL stream can import successfully.
# Require mariadb-dump's completion footer as well as a successful replay.
tail -n 5 "$ARCHIVE" | grep -q "^-- Dump completed on " || { echo INCOMPLETE_SQL_DUMP; exit 1; }
mkdir -p /work/data
mariadb-install-db --datadir=/work/data --auth-root-authentication-method=normal >/tmp/init.log 2>&1
# The dump contains mysql users/grants. Ignore them for this isolated drill;
# no production credential is needed, and there is no network listener.
mariadbd --datadir=/work/data --socket=/tmp/mysql.sock --pid-file=/tmp/mysql.pid --skip-networking --skip-grant-tables --skip-log-bin --innodb-buffer-pool-size=512M --innodb-flush-log-at-trx-commit=2 --event-scheduler=DISABLED --skip-slave-start --log-error=/tmp/mariadb.log &
pid=$!
trap 'kill "$pid"; wait "$pid" || true' EXIT
for i in $(seq 1 60); do mariadb-admin --socket=/tmp/mysql.sock ping >/dev/null 2>&1 && break; sleep 1; done
mariadb --socket=/tmp/mysql.sock --batch < "$ARCHIVE" >/tmp/restore.log 2>&1 || { echo RESTORE_FAILED; exit 1; }
# The SQL dump may FLUSH PRIVILEGES. Restart the isolated server to reapply
# skip-grant-tables without ever obtaining the production root password.
kill "$pid"
wait "$pid"
mariadbd --datadir=/work/data --socket=/tmp/mysql.sock --pid-file=/tmp/mysql.pid --skip-networking --skip-grant-tables --event-scheduler=DISABLED --skip-slave-start --log-error=/tmp/mariadb.log &
pid=$!
for i in $(seq 1 60); do mariadb-admin --socket=/tmp/mysql.sock ping >/dev/null 2>&1 && break; sleep 1; done
mariadb-check --socket=/tmp/mysql.sock --all-databases --check > /tmp/check.log 2>&1 || { echo TABLE_CHECK_FAILED; exit 1; }
# Exact row counts, not approximate information_schema.TABLE_ROWS estimates.
mariadb --socket=/tmp/mysql.sock --batch --skip-column-names <<'SQL' >/tmp/count.sql
SELECT CONCAT('SELECT COUNT(*) FROM `',REPLACE(TABLE_SCHEMA,'`','``'),'`.`',REPLACE(TABLE_NAME,'`','``'),'`;') FROM information_schema.TABLES WHERE TABLE_TYPE='BASE TABLE' AND TABLE_SCHEMA NOT IN ('mysql','sys','performance_schema','information_schema');
SQL
mariadb --socket=/tmp/mysql.sock --batch --skip-column-names < /tmp/count.sql > /tmp/counts
awk '{ rows += $1; tables++ } END { print "restored_tables=" tables " restored_rows=" rows; if(tables < 1 || rows < 1) exit 1 }' /tmp/counts
echo RESTORE_VERIFIED
