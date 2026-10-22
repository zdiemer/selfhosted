# kubevirt — in-cluster virtualization

Runs virtual machines as Kubernetes objects. A VM is a `VirtualMachine`
resource, its disk is a PVC on the NAS, and it schedules onto whichever node
has room — the same lifecycle as everything else in this repo, applied to a
guest OS.

Two components:

| | what it does |
|---|---|
| **KubeVirt** | the virtualization layer itself: `virt-api`, `virt-controller`, and a `virt-handler` DaemonSet that runs QEMU on each node |
| **CDI** | Containerized Data Importer — turns an ISO or disk image (a URL, a registry image, an upload) into a PVC a VM can boot |

## Why this and not Proxmox

Proxmox is a hypervisor *OS*: it runs underneath a cluster, not inside one.
Adopting it here would mean wiping a node's Ubuntu install, putting Proxmox on
the metal, and re-joining k3s from inside a guest — trading a node to a rebuild
(which this cluster has already lost node-local systemd drop-ins to) for a web
UI.

KubeVirt inverts that. The disks land on `truenas-iscsi` like every other PVC,
`k8up`-adjacent tooling already understands the objects, VM placement is the
scheduler's problem, and the config lives in this repo next to everything else.
The cost is one operator.

## Prerequisites — verified on this cluster

| requirement | status |
|---|---|
| `/dev/kvm` + kvm modules on the nodes | present cluster-wide |
| `vhost_net`, `tun` | loadable; `virt-handler` loads them |
| kubelet root dir | `/var/lib/kubelet` — the standard path, so no k3s override |
| cgroup v2 | yes |
| RWO/RWX **block** storage for VM disks | `truenas-iscsi`; an RWX+Block claim binds |
| RWX filesystem for vTPM state | `truenas-nfs` |
| VolumeSnapshots | registered for both classes (`infra/snapshot-controller`) |

Nested virtualization is enabled on the hosts, so a guest can itself run
Hyper-V / WSL2 — see `dev/win11` values for the CPU-feature switch that
exposes it.

## Install

```bash
./upgrade.sh          # applies the pinned operators, then the CRs, then verifies
./verify.sh           # read-only; safe any time
```

`upgrade.sh` is a two-stage install because that is how KubeVirt ships: the
upstream operator manifests are applied with `kubectl` at a pinned version, and
this chart owns only the `KubeVirt` and `CDI` custom resources that tell those
operators what to deploy. The operator YAML is ~850KB of generated upstream
manifest — fetched by version, never vendored, so a bump is a one-line diff
instead of an unreviewable one.

Versions are pinned in `upgrade.sh` (`KUBEVIRT_VERSION`, `CDI_VERSION`) and
tracked by renovate.

## Feature gates

None. This is deliberate and was checked against the v1.9.0 source rather than
inherited from a tutorial — the gate list churns hard between releases:

- **Beta gates are on by default.** `Snapshot`, which backs
  `VirtualMachineSnapshot` and therefore the whole backup story, is Beta.
- **GA gates are always on.** `VMPersistentState` (persistent vTPM — a Windows
  11 requirement), `LiveMigration` and `ExpandDisks` are all GA in 1.9.
- `HotplugVolumes` is **deprecated** as of 1.9 in favour of
  `DeclarativeHotplugVolumes`; listing it earns a warning and nothing else.

Add a gate to `values.yaml` only for an Alpha feature you actively want.

## Live migration, and where it stops

`evictionStrategy: LiveMigrate` is set cluster-wide, so draining a node moves
running VMs instead of killing them. That matters more here than on most
clusters — nodes get rebooted and re-imaged routinely, and an unclean stop for a
VM is a guest filesystem repair, not a rescheduled pod.

Two constraints are worth knowing before you rely on it:

1. **The disk must be RWX.** A ReadWriteOnce volume cannot be held open by the
   source and destination `virt-launcher` at the same time, so the VM stops
   rather than moves. `dev/win11` defaults its root disk to RWX+Block for
   exactly this reason.
