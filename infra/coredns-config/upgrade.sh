#!/usr/bin/env bash
# Apply the CoreDNS configuration overlay.
#
# THIS RESTARTS CLUSTER DNS. CoreDNS runs one replica with maxUnavailable: 1, so
# the old pod goes away before the replacement is ready — a short, cluster-wide
# gap — and a Corefile that fails to parse leaves nothing serving :53 at all.
# The `reload` plugin does not help: it watches the Corefile, not the files the
# Corefile imports, so a change here is inert until a restart happens.
#
# So this script is mostly guard rails, in order:
#   1. Confirm the two things that make the extension point work at all — the
#      import line in the live Corefile, and the volume mount on the Deployment.
#   2. Boot the real CoreDNS image on our stanza and prove it parses, BEFORE
#      touching anything live.
#   3. Apply, restart, and verify resolution actually works afterwards.
#   4. Roll the ConfigMap back automatically if it doesn't.

set -euo pipefail

RELEASE="${RELEASE:-coredns-config}"
NAMESPACE="${NAMESPACE:-infra}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
VALUE_ARGS=(-f "$VALUES")
[[ -f "${HERE}/values.local.yaml" ]] && VALUE_ARGS+=(-f "${HERE}/values.local.yaml")

KS="kubectl -n kube-system"
CANARY="coredns-config-canary"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

cleanup() {
  $KS delete pod "$CANARY" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  $KS delete configmap "$CANARY" --ignore-not-found >/dev/null 2>&1 || true
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 1. The extension point still exists
# ---------------------------------------------------------------------------
# Both of these are k3s's, not ours. A k3s upgrade that changed either would
# make this chart a silent no-op: the ConfigMap would apply cleanly, CoreDNS
# would restart cleanly, and nothing would be logged.
echo "==> Checking the CoreDNS extension point"

COREFILE="$($KS get cm coredns -o jsonpath='{.data.Corefile}')"
if ! grep -q 'import /etc/coredns/custom/\*\.override' <<<"$COREFILE"; then
  echo "FAIL: the live Corefile no longer imports /etc/coredns/custom/*.override."
  echo "      k3s owns that file. Without the import this chart does nothing."
  exit 1
fi
echo "    ok: Corefile imports *.override inside the server block"

MOUNT="$($KS get deploy coredns \
  -o jsonpath='{.spec.template.spec.volumes[?(@.name=="custom-config-volume")].configMap.name}')"
if [[ "$MOUNT" != "coredns-custom" ]]; then
  echo "FAIL: the CoreDNS Deployment does not mount a ConfigMap named coredns-custom."
  echo "      Found: '${MOUNT:-<none>}'. The name is hardcoded in the template for"
  echo "      exactly this reason; a rename here is not a rename we can make."
  exit 1
fi
echo "    ok: Deployment mounts coredns-custom at /etc/coredns/custom"

IMAGE="$($KS get deploy coredns -o jsonpath='{.spec.template.spec.containers[0].image}')"
echo "    CoreDNS image: ${IMAGE}"

# ---------------------------------------------------------------------------
# 2. Does our stanza parse on this exact image?
# ---------------------------------------------------------------------------
# Render what we are about to apply and run the real binary on it. Deliberately
# NOT a copy of the whole live Corefile: that config is k3s's and already known
# good, and reproducing it here would need the kubernetes plugin, the CoreDNS
# service account and NodeHosts. The only new thing is our stanza, so that is
# the only thing worth proving. A minimal server block with a plain forwarder is
# enough to catch the failure that actually threatens us — a directive this
# CoreDNS version does not accept.
RENDERED="$(helm template "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}")"

