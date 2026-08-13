#!/usr/bin/env bash
# Apply the egress proxy.
#
# This is on the critical path for every service that has opted in: if the
# config is wrong, their outbound traffic stops, and if it is wrong in the
# particular way that matters, it silently leaves from the house instead. So the
# script does four things before it touches the release:
#
#   1. Generates any missing client passwords into values.local.yaml.
#   2. Builds the htpasswd Secret, reusing existing salts so it does not churn.
#   3. Asserts the invariants that make this a blind tunnel and a fail-closed
#      one, against the rendered config.
#   4. Parses the rendered config with the real squid binary, in a throwaway
#      pod, before applying.
#
# Then it applies, waits, and proves a request actually gets out.

set -euo pipefail

RELEASE="${RELEASE:-egress-proxy}"
NAMESPACE="${NAMESPACE:-infra}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
# The client passwords come from 1Password into memory and never onto a disk.
# See scripts/lib/secret-values.sh.
. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

K="kubectl -n ${NAMESPACE}"
CANARY="egress-proxy-canary"
AUTH_SECRET="${RELEASE}-auth"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }
command -v openssl >/dev/null || { echo "openssl required (htpasswd generation)"; exit 1; }
command -v python3 >/dev/null || { echo "python3 required"; exit 1; }

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

cleanup() { $K delete pod "$CANARY" --ignore-not-found --wait=false >/dev/null 2>&1 || true; }
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 1. Client passwords
# ---------------------------------------------------------------------------
# Generated rather than chosen: nothing types these, they only ever move from
# this file into a Secret and from there into an env var.
echo "==> Client credentials"
# This script used to GENERATE missing passwords straight into values.local.yaml.
# It cannot any more, and it should not: with the file gone the generated value
# would live only in this process, so the next run would generate a different
# one, rewrite the htpasswd Secret, and break every proxied client's egress.
#
# So it refuses instead. This only fires when a NEW client is added to
# values.yaml — perhaps twice a year — in exchange for deleting a whole class of
# race. The password is not printed here on purpose: a terminal is not a safe
# sink for one (tmux scrollback, shell history, this repo's own logs).
#
# The resolved document goes through a tmpfs FILE, not a pipe on stdin: this
# python reads its own script from stdin (`python3 -` plus a heredoc), so a
# `sv_fd | python3 - <<PY` would have the heredoc replace the pipe and hand the
# script an EMPTY stdin. That failure is quiet and total — every client reads as
# having no password, and the deploy refuses every single time.
LOCAL_DOC="$(sv_file egress-local.yaml)"
sv_fd > "$LOCAL_DOC"

VALUES="$VALUES" LOCAL_DOC="$LOCAL_DOC" python3 - <<'PY' || exit 1
import os, sys, yaml

values = yaml.safe_load(open(os.environ["VALUES"])) or {}
local = yaml.safe_load(open(os.environ["LOCAL_DOC"])) or {}
pw = local.get("clientPasswords") or {}

missing = [c["name"] for c in (values.get("clients") or []) if not pw.get(c["name"])]
if missing:
    print("FAIL: no password in the vault for client(s): " + ", ".join(missing), file=sys.stderr)
    print("", file=sys.stderr)
    print("  These are generated, never chosen. For each one:", file=sys.stderr)
    print("      openssl rand -base64 24", file=sys.stderr)
    print("  then add them under clientPasswords with:", file=sys.stderr)
    print("      ./scripts/secrets.sh edit infra/egress-proxy", file=sys.stderr)
    print("", file=sys.stderr)
    print("  clientPasswords is a MAP keyed by name, never part of the `clients`", file=sys.stderr)
    print("  list in values.yaml: helm merges maps by key but REPLACES lists", file=sys.stderr)
    print("  wholesale, so a client added to a list would drop every other one.", file=sys.stderr)
    sys.exit(1)

print(f"    {len(pw)} client credential(s) in the vault, none missing")
PY

VALUE_ARGS=(-f "${HERE}/values.yaml")

RENDERED="$(helm template "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" -f <(sv_fd))"

# ---------------------------------------------------------------------------
# 2. The htpasswd Secret
# ---------------------------------------------------------------------------
# Not a Helm template: no template function produces a hash basic_ncsa_auth
# accepts, and Sprig's bcrypt `htpasswd` re-salts on every render — which would
# rewrite the Secret on every upgrade, and glibc's crypt() does not do bcrypt
# anyway. apr1 is what squid's helper reads, and reusing the existing salt keeps
# the output byte-identical when nothing has changed.
echo "==> htpasswd Secret ${AUTH_SECRET}"
EXISTING="$($K get secret "$AUTH_SECRET" -o jsonpath='{.data.passwd}' 2>/dev/null | base64 -d 2>/dev/null || true)"

