# Mac Mini for private macOS CI and development VMs

Status: requirements agreed; hardware not purchased. Plan only. Select a used
Apple Silicon Mac Mini with 16 GB RAM, targeting up to $400, in or near San
Francisco. Do not buy hardware or change workflows as part of this plan.

## Scope and decisions

- GitHub Actions for trusted private repositories: compilation, unit/GUI tests,
  packaging and eventually signing/notarization. Start unsigned; Apple Developer
  enrollment and release credentials are not set up yet.
- Native Apple Silicon builds/tests plus Intel builds and Rosetta compatibility
  tests are acceptable initially. Rosetta tests are not native Intel validation.
- One persistent development/testing VM plus one queued CI worker. Keep normal
  CI separate from the development desktop so both can operate concurrently.
  Persistent workers are acceptable; choose worker placement after resource tests.
- GPU/GUI-dependent jobs may reserve the physical host desktop while the
  development VM remains available. Detailed emulator E2E scenarios are outside
  this plan; provide the infrastructure for that subsequent work.
- Headless Ethernet operation with shell and browser desktop access restricted
  to Tailscale. Integrate provisioning, monitoring and recovery with this repo.
- External SSD and NAS are acceptable. Selected NAS test fixtures may be read
  by runners but must not be uploaded with CI artifacts.
- Occasional physical recovery after an outage is acceptable. Do not weaken
  encryption or enable automatic login just to promise unattended recovery.

## Repository inventory

Observed in the existing sibling checkouts; refresh against remote default
branches before implementation. Related worktrees are not necessarily separate
repos or separate migration targets.

| Repository | Existing work and migration |
|---|---|
| romnas | macOS matrix disabled because no self-hosted Mac was available. Python 3.11, uv, PySide6, pytest, lint/type checks and CLI smoke tests. Existing GUI tests run offscreen; restoring that matrix alone is not interactive GUI validation. |
| vxp | Hosted macOS matrix with .NET 8 build/tests. SDL frontend is not opened by current CI. Replace the macOS runner target once the equivalent tests pass locally. |
| chrono-trigger-recomp | Hosted macOS Clang build with Ninja/SDL2 and Python. Related movies/remainders checkouts have similar workflows; deduplicate by repository identity. |
| unicorn | Intel/ARM macOS build and packaging workflows are present. Establish whether this checkout/fork is an intended maintained target before migrating inherited jobs. |
| zodemu | Existing release workflow is Windows-only. macOS packaging/testing is new work, not a runner-label replacement. |

Start acceptance with Romnas and vxp. Inventory other intended repositories
before expanding runner access. Keep the existing
[Windows/Linux runner cutover](runner-cutover-vxp-zodemu.md) separate.

## Hardware shortlist and purchasing

These are configuration priorities and buying limits, not claims that a listing
is currently available. Price RAM ahead of internal storage capacity.

| Candidate | Decision |
|---|---|
| M1 Mini, 16 GB, 256 GB or larger | Preferred budget target at or below $400. External SSD can hold active VM disks when internal capacity is tight. |
| M2 Mini, 16 GB, 256 GB or larger | Prefer over an equivalent M1 if the inspected unit fits the same ceiling. |
| Newer Apple Silicon Mini, at least 16 GB | Consider only a verified deal within budget; no need to spend more merely for a newer chip. |
| 8 GB models or Intel Mini | Not the agreed primary target: prioritize 16 GB and native ARM capability. Revisit only with the owner. |