# Every `*.override` key the chart renders, as `### <key>` followed by its body,
# sorted by key. Each feature owns a key (dnsLogging, internalNames, whatever
# comes next), so this reads them all rather than naming one: a key it did not
# know about would otherwise be applied without ever being validated. The same
# shape is read back off the live ConfigMap below, so the two are comparable.
OVERRIDES="$(printf '%s' "$RENDERED" | python3 -c '
import re, sys
blocks, key, buf = [], None, []
for line in sys.stdin.read().split("\n"):
    if key is not None:
        # The block ends at the first non-blank line that leaves the indent.
        if line.strip() and not line.startswith("    "):
            blocks.append((key, "\n".join(buf).strip("\n")))
            key, buf = None, []
        else:
            buf.append(line[4:])
            continue
    m = re.match(r"^  (\S+\.override): \|\s*$", line)
    if m:
        key, buf = m.group(1), []
if key is not None:
    blocks.append((key, "\n".join(buf).strip("\n")))
for k, v in sorted(blocks):
    print("### " + k)
    print(v)
')"

# The bodies without the key markers — this is the Corefile fragment itself.
STANZAS="$(grep -v '^### ' <<<"$OVERRIDES" || true)"

if [[ -z "${STANZAS//[[:space:]]/}" ]]; then
  echo "==> Nothing is enabled — the ConfigMap will be removed"
  WANT=""
else
  echo "==> Validating the rendered stanzas on ${IMAGE}"
  WANT="$OVERRIDES"

  $KS delete pod "$CANARY" --ignore-not-found >/dev/null 2>&1 || true
  CANARY_COREFILE=".:5353 {
    errors
$(sed 's/^/    /' <<<"$STANZAS")
    forward . 1.1.1.1
}"
  $KS create configmap "$CANARY" --from-literal=Corefile="$CANARY_COREFILE" \
    --dry-run=client -o yaml | $KS apply -f - >/dev/null

  cat <<EOF | $KS apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: ${CANARY}
  labels:
    app.kubernetes.io/name: coredns-config
spec:
  restartPolicy: Never
  containers:
    - name: coredns
      image: ${IMAGE}
      args: ["-conf", "/etc/coredns/Corefile"]
      volumeMounts:
        - name: config
          mountPath: /etc/coredns
  volumes:
    - name: config
      configMap:
        name: ${CANARY}
EOF

  # CoreDNS exits immediately and non-zero on a config it cannot parse, and runs
  # forever on one it can. So "still Running after a few seconds" is the pass.
  OK=""
  for _ in $(seq 1 20); do
    sleep 1
    PHASE="$($KS get pod "$CANARY" -o jsonpath='{.status.phase}' 2>/dev/null || echo "")"
    [[ "$PHASE" == "Failed" ]] && break
    if [[ "$PHASE" == "Running" ]] && $KS logs "$CANARY" 2>/dev/null | grep -q 'CoreDNS-'; then
      OK=1; break
    fi
  done

  if [[ -z "$OK" ]]; then
    echo "FAIL: CoreDNS would not start with these stanzas. Nothing has been changed."
    $KS logs "$CANARY" 2>&1 | sed 's/^/      /' | head -20
    $KS delete configmap "$CANARY" --ignore-not-found >/dev/null 2>&1 || true
    exit 1
  fi
  echo "    ok: CoreDNS parsed the stanzas and started"
  $KS delete configmap "$CANARY" --ignore-not-found >/dev/null 2>&1 || true
  cleanup
fi

# ---------------------------------------------------------------------------
# 3. Apply, and restart only if the content actually moved
# ---------------------------------------------------------------------------
HAVE="$($KS get cm coredns-custom -o json 2>/dev/null | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin).get("data") or {}
except Exception:
    data = {}
for k in sorted(k for k in data if k.endswith(".override")):
    print("### " + k)
    print(data[k].strip("\n"))
' || true)"
BEFORE_HASH="$(printf '%s' "$HAVE" | md5sum | cut -d' ' -f1)"
AFTER_HASH="$(printf '%s' "$WANT" | md5sum | cut -d' ' -f1)"

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" --cleanup-on-fail