PASSWD="$(RENDERED="$RENDERED" EXISTING="$EXISTING" python3 - <<'PY'
import os, re, subprocess, sys
try:
    import yaml
except ImportError:
    sys.exit("PyYAML required")

existing = {}
for line in os.environ["EXISTING"].splitlines():
    if ":" in line:
        u, h = line.split(":", 1)
        existing[u] = h

creds = {}
for doc in yaml.safe_load_all(os.environ["RENDERED"]):
    if not doc or doc.get("kind") != "Secret":
        continue
    sd = doc.get("stringData") or {}
    if "username" in sd and "password" in sd:
        creds[sd["username"]] = sd["password"]

out = []
for user in sorted(creds):
    pw  = creds[user]
    old = existing.get(user, "")
    salt = None
    m = re.match(r'^\$apr1\$([^$]+)\$', old)
    if m:
        salt = m.group(1)
        # Same salt + same password => same hash. If it matches, the credential
        # has not changed and the Secret must not be rewritten.
        again = subprocess.run(["openssl", "passwd", "-apr1", "-salt", salt, pw],
                               capture_output=True, text=True).stdout.strip()
        if again == old:
            out.append(f"{user}:{old}")
            continue
    h = subprocess.run(["openssl", "passwd", "-apr1", pw],
                       capture_output=True, text=True).stdout.strip()
    out.append(f"{user}:{h}")
print("\n".join(out))
PY
)"

if [[ "$PASSWD" == "$EXISTING" ]]; then
  echo "    unchanged"
else
  $K create secret generic "$AUTH_SECRET" \
    --from-literal=passwd="$PASSWD" \
    --dry-run=client -o yaml | $K apply -f - >/dev/null
  echo "    written ($(grep -c . <<<"$PASSWD") entries)"
fi

# ---------------------------------------------------------------------------
# 3. Invariants
# ---------------------------------------------------------------------------
# These are the properties that are expensive to lose and cheap to check, and
# every one of them fails silently rather than loudly in production.
echo "==> Asserting config invariants"
RENDERED="$RENDERED" python3 - <<'PY'
import os, re, sys
try:
    import yaml
except ImportError:
    sys.exit("PyYAML required")

conf = None
for doc in yaml.safe_load_all(os.environ["RENDERED"]):
    if doc and doc.get("kind") == "ConfigMap" and "squid.conf" in (doc.get("data") or {}):
        conf = doc["data"]["squid.conf"]
if conf is None:
    sys.exit("FAIL: no squid.conf in the rendered output")

fails = []

# A blind tunnel. Any of these means TLS is being terminated, which breaks
# curl_cffi/Camoufox/httpcloak fingerprinting without any error surfacing.
for bad in ("ssl_bump", "sslproxy_cert", "ssl-bump", "generate-host-certificates"):
    if re.search(rf'^\s*{re.escape(bad)}', conf, re.M):
        fails.append(f"{bad} present — this must stay a blind CONNECT tunnel")

# Default-deny. Without a terminal deny, squid's built-in default applies and
# the set of things it permits is not this file's decision.
if not re.search(r'^\s*http_access deny all\s*$', conf, re.M):
    fails.append("no terminal `http_access deny all`")

# Fail-closed exits. A client routed to a peer MUST also be never_direct, or a
# dead peer silently drops it back onto the home address.
peered = set(re.findall(r'^\s*cache_peer_access \S+ allow (\S+)', conf, re.M))
nodirect = set(re.findall(r'^\s*never_direct allow (\S+)', conf, re.M))
for acl in sorted(peered - nodirect):
    fails.append(f"{acl} is routed to a peer but has no `never_direct` — a dead "
                 f"peer would silently fall back to the home address")

# Every peer must be closed off to everyone not explicitly allowed.
declared = set(re.findall(r'^\s*cache_peer_access (\S+) allow', conf, re.M))
denied   = set(re.findall(r'^\s*cache_peer_access (\S+) deny all', conf, re.M))
for peer in sorted(declared - denied):
    fails.append(f"peer {peer} has no closing `cache_peer_access {peer} deny all`")