2. **No VM migrates between an Intel and an AMD host.** That is a QEMU/KVM
   constraint, not a KubeVirt one, and this cluster has both. In practice
   migration works among the Intel nodes or among the AMD nodes. See the
   `cpu.model` discussion in `dev/win11/values.yaml`.

## Backups

**`k8up` does not back up VM disks, and should not.** It takes filesystem-level
restic backups; a `volumeMode: Block` PVC has no filesystem to walk, and a
running guest's disk is a torn image anyway. k8up in this cluster is *opt-out*
(`skipWithoutAnnotation: false`), so every VM PVC carries
`k8up.io/backup: "false"` — `dev/win11` sets it via a helper on everything it
creates.

Use KubeVirt's own snapshots instead, which quiesce the guest and capture the
disks through the existing `truenas-iscsi` VolumeSnapshotClass:

```bash
kubectl apply -f - <<'YAML'
apiVersion: snapshot.kubevirt.io/v1beta1
kind: VirtualMachineSnapshot
metadata:
  name: win11-$(date +%Y%m%d)
  namespace: dev
spec:
  source:
    apiGroup: kubevirt.io
    kind: VirtualMachine
    name: win11
YAML
```

Restore with a `VirtualMachineRestore` against the same VM.

## Teardown

Not automated, on purpose.

⚠️ **Deleting the `KubeVirt` CR tears down every VM on the cluster.**
`virt-operator` treats it as the desired state of the entire install, so
removing it removes `virt-api`, `virt-controller`, `virt-handler` and every
running guest with them. `helm uninstall kubevirt` would do precisely that,
which is why `protectCRs: true` stamps `helm.sh/resource-policy: keep` on both
CRs — an uninstall then leaves the virtualization layer running and merely stops
managing it.

To actually remove it: delete the VMs, then the CRs, then the operator
manifests, in that order.

## Handler certificate compatibility workaround (KubeVirt 1.9)

KubeVirt 1.9 still starts a legacy migration client certificate manager at
`/etc/virt-handler/clientcertificates`, although the operator no longer mounts
that identity. This produced a missing-file error every minute on each handler.
The chart sets `customizeComponents.flags.handler.client-cert-file` and
`client-key-file` to the existing `migrationservercertificates` mount. The
operator continues to issue, rotate and project that migration certificate.
No private keys are copied or stored in Git.

Do not mount `kubevirt-virt-handler-certs` back into handlers: separating the
API client identity from migration clients addresses CVE-2025-64434. All local
handlers use the migration CN; this workaround does not restore compatibility
with old peers requiring the API client identity. Remove these flags when a
future KubeVirt version removes the legacy manager/flags, after reviewing its
source and release notes.

Changing these flags rolls the handler DaemonSet; running VM launcher pods
remain in place. Verify the new pod arguments, certificate reload logs, KubeVirt
availability and unchanged VMI UID/Ready condition. This is not a live-migration
test of Windows or its storage.

References:
- https://github.com/kubevirt/kubevirt/issues/16264
- https://github.com/kubevirt/kubevirt/blob/v1.9.0/cmd/virt-handler/virt-handler.go
- https://github.com/kubevirt/kubevirt/security/advisories/GHSA-ggp9-c99x-54gp

Verified on 2026-10-19 with Helm revision 2: all nine handlers rolled to Ready,
all four certificate managers in each handler loaded successfully, and no
missing-certificate errors recurred. `verify.sh` passed; Windows remained
Running/Ready with the same VMI UID throughout. Live migration was not invoked.

## Host trust boundary

`virt-handler` and its node-labeller init container remain privileged and share
host PID by upstream design. They manage host processes, devices and mount
namespaces; pod flags cannot turn them into untrusted application workloads.
The [host containment baseline](../../infra/host-containment/README.md)
documents this exception alongside the existing network isolation. Its
`verify-handler.py` uses an actual bound handler token and server-side dry runs
to verify own-node restrictions, forbidden node-spec changes and RBAC denials.
These checks do not alter live nodes or restart VMs.
