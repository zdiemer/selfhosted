#!/usr/bin/env bash
# Apply the Ingress admission policy.
#
# Cluster-scoped and cluster-wide in effect: it evaluates every Ingress write in
# every namespace except the exempt ones, including namespaces owned by other
# repos. Nothing here runs a pod — a ValidatingAdmissionPolicy is evaluated by
# the API server itself.
#
# Before applying, this dry-runs the rules against every Ingress already in the
# cluster and prints what would fail. In Warn mode that is informational; before
# flipping to Deny it is the thing you actually need to see.

set -euo pipefail

RELEASE="${RELEASE:-ingress-policy}"
NAMESPACE="${NAMESPACE:-infra}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
VALUE_ARGS=(-f "$VALUES")
[[ -f "${HERE}/values.local.yaml" ]] && VALUE_ARGS+=(-f "${HERE}/values.local.yaml")

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

if ! kubectl api-resources --api-group=admissionregistration.k8s.io 2>/dev/null \
     | grep -q validatingadmissionpolicies; then
  echo "FAIL: ValidatingAdmissionPolicy is not served by this API server."
  echo "      It is GA from Kubernetes 1.30. Check: kubectl version"
  exit 1
fi

# Evaluate the same three rules the policy enforces, against what is live now.
# Deliberately reimplemented here rather than read out of the CEL: the point is
# to answer "what breaks if I flip to Deny", and that needs to run before the
# policy is applied at all.
echo "==> Checking existing Ingresses against the rules"
kubectl get ingress -A -o json | python3 -c '
import sys, json
P    = "traefik.ingress.kubernetes.io/router."
EDGE = "ingress.zachd/tls-terminates-at-edge"
bad = []
total = 0
for i in json.load(sys.stdin)["items"]:
    total += 1
    m = i["metadata"]; a = m.get("annotations", {}) or {}
    if m["namespace"] == "kube-system":
        continue
    fails = []
    if i["spec"].get("ingressClassName") != "traefik":
        fails.append("ingressClassName != traefik")
    if a.get(P + "entrypoints") != "websecure":
        fails.append("router.entrypoints != websecure")
    if not a.get(P + "tls.certresolver") and a.get(EDGE) != "true":
        fails.append("no certresolver and no " + EDGE)
    if fails:
        bad.append((m["namespace"] + "/" + m["name"], fails))
print(f"    {total} Ingresses checked")
for name, fails in sorted(bad):
    reasons = "; ".join(fails)
    print(f"    FAIL {name}: {reasons}")
if not bad:
    print("    all conform — safe to set validationActions: [Deny]")
else:
    print(f"    {len(bad)} would be rejected under Deny; they are only warned about today")
'

ACTIONS="$(helm template "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" \
  | awk '/validationActions:/{f=1;next} f&&/^    - /{printf "%s ",$2} f&&!/^    - /{exit}')"
echo "==> validationActions: ${ACTIONS:-unknown}"
case "$ACTIONS" in
  *Deny*) echo "    ENFORCING — a non-conforming Ingress will be REJECTED at admission." ;;
  *)      echo "    Advisory only — non-conforming Ingresses are admitted with a warning." ;;
esac

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}"

# Prove the policy actually rejects something, rather than trusting that the
# object exists. --dry-run=server runs the full admission chain without
# persisting, so this is a real evaluation against a deliberately bad Ingress.
echo "==> Verifying the policy evaluates (server dry-run with a bad Ingress)"
OUT="$(kubectl apply --dry-run=server -f - 2>&1 <<'EOF' || true
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ingress-policy-selftest
  namespace: default
spec:
  rules:
    - host: ingress-policy-selftest.invalid
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: nonexistent
                port:
                  number: 80
EOF
)"
if grep -qi 'ingressClassName\|entrypoints\|certresolver' <<<"$OUT"; then
  echo "    ok: policy fired on a non-conforming Ingress"
  sed 's/^/      /' <<<"$OUT" | grep -i 'warning\|denied\|invalid' | head -4
else
  echo "    WARN: policy did not fire. Output was:"
  sed 's/^/      /' <<<"$OUT" | head -6
fi

echo "==> Policy"
kubectl get validatingadmissionpolicy "$RELEASE" \
  -o custom-columns='NAME:.metadata.name,VALIDATIONS:.spec.validations[*].reason' 2>/dev/null
kubectl get validatingadmissionpolicybinding "$RELEASE" \
  -o custom-columns='NAME:.metadata.name,ACTIONS:.spec.validationActions' 2>/dev/null
