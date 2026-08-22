#!/usr/bin/env bash
# Apply the chart to the win11 release.
#
# WHAT THIS DOES NOT DO: restart the guest. KubeVirt treats a running VM as
# live state — editing the VirtualMachine changes the *next* boot, and the
# running VirtualMachineInstance keeps whatever it started with. That is the
# right default (nobody wants `helm upgrade` to yank a desktop out from under
# someone), but it means a spec change can look applied and not be. This script
# says so explicitly at the end rather than leaving you to notice.
#
# Restart deliberately, when the guest is idle:  virtctl restart <vm>

set -euo pipefail

RELEASE="${RELEASE:-win11}"
NAMESPACE="${NAMESPACE:-dev}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
VALUE_ARGS=(-f "$VALUES")
[[ -f "${HERE}/values.local.yaml" ]] && VALUE_ARGS+=(-f "${HERE}/values.local.yaml")

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

# A VirtualMachine is an instance of a CRD dev/kubevirt owns. Without it, helm
# fails with "no matches for kind VirtualMachine" — which reads like a chart
# bug rather than a missing prerequisite.
echo "==> Pre-flight"
kubectl get crd virtualmachines.kubevirt.io >/dev/null 2>&1 || {
  echo "FAIL: KubeVirt is not installed. Run dev/kubevirt/upgrade.sh first."
  exit 1
}
kubectl get crd datavolumes.cdi.kubevirt.io >/dev/null 2>&1 || {
  echo "FAIL: CDI is not installed. Run dev/kubevirt/upgrade.sh first."
  exit 1
}

# tpm.persistent is rejected at admission when the cluster has no backing store
# for VM state, and the message points at KubeVirt internals rather than at
# this. Catch it here.
VMSTATE="$(kubectl -n kubevirt get kv kubevirt -o jsonpath='{.spec.configuration.vmStateStorageClass}' 2>/dev/null || true)"
if [[ -z "$VMSTATE" ]]; then
  echo "FAIL: kubevirt.spec.configuration.vmStateStorageClass is unset."
  echo "      This VM asks for a persistent vTPM (Windows 11 requires a TPM),"
  echo "      which needs somewhere to keep its state. Fix in dev/kubevirt."
  exit 1
fi
echo "    vmStateStorageClass: ${VMSTATE}"

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

# Capture the running generation before the upgrade so we can tell afterwards
# whether what is running still matches what is declared.
BEFORE_GEN="$($K get vm "$RELEASE" -o jsonpath='{.metadata.generation}' 2>/dev/null || echo "")"
WAS_RUNNING="$($K get vmi "$RELEASE" -o jsonpath='{.status.phase}' 2>/dev/null || echo "")"

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" --cleanup-on-fail

echo "==> DataVolumes"
$K get dv -l app.kubernetes.io/instance="$RELEASE" 2>/dev/null || echo "    none"

# An installer DataVolume sitting in UploadReady is the normal state before the
# ISO has been pushed, and it is the single most likely reason a fresh VM will
# not boot. Say so plainly instead of letting it look like an error.
UPLOAD_PENDING="$($K get dv -o jsonpath='{range .items[?(@.status.phase=="UploadReady")]}{.metadata.name}{"\n"}{end}' 2>/dev/null | grep -c . || true)"
if [[ "${UPLOAD_PENDING:-0}" -gt 0 ]]; then
  echo
  echo "    ${UPLOAD_PENDING} DataVolume(s) are waiting for an upload — the VM cannot install yet."
  echo "    See README §Install:  virtctl image-upload dv ${RELEASE}-installer ..."
fi

echo "==> VirtualMachine"
$K get vm "$RELEASE" 2>/dev/null || true

AFTER_GEN="$($K get vm "$RELEASE" -o jsonpath='{.metadata.generation}' 2>/dev/null || echo "")"
if [[ -n "$WAS_RUNNING" && -n "$BEFORE_GEN" && "$BEFORE_GEN" != "$AFTER_GEN" ]]; then
  echo
  echo "    NOTE: the VM spec changed (generation ${BEFORE_GEN} -> ${AFTER_GEN}) while a"
  echo "          guest is running. The running VM still has the OLD spec."
  echo "          Apply it when the guest is idle:  virtctl restart ${RELEASE} -n ${NAMESPACE}"
fi

RESTART_REQUIRED="$($K get vm "$RELEASE" -o jsonpath='{.status.restartRequired}' 2>/dev/null || echo "")"
[[ "$RESTART_REQUIRED" == "true" ]] && echo "    KubeVirt reports restartRequired=true."

exit 0