Research on 2026-10-23 found an [SF OfferUp M1 16 GB listing at $350](https://offerup.com/item/detail/eb6906d4-c397-35ff-b84a-6f1a560aa7a9),
but its detail page says **SOLD**. It is a price reference, not an actionable
recommendation. No available qualifying local unit was verified in this review.
Search SF/Bay Area listings and reputable shipped used/refurbished stock again
when buying. Confirm total tax, shipping and any external-storage expense before
purchase; the owner accepted external storage but did not set a separate budget.

Before paying, verify the exact chip/RAM/storage in System Information, usable
ports and Ethernet, physical condition, erase/setup capability and absence of
Activation Lock or organizational enrollment. Confirm the machine can complete
online setup without a prior owner's account or Remote Management requirement.
Prefer a return window when inspection is limited. Do not substitute a seller's
model-name claim for the actual configuration.

## Host, VM and storage layout

The Mini runs macOS as a separately managed host, not a macOS guest on the Linux
K3s nodes. The cluster can provide monitoring, secrets, backups and the access
portal without being the Mac's hypervisor.

1. Provision a minimal host with SSH, Tailscale, selected macOS/Xcode versions,
   and a reproducible VM manager using a supported Apple Silicon virtualization
   path. Select the VM tool after checking resource use, remote control,
   snapshots/clones, pricing and compatibility; do not assume an existing license.
2. Create a persistent development VM and a separate persistent CI VM if the
   16 GB host supports both acceptably. An initial measurement budget could be
   4 GB development + 6 GB CI, leaving 6 GB for the host; this is a trial, not a
   promised adequate allocation. Queue one CI job and cap build parallelism.
3. If two guests create memory pressure, keep the development VM and run the
   queued CI worker in a dedicated host account. Preserve separate workspaces,
   credentials and runner scheduling. Do not silently put CI in the user's VM.
4. Use a separately scheduled host GUI runner for jobs needing physical graphics.
   Do not advertise a VM as equivalent to host GPU rendering. Validate GUI
   session/login requirements before enabling unattended GUI jobs.
5. Keep active VM disks and build caches on local/internal or external SSD by
   default. Use NAS for selected fixtures, backups and retained outputs. Running
   active VM disks over NAS requires separate I/O and disconnect testing.
6. Back up provisioning, VM configuration and non-reproducible development data.
   Quiesce guests or use a supported consistent backup method; copying a live VM
   disk is not a verified restore. Caches are rebuildable. Test an isolated restore.

## Architecture and OS coverage

Use explicit runner labels for host architecture and capability; an ARM runner
executing an x86_64 process under Rosetta remains an ARM host. Separate native
ARM, translated Intel and packaging results. Verify artifact slices and dependency
architectures; changing a label does not create a universal binary.

[Apple's Rosetta guidance](https://support.apple.com/en-us/102527) says general
Intel-app translation is available through macOS 27, with narrower game-specific
support starting in macOS 28. Therefore select and maintain a compatible test
OS and revisit Intel coverage before OS upgrades; do not assume Rosetta is a
permanent replacement for Intel hardware. Future native Intel validation would
need a separate machine/service and owner approval.

[Apple's Tahoe compatibility list](https://support.apple.com/en-us/122867)
includes M1 and newer Minis, not the 2018 Intel Mini. Check the selected OS,
Xcode, SDK and deployment-target matrix against each repository before purchase
and before upgrades. No single OS version is promised to test every old target.

Apple documents [macOS guests on Apple Silicon](https://developer.apple.com/documentation/virtualization).
Review the selected OS license when choosing the guest layout. For example,
[Sequoia's license](https://www.apple.com/legal/sla/docs/macOSSequoia.pdf)
permits up to two additional virtual instances for specified purposes on an
Apple-branded computer already running the software; this is not an unlimited
VM pool or permission to transfer that layout to the Linux cluster.

## Remote access and recovery

- SSH over Tailscale for host administration and guest management.
- Browser access through the existing [Guacamole service](../dev/guacamole/README.md)
  is preferred. Prototype authenticated macOS Screen Sharing/VNC compatibility
  with the deployed guacd version before promising it. Test reconnect, keyboard,
  display sizing and access to both the host and development guest.
- [Guacamole's connection documentation](https://guacamole.apache.org/doc/gug/configuring-guacamole.html)
  is the reference for transport/authentication configuration. Do not expose a
  raw desktop port publicly or disable authentication to make a client connect.
- Validate the route from guacd to the Mac/guests explicitly. The portal being
  on the tailnet does not automatically make every guest reachable.
- Test power-loss recovery, FileVault unlock requirements, login-dependent GUI
  services, external-disk mounting and VM startup. Record the manual recovery
  steps; physical intervention is acceptable.
- Monitor host reachability, runner online state/queue age, guest health, disk
  headroom, memory pressure and backup freshness. Distinguish an offline runner
  from a healthy Mac whose guest or login session has not started.

## Rollout sequence

- [ ] Purchase and inspect an agreed configuration; inventory the host and storage.
- [ ] Provision macOS, Tailscale, SSH, toolchains and a tested browser desktop path.
- [ ] Validate development VM and CI coexistence with measured RAM/I/O pressure.
- [ ] Register scoped runners for intended trusted repositories. Prevent arbitrary
  external pull-request code from automatically executing on persistent workers.
- [ ] Serialize jobs across repositories when they share a worker or host GUI.
  Multiple repo-specific runner processes must not defeat the one-job budget.
- [ ] Restore Romnas macOS coverage, including its Mac-specific dependencies,
  and migrate vxp only after passing the current test suite on the new target.
- [ ] Add other agreed repos; remove hosted Mac legs only after equivalent
  coverage is confirmed. Keep workflows recoverable if the home runner is offline.
- [ ] Add selected read-only NAS fixtures; exclude them and credentials from
  uploaded artifacts/logs. Keep build outputs separate from fixture directories.
- [ ] Verify a reboot, recovery of runner/desktop access and a backup restore.
- [ ] Later: enroll in Apple Developer services and add signing/notarization with
  isolated keychain/secret handling and explicitly controlled release jobs.

Done when private Mac builds/tests run reliably on the queued worker while the
separate development desktop remains usable, dual-architecture results accurately
state their coverage, and remote access/recovery/backup have been exercised.
Signing is a tracked later phase, not a prerequisite for initial CI cutover.
Move lasting operating instructions to the implementation directory when shipped.
