#!/usr/bin/env bash
# Provision TLS and named node profiles, deploy, then check the local worker.

set -euo pipefail

RELEASE="${RELEASE:-buildkit}"
NAMESPACE="${NAMESPACE:-buildkit}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUE_ARGS=(-f "${HERE}/values.yaml")
[[ -f "${HERE}/values.local.yaml" ]] && VALUE_ARGS+=(-f "${HERE}/values.local.yaml")

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

# Best-effort idle check before any rollout. Clients can still race this check;
# upgrades are maintenance and callers must retry an interrupted build.
if [[ -n "$(kubectl -n "$NAMESPACE" get deployment "$RELEASE" --ignore-not-found -o name)" ]]; then
  kubectl -n "$NAMESPACE" exec "deploy/${RELEASE}" -c buildkitd -- \
    buildctl --addr unix:///run/user/1000/buildkit/buildkitd.sock debug histories --format '{{json .}}' | \
    python3 -c 'import json,sys; active=[json.loads(line)["record"]["Ref"] for line in sys.stdin if line.strip() and not json.loads(line)["record"].get("CompletedAt")]; assert not active, "Wait for active builds before upgrading: " + str(active)'
fi

python3 "$HERE/../../scripts/internal-pki.py"
python3 "$HERE/apply-node-profile.py"

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" --atomic --timeout=5m --cleanup-on-fail

echo "==> Waiting for rollout"
kubectl -n "$NAMESPACE" rollout status "deployment/${RELEASE}" --timeout=300s

# Check the worker advertises process isolation; real RUN verification lives
# in verify-isolation.py and must also pass after containment changes.
echo "==> Smoke: buildctl debug workers"
kubectl -n "$NAMESPACE" exec "deploy/${RELEASE}" -c buildkitd -- \
  buildctl --addr unix:///run/user/1000/buildkit/buildkitd.sock debug workers

WORKERS="$(kubectl -n "$NAMESPACE" exec "deploy/${RELEASE}" -c buildkitd -- \
  buildctl --addr unix:///run/user/1000/buildkit/buildkitd.sock debug workers --format '{{json .}}')"
python3 -c 'import json,sys; workers=json.load(sys.stdin); assert workers and all(w["labels"]["org.mobyproject.buildkit.worker.oci.process-mode"] == "sandbox" for w in workers), "Worker process sandbox missing"' <<<"$WORKERS"