collector_status() {
  # The collector is deployed alongside the log, not by a separate release, so
  # report it here whether or not CoreDNS itself had to move.
  if kubectl -n "$NAMESPACE" get deploy "${RELEASE}-collector" >/dev/null 2>&1; then
    echo "==> Waiting for the collector"
    kubectl -n "$NAMESPACE" rollout status "deployment/${RELEASE}-collector" --timeout=120s || {
      echo "    WARN: collector did not become ready. Query logging is unaffected,"
      echo "          but nothing is reading it. Check:"
      echo "            kubectl -n ${NAMESPACE} logs deploy/${RELEASE}-collector"
      return 0
    }
    kubectl -n "$NAMESPACE" logs "deployment/${RELEASE}-collector" --tail=3 2>/dev/null | sed 's/^/      /'
  fi
}

if [[ "$BEFORE_HASH" == "$AFTER_HASH" ]]; then
  echo "==> CoreDNS config unchanged — no restart needed"
  collector_status
  exit 0
fi

# Both directions need the restart. Turning logging OFF removes the ConfigMap,
# but the running CoreDNS keeps its parsed config and keeps logging until it is
# replaced.
echo "==> Restarting CoreDNS (one replica, maxUnavailable 1 — expect a brief DNS gap)"
$KS rollout restart deployment coredns

if ! $KS rollout status deployment coredns --timeout=90s; then
  echo "FAIL: CoreDNS did not come back. Rolling the ConfigMap back."
  $KS delete configmap coredns-custom --ignore-not-found
  $KS rollout restart deployment coredns
  $KS rollout status deployment coredns --timeout=90s || true
  echo "      Rolled back. The Helm release still believes logging is on —"
  echo "      set dnsLogging.enabled: false and re-run before trying again."
  exit 1
fi

# ---------------------------------------------------------------------------
# 4. Prove resolution still works
# ---------------------------------------------------------------------------
# A Corefile can parse and start and still resolve nothing — a broken `forward`
# or a kubernetes plugin that never goes ready both look healthy from the
# Deployment's point of view. Ask it a real question, internal and external.
echo "==> Verifying resolution"
RESOLVE_OUT="$(kubectl run "coredns-config-verify-$$" --rm -i --restart=Never \
  --pod-running-timeout=60s --image=busybox:1.36 --command -- sh -c \
  'nslookup kubernetes.default.svc.cluster.local >/dev/null 2>&1 && echo INTERNAL_OK;
   nslookup api.ipify.org >/dev/null 2>&1 && echo EXTERNAL_OK' 2>/dev/null || true)"

if ! grep -q INTERNAL_OK <<<"$RESOLVE_OUT"; then
  echo "FAIL: cluster DNS is not resolving after the restart. Rolling back."
  $KS delete configmap coredns-custom --ignore-not-found
  $KS rollout restart deployment coredns
  $KS rollout status deployment coredns --timeout=90s || true
  exit 1
fi
echo "    ok: internal resolution"
grep -q EXTERNAL_OK <<<"$RESOLVE_OUT" \
  && echo "    ok: external resolution" \
  || echo "    WARN: external name did not resolve — check the upstream forwarder"

if grep -q '^### internal-names\.override$' <<<"$WANT"; then
  echo "==> The DuckDNS zone is answered internally now:"
  $KS get cm coredns-custom -o jsonpath='{.data.internal-names\.override}' \
    | grep -E '^\s+name regex' | sed 's/^/      /'
fi

if grep -q '^### egress-audit\.override$' <<<"$WANT"; then
  echo "==> Query logging is ON. Sample:"
  $KS logs deployment/coredns --tail=5 2>/dev/null | sed 's/^/      /'
  collector_status
  cat <<'EOF'

    The window is open. This is a measurement window, not a steady state.

        ./scripts/egress-audit.sh status     # how much is captured so far
        ./scripts/egress-audit.sh report     # the inventory

    Close it again when you have what you need:
        rm infra/coredns-config/values.local.yaml
        ./infra/coredns-config/upgrade.sh

    Manual rollback, if ever needed:
        kubectl -n kube-system delete configmap coredns-custom
        kubectl -n kube-system rollout restart deployment coredns
EOF
else
  echo "==> Query logging is OFF"
  collector_status
fi
