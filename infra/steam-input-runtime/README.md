# Steam runtime support

Run `./upgrade.sh` with cluster-admin credentials before deploying
`games/steam-headless`. This installs the node's persistent AppArmor and seccomp
profiles and two nonprivileged runtime helpers on zachd-ubuntu-6. The installer
uses the existing registry-node-config administration pod; it does not create a
new privileged workload. The AppArmor file is loaded immediately and by the host's
AppArmor service at boot. Reapply before moving Steam to a rebuilt node.

The device plugin advertises one `devic.es/steam-display` allocation containing
card1, renderD128, uinput, tty0 and tty7. Kubelet supplies container device nodes,
not a writable host `/dev` bind mount. The separate Jellyfin/RomM render plugin is
unchanged.

The NRI hook grants only character-device major 13 (input) to the two named
containers in the games namespace with the Steam label and exact opt-in
annotation. Dynamic controller minors need this rule. A fixed marker is injected;
both containers refuse to initialize without it. Steam's main container has no
MKNOD and mounts the virtual-input directory read-only. The input sidecar has
MKNOD/CHOWN but no host mounts or API token, and exposes only virtual input nodes.

Both node helpers are trusted infrastructure: NRI and kubelet's device-plugin
socket directory are powerful host interfaces even without privileged mode.
Their images are digest-pinned, root filesystems read-only, capabilities dropped,
API tokens disabled and network policies deny ingress/egress. Keep permission to
modify these workloads or impersonate the Steam deployment restricted to admins.
No helper exposes a non-loopback TCP listener.

`profiles/steam-headless.json` derives from containerd 2.2.7's generated default
profile for Steam's reduced capabilities, with clone/unshare/setns/mount/umount2/
pivot_root permitted for unprivileged user namespaces. AppArmor derives from
[containerd v2.2's Apache-2.0 template](https://github.com/containerd/containerd/blob/v2.2.0/contrib/apparmor/template.go),
retaining proc/sysfs denials while allowing userns/mount/pivot_root. Neither
profile bypasses the kernel's capability checks. Host mounts still fail without
SYS_ADMIN; a user's Bubblewrap namespace can mount its own sandbox. Default
masked/read-only proc paths remain. Review profiles when upgrading the runtime.

Build with `./build.sh` (BuildKit and registry credentials required), then copy the
reported digest into values.yaml. Go dependency checksums and scope tests are
checked in; the image build runs those tests. Deploy helper changes before Steam.
If the NRI helper is unavailable during container creation, Steam fails closed.
Existing containers retain their device rule if the helper restarts.

This setup shares the host kernel, GPU, uinput and virtual consoles. It reduces
containment privileges but does not provide VM-grade isolation or prevent a
compromised application from affecting shared device availability.
