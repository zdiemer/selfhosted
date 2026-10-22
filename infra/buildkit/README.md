# Shared BuildKit

Rootless BuildKit v0.32.2 serves authenticated clients at
`tcp://buildkitd.buildkit.svc.cluster.local:1234`. Talaria's three image builds,
workspace builds and ARC workflows use this service; no per-app privileged
builders are needed. Clients forward registry credentials per build session.

Run `./upgrade.sh` to provision TLS, install the node's security profiles and
apply the chart. A short-lived, token-free administrative Job installs profiles;
it is deleted afterwards and has a ten-minute deadline. The builder stays on
zachd-ubuntu-4 with a 100Gi userns cache PVC and 16Gi memory limit. Four
simultaneous build steps and bounded cache GC prevent unconstrained parallelism.

## Client authentication

The TCP listener requires a certificate signed by the internal build CA. Plain
TCP, clients without certificates and untrusted certificates are rejected.
NetworkPolicy additionally restricts callers to the workspace and ARC namespaces.
No application namespace gets access merely because it runs an app built here.

Workspace: `scripts/buildkit-client.py` exports credentials to
`~/.config/buildkit/tls` (0700 directory, 0600 files). The managed `buildctl`
wrapper reads `/var/run/buildkit-client` when mounted, and refreshes exported
credentials for existing workspace pods. Other clients can use:

```
buildctl --tlsdir ~/.config/buildkit/tls --addr tcp://buildkitd.buildkit.svc.cluster.local:1234 debug workers
```

Buildx remote-driver options are `cacert=.../ca.crt`, `cert=.../tls.crt`, and
`key=.../tls.key`. ARC mounts a separate client certificate in each repository
namespace at `/var/run/buildkit-client`. Workflows must pass those three options.
Talaria's setup script does this automatically. The Service remains internal.

## Containment and limits

AppArmor/seccomp are named local profiles, not Unconfined. They permit rootless
namespaces, mounts, runc keyring/hostname operations and namespaced device setup;
ptrace/process_vm_readv/process_vm_writev/kcmp remain denied by seccomp. Ubuntu's
unprivileged-userns restriction is restored to 1 on the build node: only the
explicit AppArmor profile grants this workload user namespace creation.

Build steps now get a separate PID namespace and procfs: the unsafe
`--oci-worker-no-process-sandbox` flag is removed. The pod uses `hostUsers: false`
with `procMount: Unmasked`, retaining the rootless image and UID 1000 daemon.
Kubernetes maps the pod's 65,536 IDs onto a distinct high host range. Neither
the daemon nor the helper init container runs as host root. No privileged
container, host namespace, hostPath or Kubernetes API token is granted. The
namespace still enforces baseline admission (restricted audit warnings for
required helper capabilities/procfs are expected).

RootlessKit's stock subordinate range (100000..165535) lies outside the pod's
range. `buildkit-subids` instead maps build root to pod UID 1000, build IDs
1..1000 to pod IDs 0..999, and 1001..65535 directly. Images using IDs above 65535
are unsupported and fail instead of widening host access. The init container
copies only newuidmap/newgidmap into an emptyDir, without their image xattrs,
and sets mode 4755 owned by pod root. This avoids the stock file-capability
failure under nested user namespaces. The daemon mounts these helpers read-only;
its bounding set is limited to SETUID, SETGID and SETFCAP. Privilege escalation
is still needed for these helpers, within the pod user namespace. The init has
all capabilities dropped, no escalation and a read-only root filesystem.

