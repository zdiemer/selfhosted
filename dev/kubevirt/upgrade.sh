#!/usr/bin/env bash
# Install / upgrade the virtualization layer: KubeVirt + CDI.
#
# Two stages, because that is how KubeVirt ships:
#
#   1. kubectl apply the pinned upstream OPERATOR manifests. These are large
#      generated files (CRD + RBAC + the operator Deployment) that belong to
#      upstream, not to us. They are fetched by version, never vendored.
#   2. helm upgrade this chart, which owns the two CUSTOM RESOURCES that tell
#      those operators what to deploy. That is the part with decisions in it.
#
# Both stages are idempotent and safe to re-run. Neither restarts a running VM:
# a configuration change is picked up live, and a KubeVirt version bump
# live-migrates guests (workloadUpdateStrategy in values.yaml).
#
# ⚠️  This script never deletes anything. Tearing KubeVirt down is genuinely
# destructive — see README §Teardown — and is deliberately not automated here.

set -euo pipefail

RELEASE="${RELEASE:-kubevirt}"
NAMESPACE="${NAMESPACE:-kubevirt}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUE_ARGS=(-f "${HERE}/values.yaml")

# renovate: datasource=github-releases depName=kubevirt/kubevirt
KUBEVIRT_VERSION="${KUBEVIRT_VERSION:-v1.9.0}"
# renovate: datasource=github-releases depName=kubevirt/containerized-data-importer
CDI_VERSION="${CDI_VERSION:-v1.66.0}"

KV_BASE="https://github.com/kubevirt/kubevirt/releases/download/${KUBEVIRT_VERSION}"
CDI_BASE="https://github.com/kubevirt/containerized-data-importer/releases/download/${CDI_VERSION}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

# The KubeVirt CR is namespaced and virt-operator only reconciles the one that
# sits alongside it. The upstream manifest hardcodes `kubevirt`, so a CR
# rendered anywhere else is silently inert — it applies cleanly and nothing
# ever acts on it.
if [[ "$NAMESPACE" != "kubevirt" ]]; then
  echo "FAIL: NAMESPACE must be 'kubevirt' — virt-operator ignores a KubeVirt CR"
  echo "      in any other namespace, and the failure is silent."
  exit 1
fi

# ------------------------------------------------------------------------------
# Pre-flight: does this cluster actually have hardware virtualization?
# ------------------------------------------------------------------------------
# Without /dev/kvm, KubeVirt installs perfectly and then every VM either refuses
# to schedule or falls back to pure software emulation (which is ~10x slower and
# has to be explicitly enabled). Checking now beats debugging a Pending VM later.
#
# Before virt-handler has ever run there is no devices.kubevirt.io/kvm resource
# to look at, so on a first install this reports "unknown" rather than failing —
# the post-install verification below is the one that has real data.
echo "==> Pre-flight: KVM capacity"
KVM_NODES="$(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.allocatable.devices\.kubevirt\.io/kvm}{"\n"}{end}' 2>/dev/null | awk '$2 != "" && $2 != "0"' | wc -l)"
TOTAL_NODES="$(kubectl get nodes --no-headers | wc -l)"
if [[ "$KVM_NODES" -eq 0 ]]; then
  echo "    no nodes advertise devices.kubevirt.io/kvm yet (expected on a first install)"
else
  echo "    ${KVM_NODES}/${TOTAL_NODES} nodes advertise KVM"
fi

# ------------------------------------------------------------------------------
# Stage 1 — upstream operators
# ------------------------------------------------------------------------------
echo "==> Applying KubeVirt operator ${KUBEVIRT_VERSION}"
kubectl apply -f "${KV_BASE}/kubevirt-operator.yaml"

echo "==> Applying CDI operator ${CDI_VERSION}"
kubectl apply -f "${CDI_BASE}/cdi-operator.yaml"

echo "==> Waiting for operators"
kubectl -n kubevirt rollout status deployment/virt-operator --timeout=300s
kubectl -n cdi        rollout status deployment/cdi-operator --timeout=300s

# The CRs below are instances of CRDs the operator manifests just created. On a
# first install the API server may not have finished serving them yet, and helm
# would fail with "no matches for kind".
echo "==> Waiting for the CRDs to be established"
kubectl wait --for=condition=Established --timeout=120s \
  crd/kubevirts.kubevirt.io crd/cdis.cdi.kubevirt.io

# ------------------------------------------------------------------------------
# Stage 2 — our custom resources
# ------------------------------------------------------------------------------
echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" --cleanup-on-fail

# virt-operator now deploys virt-api / virt-controller / virt-handler and
# reports progress on the CR. This is the slow part of a first install (it pulls
# an image onto every node for the virt-handler DaemonSet).
echo "==> Waiting for KubeVirt to converge (first install pulls images on every node)"
kubectl -n kubevirt wait kv/kubevirt --for=condition=Available --timeout=900s

echo "==> Waiting for CDI to converge"
kubectl wait cdi/cdi --for=condition=Available --timeout=600s

# ------------------------------------------------------------------------------
# Verify
# ------------------------------------------------------------------------------
"${HERE}/verify.sh"
