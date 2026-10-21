# Shared BuildKit

Rootless BuildKit v0.32.2 serves authenticated clients at
`tcp://buildkitd.buildkit.svc.cluster.local:1234`. Talaria's three image builds,
workspace builds and ARC workflows use this service; no per-app privileged
builders are needed. Clients forward registry credentials per build session.

Run `./upgrade.sh` to provision TLS, install the node's security profiles and
apply the chart. A short-lived, token-free administrative Job installs profiles;
it is deleted afterwards and has a ten-minute deadline. The builder stays on
zachd-ubuntu-4 with its existing 100Gi cache PVC and 16Gi memory limit. Four
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

BuildKit still requires setuid UID-map helpers and `--oci-worker-no-process-sandbox`
with this rootless Kubernetes configuration. Builds share a daemon and can affect
its availability. This service is for trusted repositories; mTLS does not make
arbitrary third-party Dockerfiles safe or provide per-repository VM isolation.
Rootless permissions do not eliminate shared-kernel vulnerabilities.

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
