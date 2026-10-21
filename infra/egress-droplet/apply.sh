#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Fail before reading secrets or making any remote change until live review.
python3 - "$HERE/config.yml" <<'PY'
import sys, yaml, ipaddress
config = yaml.safe_load(open(sys.argv[1]))
if not config['live_configuration_reviewed']:
    sys.exit('Live egress configuration has not been reviewed; adoption is blocked.')
ipaddress.IPv4Address(config['public_ipv4'])
PY
source "$HERE/../../scripts/lib/secret-values.sh"
_sv_session
export EGRESS_SECRET_DIR="$SV_DIR"
op read 'op://homelab/egress-sfo3-production/secrets.json' > "$SV_DIR/secrets.json"
chmod 600 "$SV_DIR/secrets.json"
export ANSIBLE_LOCAL_TEMP="$SV_DIR/ansible"
export ANSIBLE_PIPELINING=true
uv run --with ansible-core==2.19.3 ansible-playbook -i "$HERE/inventory.yml" "$HERE/site.yml" "$@"