# ONE PORT PER LANE, AND NO TWO LANES SHARING ONE. Two exits behind a single
# port collapse into one clearance bucket for any client that keys anti-bot
# state on scheme://host:port, which is a 403 storm rather than a config error.
ports = re.findall(r'^\s*http_port (\d+) name=(\S+)', conf, re.M)
seen = {}
for port, lane in ports:
    if port in seen:
        fails.append(f"lanes {seen[port]} and {lane} share port {port} — they would "
                     f"share a clearance bucket while using different exits")
    seen[port] = lane

# Every lane that listens must also be routed, or a client reaches a listener
# with no exit behind it and gets what looks like a network fault.
lanes = {lane for _, lane in ports}
routed = set(re.findall(r'^\s*(?:always_direct|never_direct) allow lane_(\S+)', conf, re.M))
for lane in sorted(lanes - routed):
    fails.append(f"lane {lane} listens but has no always_direct/never_direct rule")

for f in fails:
    print(f"    FAIL {f}")
if fails:
    sys.exit(1)
print("    ok: blind tunnel, default-deny, every peered client fails closed")
PY

# ---------------------------------------------------------------------------
# 4. Does the real binary accept it?
# ---------------------------------------------------------------------------
IMAGE="$(python3 -c "
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d and d.get('kind') == 'Deployment':
        print(d['spec']['template']['spec']['containers'][0]['image'])
" <<<"$RENDERED")"

echo "==> Parsing the rendered config on ${IMAGE}"
$K delete pod "$CANARY" --ignore-not-found >/dev/null 2>&1 || true
$K run "$CANARY" --image="$IMAGE" --restart=Never --command -- sleep 300 >/dev/null
for _ in $(seq 1 40); do
  sleep 3
  [[ "$($K get pod "$CANARY" -o jsonpath='{.status.phase}' 2>/dev/null)" == "Running" ]] && break
done

python3 -c "
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if not d: continue
    if d.get('kind') == 'ConfigMap' and 'squid.conf' in (d.get('data') or {}):
        sys.stdout.write(d['data']['squid.conf'])
" <<<"$RENDERED" | $K exec -i "$CANARY" -- sh -c 'mkdir -p /etc/squid/peers /etc/squid/secret && cat > /etc/squid/test.conf'

