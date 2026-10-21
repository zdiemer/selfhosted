#!/usr/bin/env bash
# Disk guard for the screenshot cache, run ON the droplet by cron (see
# provision.sh).
#
# This used to be the tail of backup.sh, which is gone: DigitalOcean's own
# droplet backups took over the database side (see DEPLOY.md), and the two
# jobs never belonged together anyway. Backing up is about not losing data;
# this is about not filling a 25 GB disk, it is IO-heavy in its own right,
# and it wants a cadence of its own.
set -euo pipefail

DATA=/opt/whatnowgg/data
SHOTS=$DATA/art/shot
CEILING_MB=6144

# The screenshot cache is the one art tier that can outgrow the disk: covers
# and headers are small and bounded by the catalog, but shots are lazily
# fetched 1080p files with no ceiling of their own. Evicting is free in the
# only sense that matters here, since everything refetches from Steam's CDN
# the next time somebody opens the game.
[ -d "$SHOTS" ] || exit 0

while [ "$(du -sm "$SHOTS" | cut -f1)" -gt "$CEILING_MB" ]; do
  # Consume the whole sorted stream: head can SIGPIPE sort under pipefail,
  # aborting the guard after its first batch while still over the ceiling.
  find "$SHOTS" -type f -printf "%T@ %p\n" | sort -n | sed -n '1,200p' \
    | cut -d" " -f2- | xargs -r rm --
done
