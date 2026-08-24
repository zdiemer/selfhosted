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

`cdi-uploadproxy` is a ClusterIP with no Ingress, and CDI does not publish an
external address for it — `kubectl get cdi cdi -o jsonpath='{.status.uploadProxyURL}'`
is empty. So virtctl has nothing to discover and the proxy URL has to be given
explicitly, over a port-forward:

```bash
kubectl port-forward -n cdi svc/cdi-uploadproxy 18443:443 &

virtctl image-upload dv win11-installer \
  --namespace dev \
  --no-create \
  --uploadproxy-url=https://127.0.0.1:18443 \
  --insecure \
  --image-path ~/Downloads/Win11_25H2_English_x64_v2.iso
```

`--no-create` because the DataVolume already exists — `upgrade.sh` made it, and
it is sitting in `UploadReady` waiting. `--size` belongs to the create path and
is not needed here.

Any Windows 11 x64 ISO works. Check which editions yours carries and make
`unattend.imageName` match one exactly, or Setup stops with "no images are
available":

```bash
7z l Win11.iso sources/install.wim     # or: wiminfo sources/install.wim
```

**2. Start it — and press a key in the first ~30 seconds.**

```bash
virtctl start win11 -n dev
virtctl vnc   win11 -n dev             # to press the key, then to watch
```

The one keystroke this chart cannot declare away. Microsoft's ISO boots through
a UEFI loader that prints **"Press any key to boot from CD or DVD"** and waits
about five seconds. The root disk is blank on a first install, so when that
prompt times out UEFI finds nothing bootable, prints `BdsDxe: No bootable
option or device was found`, and parks at the firmware menu — where it will sit
forever, looking like a hung VM rather than one waiting on a keypress.

Headless, `virtctl vnc --proxy-only --port 15900` exposes RFB on localhost and
any VNC client can send the key. Do not try to *time* the keypress off the
`vnc/screenshot` subresource — it lags the live framebuffer badly (observed
reporting 1280x800 while the RFB socket reported 640x480), so it will tell you
the prompt is up only after the window has closed. Send Enter every ~150ms from
the moment the VM starts instead; keystrokes before the loader are harmless.

Once Windows is on the disk this stops mattering: the disk boots first, and
with the installer detached (step 3) there is no CD to prompt from at all.

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
