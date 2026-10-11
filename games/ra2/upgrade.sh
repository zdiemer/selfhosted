#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# These names must resolve to tailnet addresses before publishing any assets.
python3 - "$HERE/values.yaml" <<'PY'
import ipaddress, socket, sys, yaml
v = yaml.safe_load(open(sys.argv[1]))
for host in v['ingress']['hosts']:
    addresses = {row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    if not addresses or any(ipaddress.ip_address(ip) not in ipaddress.ip_network('100.64.0.0/10') for ip in addresses):
        raise SystemExit(f'{host} must resolve only to tailnet IPv4 addresses: {addresses}')
PY
kubectl -n games get pvc romm-library >/dev/null
helm upgrade --install ra2 "$HERE" -n games --create-namespace --atomic --cleanup-on-fail --timeout 5m
kubectl -n games rollout status deployment/ra2 --timeout=180s
for host in ra2.zachd.duckdns.org yuri.zachd.duckdns.org; do
  curl --fail --silent --show-error "https://$host/healthz"
  echo " https://$host"
done
