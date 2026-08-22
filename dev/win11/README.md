# win11 — a Windows desktop as a Kubernetes object

A general-purpose Windows 11 VM: UEFI + SecureBoot + a persistent vTPM, root
disk as a raw zvol on the NAS, RDP exposed as a Service. Nothing in the chart
is tied to a particular use — `dev/guacamole` puts it in a browser, or point a
native RDP client at it.

Requires `dev/kubevirt`.

## The one part that isn't declarative

Microsoft publishes no stable, unauthenticated Windows 11 ISO URL, so the
installer image is **uploaded once by hand**. Everything after that — the disk,
the firmware, the TPM, the VM — is declared in this chart.

Because of that the VM ships **stopped** (`runStrategy: Halted`). A fresh VM
with a blank disk and no ISO boots to a UEFI shell and sits there.

## Install

```bash
./upgrade.sh                                    # creates the VM (stopped) + DataVolumes
```

The `virtio` DataVolume imports itself from upstream. The `installer` one is
created empty in CDI's `upload` state and waits — `upgrade.sh` says so if it is
still waiting.

**1. Push the ISO** (needs `virtctl`, and the cluster reachable):

```bash
virtctl image-upload dv win11-installer \
  --namespace dev \
  --size 12Gi \
  --image-path ~/Downloads/Win11_24H2_English_x64.iso \
  --insecure
```

**2. Start it and open the console:**

```bash
virtctl start win11 -n dev
virtctl vnc   win11 -n dev
```

**3. Load the storage driver during setup.** Windows Setup will show **no
disks** — it has no virtio driver and cannot see the root disk. Click *Load
driver*, browse to the virtio CD, and pick `viostor\w11\amd64`. The disk
appears; install normally.

**4. After Windows is up**, install the rest of the drivers from the same CD
(`virtio-win-guest-tools.exe`) — that is what gives the guest a working NIC.

**5. Detach the installer** so every boot stops offering the DVD:

```yaml
# values.local.yaml
disks:
  installer:
    attached: false
vm:
  runStrategy: Always
```

```bash
./upgrade.sh && virtctl restart win11 -n dev
```

## Turn on RDP in the guest

Nothing can RDP in until Windows allows it — the Service will have endpoints
and the connection will still be refused. In an elevated PowerShell:

```powershell
Set-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -Value 0
Enable-NetFirewallRule -DisplayGroup 'Remote Desktop'
```

## Why the settings are what they are

**Windows 11 hard requirements.** The installer refuses with a bare "This PC
can't run Windows 11" unless it gets all three: UEFI (`bootloader.efi`),
SecureBoot (`efi.secureBoot` **plus** `features.smm` — SMM is what makes
SecureBoot legal), and a TPM 2.0 (`devices.tpm`). The vTPM is `persistent: true`
so Windows keeps its keys instead of re-provisioning them every boot; that needs
`vmStateStorageClass` set in `dev/kubevirt`, and `upgrade.sh` refuses to run if
it isn't.

**`machine: q35`.** The default i440fx has no PCIe and no SMM, so SecureBoot
cannot work on it.

**Boot order: disk first, CD second.** UEFI tries the empty disk, finds no
bootloader and falls through to the ISO — so the install boots from CD, and
every boot after Windows is written goes straight to disk with no "press any
key" prompt.

**`volumeMode: Block` + `ReadWriteMany`.** Block hands the zvol straight to
QEMU instead of putting a disk file on a filesystem. RWX is what makes live
migration possible — verified on this cluster: democratic-csi binds an
RWX+Block claim on `truenas-iscsi`. Drop to RWO and the VM still runs fine, it
just stops instead of moving when a node is drained.

**The root disk is a plain PVC, not a DataVolume.** The obvious shape — a
`dataVolumeTemplate` with a `blank` source — fails on this cluster: CDI's
importer runs unprivileged and cannot open a raw block device from
democratic-csi's iSCSI driver (`blockdev: cannot open /dev/cdi-block-volume:
Permission denied`). Filesystem-mode imports are unaffected, which is why only
this one broke. The import was pointless anyway — a `blank` source spends a pass
zeroing 128Gi that a fresh sparse zvol already reads as zeros, and Windows Setup
formats it regardless. The side effect is that **deleting the VM no longer
deletes its disk**, which for a pet VM is the safer default;
`helm.sh/resource-policy: keep` makes it explicit.

**`cpu.model: host-model`, not `host-passthrough`.** Passthrough is faster and
is the only way to expose nested virtualization, but it pins the guest to one
CPU. This cluster is mixed Intel *and* AMD, and no setting migrates a guest
across that boundary — `host-model` at least keeps migration working within a
vendor. To run Hyper-V or WSL2 *inside* the guest, add the `vmx`/`svm` feature
(see `values.yaml`), accepting that it confines the VM to that vendor's nodes.

**A USB tablet.** Absolute pointer positioning. Without it the browser console
uses a relative mouse and the guest pointer drifts away from yours — the most
common "the web console is unusable" complaint.

**Hyper-V enlightenments.** Windows detects it is a guest and stops doing the
things that are expensive under virtualization. Several percent of a core on an
idle desktop.

**`terminationGracePeriodSeconds: 300`.** The pod default of 30s is far too
short; Windows would still be shutting down when the kubelet SIGKILLs it, and
the next boot runs a repair.

## Backups

VM disks are excluded from k8up (`k8up.io/backup: "false"` on every PVC this
chart creates) — restic cannot walk a raw block device. Use
`VirtualMachineSnapshot` instead; see `dev/kubevirt/README.md` §Backups.

## Day-to-day

```bash
virtctl start|stop|restart win11 -n dev
virtctl vnc win11 -n dev            # QEMU console — works pre-boot, and during install
virtctl console win11 -n dev        # serial
kubectl get vmi -n dev              # which node is it on
kubectl -n dev get vm win11 -o jsonpath='{.status.restartRequired}'
```

`helm upgrade` never restarts a running guest — KubeVirt applies spec changes on
the *next* boot. `upgrade.sh` tells you when the running VM has drifted from the
declared one.