This follows the procfs/userns requirements described in
[BuildKit rootless documentation](https://github.com/moby/buildkit/blob/v0.32.2/docs/rootless.md)
and [Kubernetes user namespaces](https://kubernetes.io/docs/concepts/workloads/pods/user-namespaces/),
with named AppArmor/seccomp retained. It requires runtime/filesystem support for
idmapped mounts; validated on this node's k3s 1.35.9 and kernel 7.0.0-30.

This closes direct process visibility/signaling between RUNs and the daemon.
It is still a shared daemon, cache, pod network, memory budget and kernel, not
per-repository VM isolation. Builds can still exhaust shared resources or exploit
BuildKit/kernel bugs. Only trusted repositories should use this service.

## Cache migration and verification

`buildkit-userns-cache` is the active claim. The old `buildkit-cache` is retained
and rendered by the chart through `cache.legacyClaimName` for rollback; its
roughly 86GB of data was not modified. On-disk subordinate ownership changed,
so the old snapshot store must not be reused or recursively chowned for the new
mapping. New builds initially have a cold cache. There is sufficient local disk
for both claims; the old claim can be retired separately after the rollback
window. Changing the helper mappings again requires another cache review.

Run `python3 infra/buildkit/verify-isolation.py` from an authenticated workspace.
It verifies the worker's `sandbox` mode, RUN PID 1, daemon procfs invisibility,
concurrent build separation, compilation, UID 65534, secret/cache mounts, output
and cache reuse. `python3 -m unittest discover -s infra/buildkit/tests -v` checks
the rendered isolation contract and complete, non-overlapping UID mappings.
The upgrade script checks for active builds before changing anything (a
best-effort check, not a lock), uses atomic Helm rollback, and checks worker mode.

Validated 2026-10-22, Helm revision 17: disposable canaries passed on emptyDir
and the new PVC before rollout. The production service passed the remote tests;
six canary steps remained cached after its pod was replaced by production,
confirming idmapped persistence across pod allocations. A client without a
certificate was rejected with `certificate required`. A tiny scratch image was
pushed and pulled through the authenticated registry with its payload checked:
`registry.zachd.duckdns.org/zdiemer/buildkit-isolation-probe:20261022`.
All canary Jobs, pods and temporary ConfigMaps were removed. The production
Service/TLS identity and client configuration did not change.

Rollback to Helm revision 16 restores the old pod configuration and old cache,
including its former no-process-sandbox exception. Retain the old claim until
that rollback path is no longer needed; do not delete production storage as part
of a verification run.

Outbound traffic is limited to DNS, public HTTP/HTTPS and the registry's Traefik
endpoint. LAN, tailnet, metadata, loopback and multicast ranges are excluded from
the public rule. Kubernetes NetworkPolicy does not isolate a pod from its own node.

## Certificates and recovery

`files/internal-pki.py` is the canonical issuer; `scripts/internal-pki.py` invokes
it. The CA private key exists only in `buildkit/internal-build-ca`, never in the
builder, registry or client mounts. Leaf certificates last 90 days. The daily
05:17 UTC renewal CronJob renews leaves below 30 days remaining and restarts a
server only when its certificate changes. That maintenance can interrupt active
builds. Client Secret mounts update automatically; exported laptop credentials
must be refreshed. The CA lasts five years and requires planned rotation; renewal
fails loudly with 120 days remaining instead of silently replacing trust.

The renewal account can only read the CA, patch named TLS Secrets, list namespaces,
and restart the two named servers. New client namespaces require initial issuance
and a matching scoped Role during an admin upgrade. Back up the CA Secret with
restricted access; losing it requires reissuing clients and distributing new
registry trust to all nodes. The registry credential remains in 1Password.

Build the renewal image from `pki-image/Dockerfile`, push it, and pin its digest in
values.yaml. The scheduled job has no external network access. For a controlled
client-only rotation test, set `PKI_RENEW_CLIENT` to an existing client namespace.

The CA was backed up and round-trip verified in the homelab 1Password document
`infra-build-pki-ca`. With an authenticated `op` session, run
`python3 scripts/build-pki-recovery.py backup` to verify that copy. For cluster
recovery, create the `buildkit` namespace and run
`python3 scripts/build-pki-recovery.py restore` **before** infrastructure upgrades.
The helper refuses to overwrite a different live CA or vault backup. Leaf keys
can then be reissued; node trust remains valid because the CA is preserved.
