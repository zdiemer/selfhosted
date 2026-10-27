#!/usr/bin/env bash
# Dedicated, persistent 1 GiB emergency buffer; never replace an existing file.
set -euo pipefail
file=/swapfile-whatnow
if [[ ! -e "$file" ]]; then
  available=$(df -B1 --output=avail / | tail -1)
  (( available >= 4 * 1024 * 1024 * 1024 )) || { echo 'Insufficient disk reserve for swap' >&2; exit 1; }
  umask 077
  dd if=/dev/zero of="$file" bs=1M count=1024 status=none
  chmod 600 "$file"
  mkswap "$file" >/dev/null
  echo 'changed: created swap'
fi
[[ ! -L "$file" && $(stat -c %s "$file") == 1073741824 ]]
[[ $(blkid -p -s TYPE -o value "$file") == swap ]]
chmod 600 "$file"
if ! swapon --show=NAME --noheadings | grep -Fxq "$file"; then
  swapon "$file"
  echo 'changed: enabled swap'
fi
if ! grep -Eq '^/swapfile-whatnow[[:space:]]' /etc/fstab; then
  echo '/swapfile-whatnow none swap sw 0 0' >> /etc/fstab
  echo 'changed: persisted swap'
fi
