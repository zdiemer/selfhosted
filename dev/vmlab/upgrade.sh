#!/usr/bin/env bash
# Apply the chart to the vmlab release.
#
# WHAT THIS DOES NOT DO: disturb running guests. Session pods are independent
# Pods created by the app, not children of this Deployment, so a helm upgrade
# rolls the UI and leaves every VM running. A console websocket open across the
# rollout will drop and noVNC will reconnect once the new pod is ready.
#
# It also does not delete configs added from the browser. Those live in
# <release>-catalog-user, which this chart deliberately does not template.

set -euo pipefail

RELEASE="${RELEASE:-vmlab}"
NAMESPACE="${NAMESPACE:-vmlab}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"

# This chart has no secrets (see values.local.yaml.example), but the shape is
# the same as every other chart here so that a future one is a one-line change.
. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

echo "==> Pre-flight"

# The premise of the whole chart. devices.kubevirt.io/kvm is advertised by
# KubeVirt's virt-handler as a plain node-level extended resource; without it
# every session pod sits Pending forever with an "Insufficient" message that
# does not obviously point at KubeVirt.
KVM_NODES="$(kubectl get nodes -o jsonpath='{range .items[*]}{.status.allocatable.devices\.kubevirt\.io/kvm}{"\n"}{end}' 2>/dev/null | grep -c . || true)"
if [[ "${KVM_NODES:-0}" -eq 0 ]]; then
  echo "FAIL: no node advertises devices.kubevirt.io/kvm."
  echo "      That resource is what gives an ordinary pod /dev/kvm without"
  echo "      privileged mode, and it comes from KubeVirt's virt-handler."
  echo "      Run dev/kubevirt/upgrade.sh first."
  exit 1
fi
echo "    ${KVM_NODES} node(s) advertise devices.kubevirt.io/kvm"

# The sandbox is NetworkPolicy. k3s enforces it via kube-router's controller
# embedded in the agent, but --disable-network-policy would silently turn every
# `none` profile into an unrestricted guest. Guests also run with NETWORK=N so
# they would still have no NIC, but the container around them would be open.
if kubectl get nodes -o jsonpath='{.items[*].metadata.annotations}' 2>/dev/null \
   | grep -q 'flannel'; then
  echo "    CNI: flannel (k3s built-in netpol controller)"
fi

STORAGE_CLASS="$(awk '/^  storageClass:/{print $2; exit}' "$VALUES")"
if ! kubectl get storageclass "$STORAGE_CLASS" >/dev/null 2>&1; then
  echo "FAIL: storage class ${STORAGE_CLASS} does not exist (isos.storageClass)."
  exit 1
fi
echo "    ISO cache class: ${STORAGE_CLASS}"

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" -f "$VALUES" -f <(sv_fd) --cleanup-on-fail

echo "==> Rollout"
$K rollout status "deployment/${RELEASE}" --timeout=120s || true

echo "==> ISO cache"
$K get pvc "${RELEASE}-isos" 2>/dev/null || echo "    none (isos.enabled=false)"

echo "==> Quota"
$K get resourcequota "$RELEASE" -o custom-columns=NAME:.metadata.name,USED:.status.used,HARD:.status.hard 2>/dev/null || true

echo "==> Sessions"
RUNNING="$($K get pods -l app.kubernetes.io/component=session --no-headers 2>/dev/null | grep -c . || true)"
echo "    ${RUNNING:-0} session pod(s) running — a UI rollout does not disturb them"

# The single most likely reason a freshly-installed lab does nothing: every
# config ships with an ISO URL but nothing is cached until a fetch Job runs.
CACHED="$($K get jobs -l vmlab.zachd/role=iso-fetch --no-headers 2>/dev/null | grep -c . || true)"
if [[ "${CACHED:-0}" -eq 0 ]]; then
  echo
  echo "    No ISO has been cached yet, so every tile will offer 'Download ISO'"
  echo "    rather than 'Launch'. That is the expected first-run state."
fi

echo
echo "==> https://$(awk '/^  host:/{print $2; exit}' "$VALUES")"
exit 0
