# vmlab

A browser VM picker for short-lived guests, at
[vmlab.zachd.duckdns.org](https://vmlab.zachd.duckdns.org).

Pick an OS, it boots in an ordinary pod, you close the tab and it is gone.
Adding one is a name and an ISO URL.

`dev/win11` is the other half of this story and is untouched by it: that is a
pet — a 128Gi zvol, a persistent vTPM, SecureBoot, an unattended answer file,
reached over RDP through `dev/guacamole`. This is cattle.

---

## The fact this is built on

KubeVirt's virt-handler advertises `devices.kubevirt.io/kvm` as a **plain
node-level extended resource**. It is registered through the ordinary kubelet
device-plugin socket — not namespaced, not restricted to KubeVirt — so any pod
in any namespace can request it and the plugin's `Allocate` injects `/dev/kvm`.

Verified on `zachd-ubuntu-5` before any of this was written:

```
$ kubectl -n vmlab exec diag -- sh -c 'id; ls -l /dev/kvm; grep Cap /proc/self/status'
uid=0(root) gid=0(root) groups=0(root)
crw-rw---- 1 root 993 10, 232 /dev/kvm
CapInh: 0000000000000000
CapPrm: 0000000000000000
CapEff: 0000000000000000
CapBnd: 0000000000000000
CapAmb: 0000000000000000
```

`/dev/kvm`, present, with **every capability set empty**,
`allowPrivilegeEscalation: false`, no privileged mode and no hostPath. That is
what makes a VM here an ordinary pod.

Requesting the resource is also most of the node filter for free: the three
nodes that advertise none (the control-plane workstation and the two cordoned
laptops) are excluded by the scheduler without a nodeSelector.

## Why not KubeVirt, given it is right there

- **x86 only.** `defaultArchitecture: amd64`, one qemu binary per
  virt-launcher. RISC OS is ARM, so a third of the wish list is inexpressible.
- **`useEmulation` is unset** in the live `KubeVirt` CR, so every guest needs
  real KVM — which removes the escape hatch.
- **Every guest costs a zvol**: a CDI DataVolume per ISO, image-sized scratch
  per concurrent import, a block PVC per VM, an NFS vTPM volume. All cost, no
  benefit, for a ten-minute guest.
- Its wins — live migration, `VirtualMachineSnapshot`, hotplug — are pet wins.

## Shape

```
Browser ─> Traefik ─> Authelia forwardAuth ─> vmlab (FastAPI + static UI)
                                                │
                            ┌───────────────────┼──────────────────┐
                       catalog ConfigMaps   creates Pods    reverse-proxies
                       (seed + user)        in ns vmlab     /vm/<sid>/ -> pod:8006
```

There is **no per-VM Ingress and no per-VM Service**. The UI proxies
`/vm/<session>/` — websocket upgrade included — straight to the session pod's
IP. One host, one certificate, and one auth check that every console sits
behind.

That works because of how noVNC builds its own URLs, which was measured rather
than assumed: every asset in the page is referenced *relatively*, and
`app/ui.js` sets `path = 'websockify'` then resolves it with
`new URL(path, location.href)`. A page served at `/vm/<sid>/` therefore asks for
`/vm/<sid>/websockify`, with no rewriting anywhere. Proved end to end through a
path-stripping proxy: page `200`, `app/ui.js` `200`, and the upgrade returning
`101 Switching Protocols` followed by the RFB handshake bytes.

**The trailing slash is load-bearing.** `/vm/<sid>` without it makes the browser
resolve `app/ui.js` against `/vm/`, so `main.py` redirects.

## The ISO cache

`BOOT=<url>` makes the *VM container* download its own ISO. That contradicts the
sandbox twice: Red Star OS must have zero egress but would need the internet to
fetch itself, and every launch would re-download.

So fetching is separated from running. A fetch `Job` — the only workload here
with internet egress — writes the ISO once into `vmlab-isos` (RWX,
`truenas-nfs`, `resource-policy: keep`). Session pods never touch the network.

Two things the image's source forced, both found by reading it:

1. **`BOOT` cannot be a path.** `install.sh` hard-rejects anything that is not
   `http(s)://` with `exit 64`. The actual hook is `findBootFile()`, the *first*
   statement in the dispatch, which scans `/` at maxdepth 1 for
   `boot.{iso,img,raw,qcow2}` and short-circuits before `BOOT` is read. So the
   ISO is mounted at `/boot.iso` and `BOOT` is left unset. This is also why the
   cache beats the image's baked-in `ENV BOOT="alpine"`.

2. **The mount cannot be read-only.** `disk.sh` reads the MBR signature
   (`head -c 512 | tail -c 2`) and force-attaches any **hybrid** ISO as a
   *writable* `usb-storage` disk, bypassing `MEDIA_TYPE` entirely; only
   non-hybrid ISOs take the `readonly=on,media=cdrom` path. Alpine and Bazzite
   are hybrid; Windows XP and TempleOS are not. A read-only mount therefore
   works for half the catalog and fails the other half with
   `Could not open '/boot.iso': Read-only file system`.

Hence an **initContainer stages a private copy** out of the cache into an
emptyDir. That fixes both, and has a dividend: the VM container never mounts the
shared cache, so a guest holds no handle on the ISOs other guests boot from.

Verified by booting Alpine **3.21** — the cached version — rather than the 3.24
the image would have downloaded.

## Save points

A save point is a QEMU internal snapshot: `savevm` writes RAM, device state and
a disk snapshot **into** the guest's qcow2. Loading passes `-loadvm <name>` at
boot, so the guest comes back mid-desktop instead of booting. Verified against
Windows XP: saved at Setup, stopped the pod entirely, loaded — and the resumed
framebuffer was **100% pixel-identical** to the saved one.

Each save point carries a screenshot, shown in the UI, so months later you can
tell "before the driver install" from "after" by looking.

### How it is wired

- **QMP over TCP**, not a sidecar. `qemux/qemu` has its own `QMP` variable, so
  pointing it at a port lets the vmlab pod drive the monitor directly — no
  extra container, no shared emptyDir, and no root-running sidecar (a unix QMP
  socket is created `0755` by root, which a non-root sidecar could not connect
  to anyway).
- **Screenshots pulled over VNC** by the vmlab pod and PNG-encoded in Python
  (zlib; no Pillow). QEMU can write a PNG itself, but only to a path inside the
  session pod — which would put shared writable storage back into a design that
  deliberately has none. Session pods do not mount the snapshots volume at all.
- The **security consequence is paid explicitly**: a reachable QMP port is total
  control of a VM, so session pods now *always* get an ingress policy — the
  `full` profile included, which previously had none. `full` means unrestricted
  *egress*; it never meant "anything in the cluster may drive this VM".

### What it costs, and why

Three things had to be given up, each found by watching a save fail:

| given up | why | who pays |
|---|---|---|
| `+invtsc` | `savevm` refuses non-migratable state: *"State blocked by non-migratable CPU device (invtsc flag)"*. Every node here has a stable TSC, so the image always adds it. | The guest loses an invariant TSC and falls back to a slower clocksource. |
| Hyper-V enlightenments (`HV=N`) | `hv_passthrough` mirrors the host CPU by construction and so cannot be serialised: *"'hv-passthrough' CPU flag prevents migration"*. | Nothing for XP — enlightenments target Vista and later. A modern Windows guest would lose some paravirtual acceleration. |
| `raw` disks | `savevm` stores state *inside* the image and raw has nowhere to put it, so save-point configs are `qcow2`. | Slightly slower I/O. Not switchable later: changing the format would not convert an existing disk. |

`savepoints: true` also implies `persist: true` — `validate_config` ANDs them,
because an emptyDir disk takes every save point with it when the pod dies.

### Restoring needs the same machine

`-loadvm` restores VM state onto the devices QEMU has **now**, so a save point
taken with the installer CD inserted can only be loaded with it inserted. The
manifest records `bootFromIso` per save point and `launch()` reproduces it. The
first version forced the ISO off when loading and every live-CD save point was
unrestorable.

Two neighbouring traps, both found the same way:

- **`BOOT=""` does not mean "no boot media".** `install.sh` reads empty as
  *unset* and falls back to the image's `ENV BOOT="alpine"` — so a guest meant
  to resume from its own disk quietly downloaded Alpine and attached it as a
  writable hybrid disk, which then broke the restore. The correct value is
  `none`: it short-circuits on a disk with data, and fails loudly on one
  without.
- **RAM must match exactly.** qemux/qemu silently allocates *less* than
  `RAM_SIZE` when `wanted + 500MB > available`, and a guest saved at 1024M will
  not reload after being given 962M. `vm.resources.overheadMemoryMib` is 768
  rather than 512 for this reason — it is derived from that 500MB spare, not
  picked.

### Driving a guest without a person in front of it

`app/vmlab/guestctl.py` types at a guest and looks at its screen, over the same
QMP and VNC paths the save points use:

```
python -m vmlab.guestctl shot|keys|type|click|wait <slug> ...
```

Run it inside the vmlab pod — NetworkPolicy admits nothing else to a session's
monitor or display. `wait` blocks until the screen stops changing, which is the
only completion signal an installer offers from outside.

Absolute pointer events go to whichever mouse QEMU has **active**, which is the
relative PS/2 device by default even when a USB tablet exists — so without a
`mouse_set` the pointer silently never moves at all.

**`click` is not reliable yet, and some guests ignore synthetic input
entirely.** Windows XP's OOBE ("Display Settings") accepts neither keys nor
clicks over QMP, despite the same keyboard driving every earlier XP setup
screen — so the last few dialogs of an XP install have to be done in the
browser. Elsewhere: Selecting the absolute device fixed "the
pointer never moves" but not "the click lands where asked", and calibrating it
by locating the drawn cursor does not work: diffing two framebuffers picks up
the old cursor being erased alongside the new one being drawn, so the position
cannot be read back. `keys`/`type` are the dependable path, and they carry most
installers; a screen that genuinely needs a pointer is quicker to click in the
browser console than to automate.

### Which guests can be saved with the CD in

`savevm` refuses while any **writable** non-qcow2 device is attached, and
`disk.sh` force-attaches *hybrid* ISOs as writable. So it splits by ISO type:

| plain ISO — saveable with the CD in | hybrid ISO — flattened, so also saveable | opted out — install first |
|---|---|---|
| Windows XP, Red Star, Hannah Montana, TempleOS | Alpine, ReactOS | Bazzite |

Hybrid ISOs are handled by clearing the two-byte MBR signature on the private
staged copy, which drops them onto the read-only CD-ROM path. **That rewrites
sector 0**, and two kinds of medium object:

- **Checksum-verified media.** Fedora-family ISOs carry an implanted
  `isomd5sum` over the whole image and verify it at boot; altering sector 0
  fails the check and dracut **halts**. (Bazzite.)
- **Media that boot *through* the MBR.** Haiku's "anyboot" image locates its
  boot volume via the MBR partition table, so zeroing the signature makes it
  panic with `did not find any boot partitions`.

Both set `flattenIso: false`, keep their real bytes, and must be installed to
disk before taking a save point.

Hannah Montana is the nicest case: a live CD, so one save point of the running
desktop means every later Load lands straight on it. When a save does hit this,
the error explains the fix rather than repeating QEMU's wording.

### A save point belongs to a machine

`-loadvm` restores register and device state onto whatever QEMU builds *now*,
so a config edited since is not loadable. Every save point records a
`hardware` fingerprint — a hash of only the fields that reach QEMU's command
line, so renaming a guest costs nothing while changing its chipset, RAM,
display adapter or sound is caught:

```
save point 'fp_test' was taken on different hardware and cannot be restored:
audio: False -> True. Delete it, or revert the config.
```

Before this, two save points went stale unnoticed and were found only when
loading them produced a QEMU error naming none of the config. Save points
predating the field still load, so it is additive.

### Live tiles

A running guest's catalog tile shows what it looks like *now* — captured over
VNC every `vm.liveThumbnailSeconds` by a loop deliberately kept separate from
the reaper, so a slow screenshot can never delay the cull that enforces TTLs.
Stored as `.live.png`, a name `NAME_RE` forbids, so it cannot collide with a
save point.

### Sound

`audio: true` adds an intel-hda device and streams it over the same page — the
image ships an audio relay and the viewer has an `/audio` websocket, which the
console proxy already forwards. It is off by default because adding the device
*changes the machine*, and would invalidate any save point taken without it.

### Reconciliation

QEMU is authoritative while a VM runs; the manifest on the snapshots volume is
the fallback when it is stopped, and entries are marked `observed` accordingly.
A save point QEMU no longer has is pruned. But output the parser cannot read is
**not** treated as "none" — `list_snapshots` raises instead, because returning
an empty list there once deleted a real save point and its screenshot.

## Fetching, and what a "URL" turns out to mean

The fetch Job unpacks by **magic bytes** — zip, gz, bz2, xz, zst, 7z — because
these projects publish whatever they like: `.iso.gz` (9front), `.iso.bz2`
(MINIX), `.7z` (KolibriOS), `.iso.zst` (Redox), `.zip` (FreeDOS, AROS,
Visopsys), and archive.org URLs often carry no extension at all. Unpacking runs
*after* the size and checksum checks, so integrity covers the fetched bytes.

Not every OS ships an ISO, so `bootMedia` (iso/img/raw/qcow2) controls the
staged file's extension — `install.sh` keys on it, and Visopsys's 1.44MB floppy
saved as `boot.iso` would simply be mis-detected.

Three guards, each added after something got through:

- **Size** must match `Content-Length`; a CDN that ignored a Range header made
  `curl -C -` append a retry and cache a 7.36GiB image as 10.98GiB.
- **Content-type** must not be HTML. An AROS mirror answered a missing file
  with `200` and a web page, which was cached as a 1.2KB "aros.iso" — the size
  check could not catch it because the size *matched*.
- **Checksum**, where the URL names an immutable artifact.

## Sandboxing

Two independent levers, both used:

| | `none` (default) | `internet` | `full` |
|---|---|---|---|
| `NETWORK=` | `N` — QEMU builds no NIC at all | `slirp` (user-mode) | `slirp` |
| capabilities | **none, empty set** | `SETGID`, `SETUID` | `SETGID`, `SETUID` |
| NetworkPolicy | deny all ingress and egress | no RFC1918, no tailnet, no cluster | none |

`NETWORK=N` means the guest is air-gapped whether or not any policy exists; the
NetworkPolicy contains the *container* around it. `internet` needs `SETGID` and
`SETUID` and nothing else — `slirp` starts dnsmasq, which drops group to `dip`.
Measured: **no `NET_ADMIN`, no `/dev/net/tun`, no privileged.**

Enforcement was verified, not assumed. kube-router's controller runs in-process
in the k3s agent, and a probe pod reached the internet (`301`) before being
labelled and was blocked on internet, cluster API **and a tailnet peer**
immediately after.

> **There is a ~1-2 second window at pod start** before kube-router programs the
> rules, measured: a probe pod polling every 2s saw `OPEN` once at t+0 and
> `blocked` for the following 29 samples. It is not theoretical — it is how a
> guest container once managed to download Alpine despite `network: none`.
>
> It does not reach the guest. `NETWORK=N` means QEMU builds no NIC at all, and
> QEMU starts long after the window closes, so an untrusted OS never has a path
> out regardless. What the window exposes is the *container's own* startup, which
> is why it matters that nothing in it wants the network — the ISO cache exists
> so that a guest never fetches anything at boot.

> The `internet` egress block excludes `100.64.0.0/10`. That is the tailnet's
> CGNAT range, and the six charts here that hand-roll
> `0.0.0.0/0 except RFC1918` (`dev/claude-workspace`, `infra/egress-proxy`,
> `finance/money`, …) all omit it — so those pods can currently reach every
> tailnet peer. Worth fixing there separately.

## Why this is allowed to create pods, when hatch is not

`infra/hatch/templates/rbac.yaml` refuses pod-create RBAC and says why: *"a
hatch that could create a pod could create one with any image and any
serviceAccountName."* That objection is correct and cannot be dodged here, since
creating pods is the entire point. It is answered in three layers:

1. **Scope** — a namespaced `Role`, bound in one namespace that contains
   nothing but the lab. No cluster-scoped grant of any kind.
2. **Quota** — `templates/quota.yaml` caps
   `requests.devices.kubevirt.io/kvm`, pods, cpu and memory. Enforced by the
   API server, so it holds even if every line of app code is wrong.
3. **Shape** — the pod spec is assembled server-side in `app/vmlab/spec.py`. A
   request contributes a slug, an ISO URL, RAM, cores, disk size and a profile
   name, all validated against allow-lists and clamped. It never contributes an
   image, a command, a securityContext, a serviceAccountName, a volume or a
   node selector.

Session pods run under a ServiceAccount bound to **nothing**, with
`automountServiceAccountToken: false`.

Authelia forward-auth is **on**, unlike `dev/guacamole`. Guacamole gates VMs
that already exist and has its own login behind it; this creates pods that hold
`/dev/kvm`, and tailnet-only is not a sufficient answer to that when the tailnet
includes laptops that roam. The cost is a click-through — the session cookie is
shared across `*.zachd.duckdns.org`.

## Two catalog ConfigMaps, on purpose

- `vmlab-catalog-seed` — Helm's, rendered from `values.yaml`, read-only in the
  UI.
- `vmlab-catalog-user` — created by the app, `resource-policy: keep`, **never
  templated by this chart**.

One ConfigMap would mean the next `helm upgrade` silently deleted every config
added from the browser — the same clobber that ate `values.local.yaml` and took
the Signal bot down. Two make that structurally impossible.

Note the usual helm trap applies to the seed list: **overriding `catalog` in a
values file replaces the whole list**, it does not merge. To add one VM, use the
UI.

## Lifecycle

Launch creates a `Pod` with `restartPolicy: Never` and `activeDeadlineSeconds`
(default 4h). That hard TTL belongs to the kubelet, so it holds even when this
Deployment is down. The reaper in `main.py` is only the softer "nobody is
watching" cull (default 30m), driven by a `vmlab.zachd/last-seen` annotation the
console websocket re-stamps.

Stop deletes the pod, and an `emptyDir` disk goes with it. `persist: true` swaps
that for a per-config PVC so an install survives — set on Windows XP and
Bazzite, never on Red Star.

For a persisted config the installer ISO is attached **only while the disk is
new**. `findBootFile()` short-circuits *before* the data-disk check, so leaving
it attached would re-run the installer on every launch forever.

## Operating

```bash
./build.sh      # bump image.tag + appVersion first; tags are immutable
./upgrade.sh    # pre-flights the kvm resource and the ISO storage class
```

A UI rollout does **not** disturb running guests — session pods are independent
Pods, not children of the Deployment. An open console websocket drops and noVNC
reconnects.

### Windows XP

Points at the Internet Archive's copy of the original MSDN volume-licence SP3
image, chosen over the several "pre-activated" repacks alongside it because it
is unmodified — eight independent uploads of this file all carry sha1
`66ac289ae27724c5ae17139227cbe78c01eefe40`, which a repack would not. Setup asks
for a product key; supply your own. XP remains under copyright.

`ReactOS` is also in the catalog: an open-source NT re-implementation, and the
one Windows-shaped thing here that can be fetched with no such caveat.

Every config except RISC OS now has an `iso` URL and is fetched on demand from
the UI. The cache holds Alpine, TempleOS, ReactOS, Hannah Montana, Red Star,
Windows XP and Bazzite — about 12GB of the 60Gi volume. Both archive.org images
were verified against the sha1 the Archive publishes for them.

Every other entry now has an `iso` URL and is fetched on demand from the UI.
The cache currently holds Alpine, TempleOS, ReactOS, Hannah Montana, Red Star
and Bazzite — about 11.9GB of the 60Gi volume. The two archive.org images were
verified against the sha1 the Archive publishes for them.

Staging an ISO by hand:

```bash
kubectl -n vmlab run stage --rm -it --image=alpine:3.22 --restart=Never \
  --overrides='{"spec":{"containers":[{"name":"stage","image":"alpine:3.22",
    "stdin":true,"tty":true,
    "volumeMounts":[{"name":"isos","mountPath":"/isos"}]}],
    "volumes":[{"name":"isos","persistentVolumeClaim":{"claimName":"vmlab-isos"}}]}}' -- sh
# then, from another shell:
kubectl -n vmlab cp ./winxp.iso stage:/isos/winxp.iso
```

A hand-staged ISO just appears — the UI reads the cache directory (mounted
read-only) rather than inferring state from Job objects, so nothing else is
needed.

That was not the first design, and the first one was broken: `iso_present()`
originally inferred "cached" from a succeeded fetch Job, to avoid mounting the
volume into the UI pod. But Jobs carry `ttlSecondsAfterFinished: 600`, so the
signal vanished ten minutes after every fetch, every ISO reverted to `absent`,
and `launch()` then refused to start anything — the lab broke itself on a timer.
The avoidance was also unnecessary: session pods never mount this volume at all
(an initContainer copies out of it), so it is not guest-writable and reading it
read-only costs nothing. Jobs are still consulted, but only for "is one on its
way", which is what they are actually good for.

## A second engine, for RISC OS

Every other guest here is `qemux/qemu`: one image, one ISO, a machine assembled
from validated flags. RISC OS is not, because **no QEMU machine type boots it**.

The obvious route looked good for a long time. `qemu-system-aarch64` carries
every Raspberry Pi machine — `raspi0`, `raspi1ap`, `raspi2b`, `raspi3ap`,
`raspi3b`, confirmed with `-M help` — which is real 32-bit ARM. Getting there
needed a patched qemux image, because upstream applies `virt`-only options to
whatever machine you name: `gic-version` and `msi` on the machine line, AAVMF
pflash, a tianocore `fw_cfg`, and PCI rng/balloon/xhci devices. That patch chain
worked, and QEMU came up clean on `raspi2b` with the official RISCOSPi 5.30 SD
image on the SD controller. The screen stayed black — because QEMU's raspi
machines never run the VideoCore bootloader, so the FAT boot partition is not
read at all, and `-kernel` does not rescue it either. RISC OS's own developers
have been at this since 2016 with the same result. `values.yaml` keeps the
citations; the raspi support was reverted.

So `engine: rpcemu` selects a different emulator entirely — RPCEmu, emulating
an Acorn Risc PC, which is hardware RISC OS has a HAL for. `spec.py` dispatches
to `_build_rpcemu_pod` rather than threading conditionals through the QEMU path,
and the two share nothing but containment: same no-permission service account
with no token, same dropped capabilities, same network label the sandbox
policies select on, same kubelet-enforced deadline. It is the one guest that
also runs as an unprivileged user, because unlike the qemux images this one was
built not to need root.

What it gives up, and why:

| | |
|---|---|
| No ISO | The engine image carries the ROM and boot disc as a matched pair, so `needsIso` is false and the tile launches straight away. |
| No `/dev/kvm` | It is a 32-bit ARM machine translated on an x86 host. Asking would also spend a concurrency slot from the quota. |
| No save points | Those are QEMU internal qcow2 snapshots driven over QMP. `validate_config` refuses them for this engine rather than letting the button fail. |
| `persist: true` instead | RISC OS keeps everything on HostFS, so a PVC is what makes it a machine rather than a demo. |

### Three things the image has to get right

Each of these was found by watching it fail, and each is commented where it
lives:

1. **Filetypes.** RISC OS has no filename extensions — a file's type is a number
   the filesystem holds beside it, and it is what makes a module loadable. ROOL's
   zip carries it in an Acorn extra field that Linux `unzip` discards. Extract
   HardDisc4 with plain `unzip` and RISC OS boots as far as `!Boot` and then
   fails every `RMEnsure` in it, because every module arrived as a text file.
   `unzip-riscos.py` restores them as the `,xxx` suffixes HostFS reads back.
2. **The CMOS.** A Risc PC keeps its boot filesystem in battery-backed CMOS, and
   the one RPCEmu ships is set to ADFS — a disc that does not exist here — so a
   fresh machine stops at the supervisor `*` prompt. `entrypoint.sh` types the
   manual's two `*configure` commands on the boot after a fresh seed, then
   restarts the emulator, because a Risc PC reads its CMOS at reset and nowhere
   else. It costs about a minute, once.
3. **How to type them.** `xdotool` sends nothing useful: with no window manager
   there is no focused window for XTEST events to reach. `configure-cmos.py`
   goes in over RFB instead, moving the pointer first — which is what hands the
   emulator focus — and the keys follow it.

This was originally done at image build time, and it worked. It was moved to
first boot because buildkit shares one network namespace across builds, so every
cancelled attempt left an `Xvfb` and an `x11vnc` behind and the next build hung
on its own ghosts for twenty minutes. In the pod the namespace is the pod's own.

## Notes on individual guests

| OS | why it needs what it needs |
|---|---|
| Windows XP | `machine: pc` (i440fx) — on the image's default q35, Setup dies with `STOP: 0x000000A5 ... not fully ACPI compliant` before drawing a screen; a save-point screenshot is how that was diagnosed. `bootMode: windows_legacy`, `diskType: ide` — no virtio drivers exist for it. Setup asks for a key; supply your own. `network: none` is not optional; an unpatched XP online is minutes to compromise. |
| ReactOS | The open-source NT re-implementation, and the reason it is here: it is the one Windows-shaped thing in this catalog that can be fetched from a URL legitimately. Boots to a text installer menu. Ships as a zip, which the fetch job unwraps. |
| TempleOS | BIOS, IDE, 1 core. Has no networking by design, so `none` costs nothing. |
| Red Star OS 3.0 | Ships file-watermarking and anti-tamper components. Air-gapped, always. |
| Hannah Montana Linux | Kubuntu 8.04. ~18 years of unpatched CVEs. |
| Bazzite | The one that strains the cluster: ~6GB ISO, 8Gi RAM against a measured ~9.9Gi free on the roomiest node. llvmpipe rendering, so sluggish. `persist: true`. |
| TempleOS | `machine: pc` + `mediaType: auto`. It probes the legacy IDE ports directly, and the image's `ide`/`sata` settings both give an ich9-AHCI controller instead — TempleOS faults into its debugger asking for a "CD/DVD I/O Port Base". Red Star needs the same, for a different symptom: SeaBIOS simply hangs at "Booting from DVD/CD". |
| RISC OS 5 | `qemux/qemu-arm`, TCG only — no KVM, so slow, and it does not consume a KVM quota slot. Best-effort; likely needs hand-tuned arguments rather than a plain ISO. |

## Capacity

Memory is the binding constraint cluster-wide, not CPU. At the time of writing:
`zachd-ubuntu-5` ~29 vCPU / ~9.9Gi free, `-4` ~15.7 vCPU / ~8.8Gi free, and
`-1` has 19Gi free but **0.4 CPU** and hosts win11. Guests prefer `-5` and `-4`
softly, and are kept off `zachd-ubuntu-laptop-2` by a *required* affinity term —
it advertises `kvm=1k` and carries **no taint**, only the label
`device-type=laptop`, so requesting the KVM device does not exclude it.

`kubevirt.io/ksm-enabled` is `false` cluster-wide. Turning KSM on would be the
single highest-leverage change for running several similar guests at once.
