#!/usr/bin/env bash
# Apply the cluster-wide PriorityClasses.
#
# This chart owns the globalDefault that every pod on the cluster inherits, so it
# refuses to guess: it adopts the pre-existing objects rather than fighting them,
# checks that `value` (which Kubernetes makes immutable) hasn't drifted from what
# is live, and reports how many pods are riding the globalDefault before and
# after.

set -euo pipefail

RELEASE="${RELEASE:-priority-classes}"
NAMESPACE="${NAMESPACE:-infra}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
VALUE_ARGS=(-f "$VALUES")

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

RENDERED="$(helm template "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}")"
NAMES="$(echo "$RENDERED" | awk '/^  name:/ {print $2}')"

# `value` cannot be changed on an existing PriorityClass — the API server rejects
# it. Catch that here with a clear message instead of a wall of helm error.
echo "==> Pre-flight: immutable values"
while read -r pc; do
  [[ -z "$pc" ]] && continue
  live="$(kubectl get priorityclass "$pc" -o jsonpath='{.value}' 2>/dev/null || true)"
  want="$(echo "$RENDERED" | awk -v n="$pc" '$0 ~ "^  name: "n"$" {found=1} found && /^value:/ {print $2; exit}')"
  if [[ -n "$live" && "$live" != "$want" ]]; then
    echo "FAIL: ${pc}.value is ${live} live but ${want} in values.yaml."
    echo "      PriorityClass.value is immutable — the API server will reject this."
    echo "      Changing it means deleting and recreating the class, and if that's"
    echo "      the globalDefault, read the warning in values.yaml first."
    exit 1
  fi
  printf '    %-16s value=%s%s\n' "$pc" "${live:-<new>}" "$([[ -n "$live" ]] && echo " (unchanged)" || echo "")"
done <<< "$NAMES"

# These were created by talaria's release. Helm won't take over a resource it
# didn't create, so stamp its bookkeeping on first run. No-op afterwards.
adopt() {
  local name="$1" owner
  kubectl get priorityclass "$name" >/dev/null 2>&1 || return 0
  owner="$(kubectl get priorityclass "$name" \
    -o jsonpath='{.metadata.annotations.meta\.helm\.sh/release-name}' 2>/dev/null || true)"
  [[ "$owner" == "$RELEASE" ]] && return 0
  echo "==> adopting priorityclass/${name} from release '${owner:-<none>}' into ${RELEASE}"
  kubectl label --overwrite priorityclass "$name" app.kubernetes.io/managed-by=Helm >/dev/null
  kubectl annotate --overwrite priorityclass "$name" \
    meta.helm.sh/release-name="$RELEASE" \
    meta.helm.sh/release-namespace="$NAMESPACE" >/dev/null
}
while read -r pc; do [[ -n "$pc" ]] && adopt "$pc"; done <<< "$NAMES"

before="$(kubectl get pods -A -o jsonpath='{range .items[*]}{.spec.priorityClassName}{"\n"}{end}' 2>/dev/null | grep -c . || true)"

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}"

echo "==> Cluster scheduling policy now"
kubectl get priorityclass -o custom-columns='NAME:.metadata.name,VALUE:.value,GLOBAL-DEFAULT:.globalDefault,PREEMPTION:.preemptionPolicy' \
  | grep -vE "system-(node|cluster)-critical"

DEFAULT_PC="$(kubectl get priorityclass -o jsonpath='{range .items[?(@.globalDefault==true)]}{.metadata.name}{"\n"}{end}')"
echo
echo "    globalDefault: ${DEFAULT_PC:-<NONE — every new pod will admit at priority 0>}"
echo "    pods carrying a priority class: ${before}"
if [[ -z "$DEFAULT_PC" ]]; then
  echo "    WARNING: no globalDefault exists. New pods will admit at 0 while running"
  echo "             pods keep theirs. See the warning in values.yaml."
  exit 1
fi
