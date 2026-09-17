#!/bin/sh
# Bring up the 2016 pybank: fresh database, seed, serve.
#
# /bin/sh is fine here — unlike the terminal image there are no ulimits to set,
# and python:2.7-slim's dash does everything below.

set -eu

DB_DIR="${PYBANK_DB_DIR:-/data}"
export DJANGO_SETTINGS_MODULE=settings_shim

cd /opt/pybank

# A fresh database into the emptyDir on every start. Nothing here is worth
# persisting — it is a 2016 class project with cleartext PINs and whole-dollar
# integer balances — and a demo bank that resets to a known state every restart
# is strictly easier to reason about than one that accumulates whatever visitors
# did to it. It also means the PVC question never comes up.
rm -f "${DB_DIR}/db.sqlite3" "${DB_DIR}/db.sqlite3-journal"

# Four migrations, and they tell a story worth reading once: 0001 creates the
# Question and Choice models straight out of the Django tutorial's polls app,
# 0002 creates Account and DELETES both of them, 0003 replaces
# checking_balance/saving_balance with a single balance plus an account_type
# discriminator, and 0004 changes a column default. Both 0002 and 0004 bake a
# randomly generated card number in as that default, because
# Account.generate_card() was called at makemigrations time and Django froze
# whatever it returned.
echo "==> migrate"
python manage.py migrate --noinput

# Run directly, NOT `manage.py shell < seed.py`. Django 1.9's shell feeds piped
# input to the interactive console, which echoes each line and cannot handle a
# multi-line def — it prints the script back at you, seeds nothing, and exits 0.
echo "==> seed"
python /opt/pybank-shim/seed.py

# runserver, not gunicorn, and the reasons are specific rather than lazy:
#
#   * DEBUG is on by design (see settings_shim.py), and runserver's
#     StaticFilesHandler is the ONLY thing serving /static/ here. gunicorn
#     serves no static files at all, so it would need whitenoise or an nginx
#     sidecar — new machinery for a page that gets a handful of visits.
#   * gunicorn 19.10 is the last release supporting Python 2.7 and is itself
#     long EOL, so it would add an unmaintained dependency rather than remove
#     one.
#   * `--insecure` is what you would need to serve static with DEBUG=False. It
#     is not needed here; noted so nobody adds it out of habit.
#
# --noreload matters: the autoreloader forks a second process that stat()s the
# whole tree forever, which on a read-only rootfs can never see a change, and
# which makes SIGTERM handling ambiguous inside a 15-second grace period.
echo "==> runserver"
exec python manage.py runserver --noreload 0.0.0.0:8000
