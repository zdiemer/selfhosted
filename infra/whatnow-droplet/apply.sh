#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Includes verified tmpfs scratch space, session acquisition, and cleanup traps.
source "$HERE/../../scripts/lib/secret-values.sh"
_sv_session
export WHATNOW_SECRET_DIR="$SV_DIR"
op read 'op://homelab/whatnow-production/app.env' > "$SV_DIR/app.env"
op read 'op://homelab/whatnow-production/docker.json' > "$SV_DIR/docker.json"
chmod 600 "$SV_DIR/app.env" "$SV_DIR/docker.json"
python3 "$HERE/validate-secrets.py" "$SV_DIR"
export ANSIBLE_LOCAL_TEMP="$SV_DIR/ansible"
export ANSIBLE_NOCOLOR=1
PLAYBOOK=site.yml
if [[ ${1:-} == --reboot ]]; then
  PLAYBOOK=reboot.yml
  shift
fi
# No disk cache of plaintext secrets, including Ansible transfer staging.
uv run --with ansible-core==2.19.3 ansible-playbook -i "$HERE/inventory.yml" "$HERE/$PLAYBOOK" "$@"
