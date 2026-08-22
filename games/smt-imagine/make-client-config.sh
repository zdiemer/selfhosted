#!/usr/bin/env bash
# Write the two files that point a ReIMAGINE client at this server:
#
#   ImagineClient.dat   plaintext; lobby host + port
#   webaccess.sdat      encrypted (COMP_hack key); the in-game web endpoints,
#                       of which `login` is the one that matters — the client
#                       POSTs its credentials there, over plain HTTP, to the
#                       lobby's web port
#
# Encryption is done by comp_encrypt inside the running lobby container, so
# this needs the release up. Output lands in ./client-config/; drop both files
# into the client directory, replacing the ones from the archive.
#
# Usage: ./make-client-config.sh [host]     default: ingress.host from values.yaml

set -euo pipefail

RELEASE="${RELEASE:-smt-imagine}"
NAMESPACE="${NAMESPACE:-games}"
HERE="$(cd "$(dirname "$0")" && pwd)"
HOST="${1:-$(awk '/^  host:/{print $2; exit}' "${HERE}/values.yaml")}"
LOBBY="$(awk '/^  lobby:/{print $2; exit}' "${HERE}/values.yaml")"
WEB="$(awk '/^  web:/{print $2; exit}' "${HERE}/values.yaml")"
OUT="${HERE}/client-config"
mkdir -p "$OUT"

printf -- '-ip %s\r\n-port %s\r\n' "$HOST" "$LOBBY" > "${OUT}/ImagineClient.dat"

# The non-login lines are the in-game web panels (casino, roulette, shop);
# they point at the same lobby web server, which answers 404 for them. Kept
# so the client finds every key it looks for.
B="http://${HOST}:${WEB}"
cat > "${OUT}/webaccess.dat" <<EOF
<dbnet = ${B}/index/auth?user_id=%s&user_password=%s&character_name=%s&world_id=%d>
<dcoshop = ${B}/index/auth?user_id=%s&user_password=%s&character_name=%s&world_id=%d&ref=shop>
<slot = ${B}/index/auth?ref=ddslot&user_id=%s&user_password=%s&character_name=%s&world_id=%d&session_id=%s&mid=%d>
<roulette = ${B}/index/auth?ref=jr&user_id=%s&user_password=%s&character_name=%s&world_id=%d&session_id=%s>
<videogame = ${B}/index/auth?ref=tower&user_id=%s&user_password=%s&character_name=%s&world_id=%d&session_id=%s&mid=%d>
<slotvip = ${B}/index/auth?ref=ddslotv&user_id=%s&user_password=%s&character_name=%s&world_id=%d&session_id=%s&mid=%d>
<roulettevip = ${B}/index/auth?ref=jrv&user_id=%s&user_password=%s&character_name=%s&world_id=%d&session_id=%s>
<videogamevip = ${B}/index/auth?ref=towerv&user_id=%s&user_password=%s&character_name=%s&world_id=%d&session_id=%s&mid=%d>
<kino = ${B}/index/auth?ref=casino&user_id=%s&user_password=%s&character_name=%s&world_id=%d&session_id=%s>
<login = ${B}/>
<birthday = ${B}/index/auth?user_id=%s&user_password=%s>
EOF

echo "==> Encrypting webaccess.dat with comp_encrypt in the lobby container"
kubectl -n "$NAMESPACE" exec -i "deploy/${RELEASE}" -c lobby -- \
  sh -c 'cat > /tmp/webaccess.dat && comp_encrypt /tmp/webaccess.dat /tmp/webaccess.sdat >/dev/null && cat /tmp/webaccess.sdat && rm -f /tmp/webaccess.dat /tmp/webaccess.sdat' \
  < "${OUT}/webaccess.dat" > "${OUT}/webaccess.sdat"

ls -la "$OUT"
echo "==> Copy ImagineClient.dat and webaccess.sdat into the client directory."
