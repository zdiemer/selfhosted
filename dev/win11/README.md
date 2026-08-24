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

Any Windows 11 x64 ISO works. Check which editions yours carries and make
`unattend.imageName` match one exactly, or Setup stops with "no images are
available":

```bash
7z l Win11.iso sources/install.wim     # or: wiminfo sources/install.wim
```

**2. Start it. That is the whole install.**

```bash
virtctl start win11 -n dev
virtctl vnc   win11 -n dev             # optional — to watch, not to click
```

`unattend.enabled` (on by default) hands Setup an `autounattend.xml` on a CD
that KubeVirt builds from a Secret. It partitions the disk, loads the virtio
storage driver, installs the edition named above, creates the local account,
skips OOBE, switches Remote Desktop on, and installs the virtio guest tools at
first logon. Nobody has to be watching.

Turn `unattend.enabled` off to install by hand instead — in which case Setup's
disk picker will be **empty** (no virtio driver), and you click *Load driver*
and point it at `viostor\w11\amd64` on the virtio CD.

**3. Detach the installer** so every boot stops offering the DVD:

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

The unattended install already does this (`unattend.enableRdp`, on by default),
so this section is only for a hand-built guest — or to check the setting.
Nothing can RDP in until Windows allows it: the Service will have endpoints and
the connection will still be refused. In an elevated PowerShell:

```powershell
Set-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -Value 0
Enable-NetFirewallRule -DisplayGroup 'Remote Desktop'
```

## The answer file

`templates/sysprep.yaml` renders `autounattend.xml` into a Secret, which
KubeVirt turns into a CD-ROM (`sysprep` volume source). Windows Setup finds it
by scanning removable media. A Secret rather than a ConfigMap because the local
account password is in it, and Windows Setup only accepts that as plaintext.

Three passes do three jobs:

| pass | what it fixes |
|---|---|
| `windowsPE` | loads the virtio storage driver — **without this Setup's disk list is empty** — then wipes and GPT-partitions the disk and picks the edition |
| `specialize` | names the machine, enables Remote Desktop, opens the firewall group |
| `oobeSystem` | skips the out-of-box screens, creates the local account, installs the virtio guest tools (the NIC) |

Every value interpolated into that XML goes through a `win11.xml` escaping
helper. That is not cosmetic: a password containing `&` or `<` produces a
malformed answer file, and Setup does not report that as an error — it ignores
the file and silently drops to the interactive installer, so an unattended
build becomes a machine waiting for a human.

The virtio CD's drive letter is not knowable in WinPE, so the driver path lists
`D:` through `G:` and the guest-tools command loops the same letters.

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
