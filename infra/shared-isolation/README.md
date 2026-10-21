# Shared infrastructure network isolation

`./upgrade.sh` deploys namespace-wide policies for the first reviewed shared
services. Existing application isolation is managed separately.

| Namespace | Allowed egress | Ingress |
| --- | --- | --- |
| headlamp | Cluster DNS, Kubernetes API VIP and three control-plane endpoints on TCP 443/6443 | Existing dashboard access unchanged |
| renovate | Cluster DNS, public IPv4 HTTP/HTTPS | Denied |

Both namespaces deny other egress, including LAN, tailnet, metadata and IPv6.
Renovate uses HTTPS GitHub repositories and public package registries; private
registries or SSH clones need an explicit policy change. No production Renovate
run is triggered during verification, avoiding unsolicited dependency PRs.
Headlamp operations proxy through the API, including API-authorized access to
cluster resources. This policy does not reduce its cluster-admin credential's
powers or replace RBAC. Its existing LAN NodePort still requires a login token.

The API addresses in values.yaml must follow control-plane replacements.
Policies are additive; another policy can grant access. Kubernetes also exempts
some resident-node traffic, so these are not kernel or host-network boundaries.
See the [Kubernetes NetworkPolicy documentation](https://kubernetes.io/docs/concepts/services-networking/network-policies/).

`python3 verify.py` creates credential-free, non-root temporary probes on the
same worker in each namespace plus an unrestricted control in infra. It checks
API VIP/backend, NAS HTTPS, Authelia, dashboard, GitHub/npm HTTPS, DNS and
inbound connections, then removes all probes. On 2026-10-21, positive and
negative controls passed; the live dashboard remained healthy without restart.
Metadata timed out in the unrestricted control too: its exclusion is verified
in the policy, but that timeout does not independently prove enforcement.

Monitoring, ARC, CrowdSec, storage/VM operators, backups and remaining infra
services still need their own dependency review. This release intentionally
covers only Headlamp and Renovate.

## Shared CI

The shared-isolation release also isolates `arc-systems`: DNS, API VIP/backends,
and public HTTPS are its only egress paths; ingress is denied. ARC's controller
and listeners communicate through Kubernetes/GitHub, with no inbound webhooks
or enabled metrics listener in the deployed configuration.

Runner policies are owned by `infra/actions-runner`, generated from its repo
list so newly added repositories inherit the boundary. Each of the seven
current runner namespaces allows DNS, public HTTP/HTTPS, shared BuildKit TCP
1234 and Traefik's HTTPS listener for registry login. Other egress and all
inbound connections are denied. The BuildKit client certificate and registry
credentials are still required. The Traefik grant includes all routes on that
listener, not just the registry; route authentication remains necessary.

`python3 verify.py --ci` also checks the controller and every active runner
namespace, including BuildKit reachability and registry TLS/anonymous rejection.
It does not run repository workflows or publish images. The existing terminating
`arc-runners` namespace is excluded. Monitoring isolation remains pending.

CI validation on 2026-10-21: all seven namespaces passed allowed/denied
connectivity tests; the controller/listeners retained API access and active
GitHub long-polls. An authenticated `buildctl debug workers` from arc-talaria
with its existing client Secret succeeded. Immediate-start probes initially
received connection refused; allowing ten seconds after pod creation succeeded,
consistent with asynchronous network-policy propagation. No workflow build or
image push was triggered. All temporary probes were removed.
