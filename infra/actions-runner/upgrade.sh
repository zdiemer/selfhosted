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
ARC_VERSION="${ARC_VERSION:-0.14.2}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

# The PAT comes from 1Password into memory for this run only; each helm call
# gets its own <(sv_fd) pipe. See scripts/lib/secret-values.sh.
. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

NS_CTRL="$(awk '/^  controller:/{print $2; exit}' "$VALUES")"
NS_PREFIX="$(awk '/^  runnersPrefix:/{print $2; exit}' "$VALUES")"

echo "==> 1/3 controller: gha-runner-scale-set-controller@${ARC_VERSION} -n ${NS_CTRL}"
helm upgrade --install arc "${OCI}/gha-runner-scale-set-controller" --version "$ARC_VERSION" \
  -n "$NS_CTRL" --create-namespace --cleanup-on-fail --wait

echo "==> 2/3 namespaces + secrets: actions-runner -n ${NS_CTRL}"
helm upgrade --install actions-runner "$HERE" -n "$NS_CTRL" \
  -f "$VALUES" -f <(sv_fd) --cleanup-on-fail

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
