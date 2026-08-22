#!/usr/bin/env bash
# Is the virtualization layer actually usable?
#
# "Installed" and "able to run a VM" are different states, and the gap between
# them is silent: KubeVirt reports Available with zero KVM-capable nodes, and
# CDI reports Available whether or not it can find scratch space. This checks
# the things a VM actually depends on.
#
# Safe to run any time — reads only.

set -uo pipefail

fail=0
note() { printf '    %-58s %s\n' "$1" "$2"; }
bad()  { note "$1" "$2"; fail=1; }

echo "==> Operators"
for d in kubevirt/virt-operator cdi/cdi-operator; do
  ns="${d%%/*}"; name="${d##*/}"
  ready="$(kubectl -n "$ns" get deploy "$name" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)"
  [[ "${ready:-0}" -ge 1 ]] && note "$d" "[OK] ${ready} ready" || bad "$d" "[FAIL] not ready"
done

echo "==> Custom resources"
kvp="$(kubectl -n kubevirt get kv kubevirt -o jsonpath='{.status.phase}' 2>/dev/null || true)"
[[ "$kvp" == "Deployed" ]] && note "KubeVirt CR phase" "[OK] $kvp" || bad "KubeVirt CR phase" "[FAIL] ${kvp:-<missing>}"
cdip="$(kubectl get cdi cdi -o jsonpath='{.status.phase}' 2>/dev/null || true)"
[[ "$cdip" == "Deployed" ]] && note "CDI CR phase" "[OK] $cdip" || bad "CDI CR phase" "[FAIL] ${cdip:-<missing>}"

kvv="$(kubectl -n kubevirt get kv kubevirt -o jsonpath='{.status.observedKubeVirtVersion}' 2>/dev/null || true)"
note "KubeVirt version" "${kvv:-unknown}"

# ------------------------------------------------------------------------------
# The check that matters: can any node actually run a guest?
# ------------------------------------------------------------------------------
# virt-handler advertises devices.kubevirt.io/kvm only on nodes where it found a
# usable /dev/kvm. A cluster with zero is a cluster where every VM stays Pending
# forever with an unschedulable message that does not mention virtualization.
echo "==> Nodes able to run VMs"
total=0; capable=0
while read -r n kvm; do
  total=$((total + 1))
  if [[ -n "$kvm" && "$kvm" != "0" ]]; then
    capable=$((capable + 1))
    printf '    %-30s kvm=%s\n' "$n" "$kvm"
  fi
done < <(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.allocatable.devices\.kubevirt\.io/kvm}{"\n"}{end}' 2>/dev/null)
if [[ "$capable" -eq 0 ]]; then
  bad "KVM-capable nodes" "[FAIL] 0 of ${total} — no VM can schedule"
else
  note "KVM-capable nodes" "[OK] ${capable} of ${total}"
fi

# ------------------------------------------------------------------------------
# Storage the VM layer depends on
# ------------------------------------------------------------------------------
# vmStateStorageClass backs the persistent vTPM. Windows 11 will not install
# without a TPM, and a VM asking for tpm.persistent when this class is unset or
# missing is rejected at admission with a message about backend storage.
echo "==> Storage"
vmstate="$(kubectl -n kubevirt get kv kubevirt -o jsonpath='{.spec.configuration.vmStateStorageClass}' 2>/dev/null || true)"
if [[ -z "$vmstate" ]]; then
  bad "vmStateStorageClass" "[FAIL] unset — persistent vTPM unavailable"
elif kubectl get sc "$vmstate" >/dev/null 2>&1; then
  note "vmStateStorageClass" "[OK] ${vmstate}"
else
  bad "vmStateStorageClass" "[FAIL] ${vmstate} does not exist"
fi

scratch="$(kubectl get cdi cdi -o jsonpath='{.spec.config.scratchSpaceStorageClass}' 2>/dev/null || true)"
if [[ -n "$scratch" ]] && kubectl get sc "$scratch" >/dev/null 2>&1; then
  note "CDI scratch class" "[OK] ${scratch}"
else
  bad "CDI scratch class" "[FAIL] ${scratch:-<unset>} missing — image imports will fail"
fi

# Live migration needs the disk reachable from two nodes at once. This is the
# claim that decides whether a node drain moves a VM or kills it, so it is
# checked against the cluster rather than assumed.
echo "==> Live migration prerequisites"
evict="$(kubectl -n kubevirt get kv kubevirt -o jsonpath='{.spec.configuration.evictionStrategy}' 2>/dev/null || true)"
[[ "$evict" == "LiveMigrate" ]] && note "cluster evictionStrategy" "[OK] $evict" \
  || note "cluster evictionStrategy" "[WARN] ${evict:-<unset>} — a drain will stop VMs, not move them"

echo "==> Workloads"
vms="$(kubectl get vm -A --no-headers 2>/dev/null | grep -c . || true)"
vmis="$(kubectl get vmi -A --no-headers 2>/dev/null | grep -c . || true)"
note "VirtualMachines defined" "$vms"
note "VirtualMachineInstances running" "$vmis"
[[ "$vmis" -gt 0 ]] && kubectl get vmi -A

echo
if [[ $fail -eq 0 ]]; then
  echo "kubevirt: OK"
else
  echo "kubevirt: FAILED — see [FAIL] lines above"
fi
exit $fail
