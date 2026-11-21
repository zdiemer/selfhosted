#!/usr/bin/env bash
# Apply the runner fleet: the ARC controller, this chart (the PAT Secret), then
# one runner scale set per repo listed in values.yaml.
#
# Order matters on first install: the scale-set chart's listener needs the
# controller's CRDs (AutoscalingRunnerSet) to exist and its namespace + Secret
# to read, so the controller and this chart go first and the scale sets last.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
OCI="oci://ghcr.io/actions/actions-runner-controller-charts"
# Both upstream charts ship in lock-step under one version. Pinned: a listener
# and a controller from different releases refuse to talk to each other.
# renovate: datasource=docker depName=ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set-controller
ARC_VERSION="${ARC_VERSION:-0.15.1}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

# The PAT comes from 1Password into memory for this run only; each helm call
# gets its own <(sv_fd) pipe. See scripts/lib/secret-values.sh.
. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

NS_CTRL="$(awk '/^  controller:/{print $2; exit}' "$VALUES")"
NS_PREFIX="$(awk '/^  runnersPrefix:/{print $2; exit}' "$VALUES")"

# ARC_REINSTALL=1: GitHub's documented upgrade for a release whose CRDs
# changed (0.14.x -> 0.15.0 rewrote all four). Helm never upgrades CRDs, and a
# 0.15 controller against 0.14 schemas has its status fields pruned. So:
# uninstall every scale set (while the controller is still up to run their
# finalizers), then the controller, delete the CRDs, and fall through to a
# fresh install below, which recreates them from the new chart's crds/.
# Costs a short runner outage; check `kubectl get ephemeralrunners -A` first.
if [[ "${ARC_REINSTALL:-0}" == "1" ]]; then
  echo "==> ARC_REINSTALL: removing scale sets, controller and CRDs"
  for NS in $(kubectl get ns -l actions-runner.zachd/repo -o jsonpath='{.items[*].metadata.name}'); do
    REPO="${NS#${NS_PREFIX}-}"
    helm status "arc-${REPO}" -n "$NS" >/dev/null 2>&1 || continue
    echo "    helm uninstall arc-${REPO} -n ${NS}"
    helm uninstall "arc-${REPO}" -n "$NS" --wait --timeout 5m
  done
  echo "    waiting for runner resources to drain"
  for _ in $(seq 60); do
    left="$(kubectl get autoscalingrunnersets,autoscalinglisteners,ephemeralrunnersets,ephemeralrunners -A --no-headers 2>/dev/null | wc -l)"
    [[ "$left" -eq 0 ]] && break
    sleep 5
  done
  [[ "${left:-0}" -eq 0 ]] || { echo "FAIL: ${left} ARC resources still present; not deleting CRDs"; exit 1; }
  if helm status arc -n "$NS_CTRL" >/dev/null 2>&1; then
    echo "    helm uninstall arc -n ${NS_CTRL}"
    helm uninstall arc -n "$NS_CTRL" --wait --timeout 5m
  fi
  kubectl delete crd --ignore-not-found \
    autoscalinglisteners.actions.github.com autoscalingrunnersets.actions.github.com \
    ephemeralrunners.actions.github.com ephemeralrunnersets.actions.github.com
fi

echo "==> 1/3 controller: gha-runner-scale-set-controller@${ARC_VERSION} -n ${NS_CTRL}"
helm upgrade --install arc "${OCI}/gha-runner-scale-set-controller" --version "$ARC_VERSION" \
  -n "$NS_CTRL" --create-namespace --cleanup-on-fail --wait

echo "==> 2/3 namespaces + secrets: actions-runner -n ${NS_CTRL}"
helm upgrade --install actions-runner "$HERE" -n "$NS_CTRL" \
  -f "$VALUES" -f <(sv_fd) --cleanup-on-fail

python3 "$HERE/../../scripts/internal-pki.py"

echo "==> 3/3 scale sets"
# The repo list and each repo's values are rendered out of this very chart, so
# values.yaml is the single place the fleet is described.
REPOS="$(helm template "$HERE" -f "$VALUES" --set render=repos -s templates/render-repos.yaml | sed -n 's/^  - //p')"
for REPO in $REPOS; do
  echo "    arc-${REPO}  (https://github.com/$(awk '/^  owner:/{print $2; exit}' "$VALUES")/${REPO})"
  helm upgrade --install "arc-${REPO}" "${OCI}/gha-runner-scale-set" --version "$ARC_VERSION" \
    -n "${NS_PREFIX}-${REPO}" --cleanup-on-fail \
    -f <(helm template "$HERE" -f "$VALUES" --set "render=${REPO}" -s templates/render-scale-set-values.yaml | sed '/^---/d;/^# Source:/d')
done

# Prune scale sets for repos no longer listed: their namespace carries the
# chart's repo label, and helm's uninstall of this chart drops the namespace.
for NS in $(kubectl get ns -l actions-runner.zachd/repo -o jsonpath='{.items[*].metadata.name}'); do
  REPO="${NS#${NS_PREFIX}-}"
  grep -qx "$REPO" <<<"$REPOS" && continue
  echo "==> removing arc-${REPO} (not in values.yaml)"
  helm uninstall "arc-${REPO}" -n "$NS" || true
  kubectl delete ns "$NS"
done

echo "==> Listeners (one per repo; Running = registered with GitHub)"
kubectl -n "$NS_CTRL" get pods
for REPO in $REPOS; do kubectl -n "${NS_PREFIX}-${REPO}" get pods; done
echo
echo "Each repo's runner shows under Settings → Actions → Runners as '$(awk '/^  name:/{print $2; exit}' "$VALUES")'."
echo "Switch a workflow with:  runs-on: $(awk '/^  name:/{print $2; exit}' "$VALUES")"