# EVERY key of the peers Secret, not just peers.conf. The pinned CA lands in the
# same mount and `tls-cafile` names it, and squid does not treat a missing CA
# file as fatal — it logs "Ignoring error setting CA certificate location" and
# carries on with default verification, which then rejects a self-signed peer.
# A canary that only wrote peers.conf reproduced exactly that warning and failed
# the deploy for a problem that did not exist.
for KEY in $(python3 -c "
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d and d.get('kind') == 'Secret' and 'peers.conf' in (d.get('stringData') or {}):
        print('\n'.join(d['stringData'])); break
" <<<"$RENDERED"); do
  python3 -c "
import sys, yaml
key = sys.argv[1]
for d in yaml.safe_load_all(sys.stdin):
    if d and d.get('kind') == 'Secret' and 'peers.conf' in (d.get('stringData') or {}):
        sys.stdout.write(d['stringData'][key]); break
" "$KEY" <<<"$RENDERED" | $K exec -i "$CANARY" -- sh -c "cat > /etc/squid/peers/${KEY}"
done

PARSE="$($K exec "$CANARY" -- sh -c 'touch /etc/squid/secret/passwd; squid -k parse -f /etc/squid/test.conf 2>&1' || true)"
# The Via warning is expected and deliberate — see values.yaml.
if grep -iE 'ERROR|FATAL|aborting' <<<"$PARSE" | grep -v 'requires the use of Via'; then
  echo "FAIL: squid rejected the rendered config. Nothing has been applied."
  sed 's/^/      /' <<<"$PARSE" | tail -20
  exit 1
fi
echo "    ok: squid parsed it"
cleanup

# ---------------------------------------------------------------------------
# 5. Apply
# ---------------------------------------------------------------------------
echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" -f <(sv_fd) --atomic --cleanup-on-fail

echo "==> Waiting for rollout"
$K rollout status "deployment/${RELEASE}" --timeout=180s

# ---------------------------------------------------------------------------
# 6. Prove a request actually gets out
# ---------------------------------------------------------------------------
# A listener that accepts connections and refuses every request looks identical
# to a healthy one from the Deployment's point of view.
FIRST_CLIENT="$(python3 -c "
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d and d.get('kind') == 'Secret' and 'username' in (d.get('stringData') or {}):
        print(d['stringData']['username'], d['stringData']['password'], sep='\t'); break
" <<<"$RENDERED")"

if [[ -z "$FIRST_CLIENT" ]]; then
  echo "==> No clients configured yet — skipping the egress smoke test."
  echo "    Add one under 'clients:' in values.yaml and re-run."
  exit 0
fi

# The first client's OWN lane port, not "the Service's port" — there is no such
# thing any more, and a client is only permitted on its own lane's listener.
PORT="$(python3 -c "
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d and d.get('kind') == 'Secret' and 'username' in (d.get('stringData') or {}):
        print(d['stringData']['port']); break
" <<<"$RENDERED")"

USER_NAME="${FIRST_CLIENT%%$'\t'*}"
USER_PASS="${FIRST_CLIENT##*$'\t'}"
ADDR="${RELEASE}.${NAMESPACE}.svc.cluster.local:${PORT}"

echo "==> Egress smoke test as '${USER_NAME}'"
SMOKE="egress-smoke-$$"
# Deliberately not `kubectl run -i`: attaching races the container, and output
# written before the attach lands is lost — which showed up here as a passing
# request whose first line had vanished. Run to completion, then read the logs.
$K delete pod "$SMOKE" --ignore-not-found --wait=true >/dev/null 2>&1 || true
# The first attempt is retried on purpose. A newly created pod is not
# immediately reachable through the proxy's NetworkPolicy: kube-router has to
# resolve its labels into an ipset first, and until it does the connection is
# refused outright. Measured here at well under two seconds — the request at
# t=0s fails and the one at t=2s succeeds — but a client that makes exactly one
# request in its first second would see it. See the README.
$K run "$SMOKE" --restart=Never --image=curlimages/curl:8.11.1 \
  --labels="egress.zachd/proxied=true" --command -- \
  sh -c "echo -n 'AUTHED='; curl -sS --retry 5 --retry-all-errors --retry-delay 2 --max-time 20 -x 'http://${USER_NAME}:${USER_PASS}@${ADDR}' https://api.ipify.org 2>&1 || true;
         echo; echo -n 'UNAUTHED='; curl -sS --max-time 20 -o /dev/null -x 'http://${ADDR}' https://api.ipify.org 2>&1 || true; echo" >/dev/null

for _ in $(seq 1 40); do
  sleep 3
  PH="$($K get pod "$SMOKE" -o jsonpath='{.status.phase}' 2>/dev/null || echo)"
  [[ "$PH" == "Succeeded" || "$PH" == "Failed" ]] && break
done
OUT="$($K logs "$SMOKE" 2>/dev/null || true)"
$K delete pod "$SMOKE" --ignore-not-found --wait=false >/dev/null 2>&1 || true
sed 's/^/    /' <<<"$OUT"

# An address came back => the tunnel works end to end on this client's lane.
# Matched anywhere rather than anchored to AUTHED=, because a retried first
# attempt prints its error on that line and the address on the next one.
if ! grep -qE '(^|[^0-9.])[0-9]{1,3}(\.[0-9]{1,3}){3}([^0-9.]|$)' <<<"$OUT"; then
  echo "    WARN: no address came back. Check: $K logs deploy/${RELEASE}"
fi
# A denied CONNECT is a transport failure, not an HTTP status: curl reports
# "CONNECT tunnel failed, response 407" and exits 56, and %{http_code} would be
# 000 because no end-to-end response was ever received. Match the 407 itself.
if ! grep -q '407' <<<"$OUT"; then
  echo "    WARN: an unauthenticated request was not refused with 407."
  echo "          Auth may not be enforced — every log line's svc= would be '-'."
fi

cat <<'EOF'

    The proxy is up. Nothing routes through it until a chart opts in with:
        egress.proxy.enabled: true
    and stamps  egress.zachd/proxied: "true"  on its pods (the NetworkPolicy
    selects on that label, so a pod without it cannot reach the listener).

    This script GENERATES client passwords into values.local.yaml, so it is the
    one chart whose local file can be newer than 1Password. Push it back, or the
    next machine materializes the old passwords:

        ../../scripts/secrets.sh push infra/egress-proxy/values.local.yaml

    (Deliberately no auto-materialize block above: step 1 creates the file
    itself, and racing that against op inject would only confuse things.)
EOF
