# Registry

The private OCI registry is `registry.zachd.duckdns.org`, with its 200Gi iSCSI
volume preserved. It requires its existing 1Password-managed Basic authentication
for pushes and pulls. Anonymous `/v2/` access returns 401.

## Transport and containment

Pushes use HTTPS through Traefik. Traefik verifies the registry backend against
the internal CA, including its server name. Node containerd pulls use the pinned
`https://10.43.200.10:5000` mirror with CA verification. No insecure-skip-verify or
plaintext pull path remains. The public hostname is retained as an authenticated
HTTPS fallback. Keep the Service IP fixed.

The registry runs as UID/GID 1000 with all capabilities dropped, no privilege
escalation, RuntimeDefault seccomp, a read-only root filesystem, no API token and
no egress. Only its blob volume is writable. Ingress policy admits Traefik,
workspace maintenance clients and node sources. Flannel host-to-pod traffic uses
node tunnel addresses, so the policy includes those individual /32 addresses,
not the pod CIDR. Review `nodeConfig.overlaySourceCIDRs` when adding nodes.

## Node configuration

The permanent privileged hostPID DaemonSet and its RBAC are removed.
`apply-node-config.py` creates one short-lived, token-free privileged maintenance
Job at a time, writes root-only registry configuration/trust, restarts k3s only
if content changed, and verifies both authenticated TLS and a real CRI pull.
Jobs are deleted in finally blocks, have a ten-minute deadline and TTL cleanup.
No registry secret is passed in command arguments or stored in a Pod spec.

This is explicit administration, not a continuously running node agent. After
adding/rebuilding a node, run the upgrade to configure it before relying on
private image pulls. Offline nodes must return Ready before the all-node apply.
Other host profile installers use the same short-lived helper.

## Deploy and rotate

`./upgrade.sh` resolves `op://homelab/infra-registry` into memory, provisions TLS,
applies the chart, tests an anonymous rejection and an authenticated push, then
updates and verifies every node sequentially. A k3s restart can close its exec
stream; the idempotent operation retries and requires an explicit success marker.
A failed service restart restores the previous files before retrying.

The same existing credential is used by node pulls and developer pushes.
Rotating it also requires updating developer Docker logins with
`docker login --password-stdin`. The internal CA, 90-day certificates and daily
renewal are managed by [BuildKit's PKI](../buildkit/README.md). Certificate renewal
restarts this registry; CA rotation additionally needs every node's trust updated.

## Storage

Keep the existing 200Gi capacity; do not shrink it during upgrades. Tag deletion
is enabled, but deleting a manifest does not reclaim blobs until registry garbage
collection runs. GC requires a deliberate stopped/read-only maintenance window;
normal deployment does not delete images or data. Incomplete uploads expire after
a week. Successful pulls/pushes are quiet; authentication failures remain logged.
