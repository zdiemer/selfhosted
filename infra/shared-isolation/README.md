# Shared infrastructure network isolation

`./upgrade.sh` deploys namespace-wide policies for the first reviewed shared
services. Existing application isolation is managed separately.

| Namespace | Allowed egress | Ingress |
| --- | --- | --- |
| renovate | Cluster DNS, public IPv4 HTTP/HTTPS | Denied |

The namespace denies other egress, including LAN, tailnet, metadata and IPv6.
Renovate uses HTTPS GitHub repositories and public package registries; private
registries or SSH clones need an explicit policy change. No production Renovate
run is triggered during verification, avoiding unsolicited dependency PRs.

The API addresses in values.yaml must follow control-plane replacements.
Policies are additive; another policy can grant access. Kubernetes also exempts
some resident-node traffic, so these are not kernel or host-network boundaries.
See the [Kubernetes NetworkPolicy documentation](https://kubernetes.io/docs/concepts/services-networking/network-policies/).

`python3 verify.py` creates credential-free, non-root temporary probes on the
same worker in each namespace plus an unrestricted control in infra. It checks
API VIP/backend, NAS HTTPS, Authelia, GitHub/npm HTTPS, DNS and
inbound connections, then removes all probes. On 2026-10-21, positive and
negative controls passed.
Metadata timed out in the unrestricted control too: its exclusion is verified
in the policy, but that timeout does not independently prove enforcement.

Storage/VM operators and remaining infra
services still need their own dependency review. This release intentionally
started with Renovate; subsequent coverage is described below.

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
`arc-runners` namespace is excluded. Alloy monitoring is covered below.

CI validation on 2026-10-21: all seven namespaces passed allowed/denied
connectivity tests; the controller/listeners retained API access and active
GitHub long-polls. An authenticated `buildctl debug workers` from arc-talaria
with its existing client Secret succeeded. Immediate-start probes initially
received connection refused; allowing ten seconds after pod creation succeeded,
consistent with asynchronous network-policy propagation. No workflow build or
image push was triggered. All temporary probes were removed.

## Alloy monitoring

Three policies isolate the entire `alloy` namespace and grant different egress
permissions to the collector and blackbox instances. Unrecognized workloads
get only DNS and access to Alloy's metrics port. Ingress is limited to TCP
12345 from the same namespace, preserving self-scrapes and upgrade validation.

Collectors can reach the API, public HTTPS for Grafana Cloud, exact node IPs
on 9100/10250, and the listed exporter pods on their individual metrics ports.
Health exporters include Claude's messaging sidecar and Arr sidecars on 9105;
this does not grant their application/admin ports. The blackbox instance gets
public HTTP/HTTPS, the SMS health service, cluster-status and KeeWeb. Its
existing Minecraft TCP relay probe is preserved only to 147.182.206.23:25565;
validation does not connect to Minecraft or wake the server. Neither role
gets general LAN, tailnet or metadata access, and the blackbox role cannot
reach the Kubernetes API or node metrics.

Update `monitoringNodes` when nodes change and the explicit target rules when
adding scrape targets. Host-network agents and resident-node exceptions remain
outside full NetworkPolicy containment. Public HTTPS is not hostname-filtered.

Validated 2026-10-21: ten collectors and one prober ready; 72 configured scrape
targets healthy (including blackbox exporter scrapes, which do not imply every
probed application is healthy); fresh accepted remote-write timestamps on all
11 pods. All ten collectors' accepted sample counters increased after policy
enforcement, with no increase in Loki drops or remote-write failures during
the observation window. Collector/prober connectivity matrices passed and an
outside-namespace metrics connection was rejected. Temporary Jobs were removed.

All ten kubelet certificates also passed cluster-CA and IP-SAN validation.
`infra/alloy/values.yaml` now enables TLS verification; all ten live kubelet
scrapes remained healthy after reloading. Verification uses Alloy's
[internal metrics](https://grafana.com/docs/alloy/latest/reference/http/) and
component target health, not just pod readiness.

## CrowdSec

Agent and LAPI policies select only `k8s-app=crowdsec` pods of their respective
type, preserving independent backup and maintenance Jobs. Agents can reach DNS,
public HTTPS (hub downloads/enrichment) and LAPI TCP 8080. LAPI can reach DNS,
public HTTPS (central API/console) and the notification relay TCP 8000. Neither
gets Kubernetes API, LAN, tailnet or metadata access. Metrics TCP 6060 admit
only Alloy collectors; LAPI TCP 8080 admits agents, Traefik and cluster-status.

Live checks on 2026-10-21 verified the allowed/denied connection matrix, agent
LAPI authentication, central API authentication and existing console enrollment,
registry TLS/authentication, fresh Traefik bouncer polling, and continued parsing.
Temporary test Jobs were removed. Notifications were not sent as tests.

K8up operator, worker and database-dump policies are owned by the K8up chart;
see [backup isolation and recovery validation](../k8up/README.md).

## KEDA

The entire KEDA namespace now denies ingress and limits egress to cluster DNS
and the API VIP/backends. The operator additionally reaches only Talaria's
Redis pods on TCP 6379; the metrics API server reaches only the operator on
TCP 9666. Admission TCP 9443 and the metrics API TCP 6443 admit the three
control-plane hosts and their exact Flannel host source addresses. Update
`controlPlaneSources` when replacing control planes. No general internet,
LAN, tailnet, metadata or other application access is granted. Future scaler
backends require an explicit dependency rule. API/RBAC powers and Kubernetes'
resident-node exceptions remain outside this boundary.

Validated 2026-10-21: all three roles passed DNS/API and role-specific positive
checks and denied public HTTPS, NAS, unrelated applications and the other
role's dependency. Metadata was unreachable, but prior unrestricted controls
also could not reach it, so this is not independent enforcement evidence.
All twelve real external metrics returned values, all five ScaledObjects/HPAs
retained working metrics, and a duplicate-target server dry-run was rejected
by the KEDA admission webhook. This last check matters because deployed
webhooks have failurePolicy Ignore: a successful valid dry-run alone cannot
prove webhook reachability. No scaler bounds or policies were changed.

Temporary verification Jobs must remain unready: KEDA Services select the same
`app` labels as these policies. Initial ready probes briefly entered Service
endpoints and caused a metrics request failure; they were removed and checks
rerun with failing readiness probes to exclude them from service traffic.
All verification Jobs were removed afterward.

## Storage controllers

`storage-controller-boundary` selects democratic-csi's iSCSI and NFS controller
pods by name and CSI role. It denies ingress and permits only DNS, Kubernetes
API VIP/backends and the configured TrueNAS IP on HTTPS TCP 443. Controller
sidecars communicate with the driver through Unix sockets. Node-side NFS and
iSCSI mounts do not need controller pod access to ports 2049 or 3260.

This does not contain the privileged, host-network node plugins, reduce the
controller's Kubernetes permissions, or restrict what its NAS credential can
do through the allowed API. NAS API clients now verify the explicitly trusted certificate and IP identity;
see [NAS TLS trust](../democratic-csi/README.md#nas-api-tls-trust). Host-level
containment remains separate work.

Use `infra/democratic-csi/verify.sh --class <class>` with a fresh `NS` per class
for live validation: it provisions a disposable volume, writes/fsyncs a payload,
snapshots it, restores and compares the contents, expands it, and cleans up.
Never run the smoke test in an existing application namespace.

Validated 2026-10-21: both storage classes passed provision, write/fsync,
snapshot, restore, content comparison and expansion from 1Gi to 2Gi. The
controller-label probe could reach DNS, API VIP/backend and NAS HTTPS; public
HTTPS, unrelated apps, tailnet SSH and NAS NFS/iSCSI ports were blocked.
Metadata was unreachable, with the same control-test limitation noted above.
The Windows VMI kept its original UID, Ready state and guest-agent connection;
all 72 Alloy scrape targets remained healthy. No storage plugin was restarted.

## CDI control components

The `cdi` namespace permits only DNS and Kubernetes API egress by default.
The admission/aggregated API accepts TCP 8443 from the exact control-plane host
and Flannel source addresses. The upload proxy additionally reaches CDI upload
server pods in `dev` on TCP 8443; clients continue to use authenticated
`kubectl port-forward`, as documented in `dev/win11`. There is no general pod
access to the upload proxy or general internet/LAN/NAS access from CDI.

Import/upload workers run in the destination namespace, outside this control
component boundary. Supporting another upload namespace requires an explicit
rule. Worker isolation is part of the remaining VM networking review.
The upload target label/port are from the deployed
[CDI v1.66.0 source](https://github.com/kubevirt/containerized-data-importer/blob/v1.66.0/pkg/common/common.go).

Validated 2026-10-21: all four control roles passed allowed/denied connection
checks; the admission webhook rejected an invalid two-source DataVolume. A
disposable archive DataVolume reached UploadReady, received a real API-issued
upload token, accepted an authenticated upload through the proxy, and reached
Succeeded. A separate read-only mount verified the exact uploaded payload.
Temporary probes, DataVolume and test volumes were removed. All four CDI
deployments, Windows and all 72 Alloy scrape targets remained healthy.

## Remaining workstreams

Planning inventory as of 2026-10-22, after host containment, BuildKit isolation and logical database restore drills:

1. [Provider IaC](../provider-iac/README.md): independent DigitalOcean,
   Cloudflare DNS and Tailscale OpenTofu stacks prepared and locally validated.
   Live inventory/import, firewall apply and full-policy grant hardening remain
   pending provider credentials; no provider changes deployed yet.
   [Completion checklist](../provider-iac/FINISH.md).
2. [Stronger Cloudflare origin identity](../whatnow-droplet/ORIGIN-IDENTITY.md):
   proposed zone-specific authenticated origin pulls; needs Cloudflare SSL API
   access but can proceed independently of DO/Tailscale adoption.

Deferred by owner on 2026-10-22: [droplet recovery rehearsal](../droplet-recovery/README.md).
Quarantine runbook and clone database verifier are prepared; provider-backup restore
and clean-host rebuild remain untested. Deferred does not mean verified.

Logical database restore validation is complete for the selected latest stored
dumps: Paperless, Rachel, RomM and Vōcard passed isolated full restores and
engine checks; Talaria was verified previously. [Results and limits](../k8up/recovery/README.md)
distinguish these logical dumps from SQLite copies, application files and
full application recovery.

Owner decision (2026-10-22): offsite cluster backups are not planned because
of cost. The owner accepts that cluster data and NAS-hosted backups share a
failure domain. Droplet backups are already enabled; testing their recovery
remains on the list. This decision removes offsite storage from the backlog,
not from the residual-risk description.

Shared [BuildKit process isolation](../buildkit/README.md) is complete: rootless
BuildKit now uses pod user namespaces and per-RUN PID namespaces, with live
remote builds, cache persistence and registry push/pull verified. The shared
daemon/kernel/resource budget remains a trust boundary for approved repositories.

The [host-containment baseline](../host-containment/README.md) now covers all ten
nodes, CSI sidecars/mount exceptions, virt-handler permissions, host metrics
and infrastructure listeners. Required CSI/VM host privilege remains explicit;
removing it would require architectural changes, not another security-context
flag. The baseline is not a guarantee against compromise of these trusted agents.

Access repair completed 2026-10-22: `zachd-ubuntu-laptop-6` was reauthenticated.
Its key now expires 2027-04-20 01:16:18 UTC. Tailnet SSH works, Kubernetes
reports Ready, and the host guard service and nftables table remain active.
[Expiry notifications](../tailscale-expiry/README.md) are now deployed on the
primary host: private ntfy reminders at 7d, 3d, 2d, 1d and 12hr, plus expiry.
They cover devices visible in that host's tailnet inventory.

These are workstreams, not three confirmed exploitable vulnerabilities or an
exhaustive audit result. New dependency findings can change the list. Existing
application exceptions, including Jellyfin media writes, remain intentional.


## KubeVirt

The namespace boundary denies inbound traffic and allows only DNS and Kubernetes
API egress by default. API server hosts and their exact Flannel source IPs can
reach virt-api 8443, operator admission 8444 and template API/webhooks 9443.
Virt-api can reach handlers on console 8186 and VM statistics 8187. Handler
peers can reach each other on TCP: the deployed
[v1.9.0 migration proxy](https://github.com/kubevirt/kubevirt/blob/v1.9.0/pkg/virt-handler/migration-proxy/migration-proxy.go)
allocates random listener ports, so a fixed 49152/49153 pod rule would break
migration. This grant is restricted to handler labels within the namespace;
it does not grant access to arbitrary pods or node/LAN addresses. Migration TLS
remains enabled. Launcher communication otherwise uses local Unix sockets.

The export proxy can reach only labeled export servers in `dev` on TCP 8443.
No public export route exists; clients can use authenticated API port-forward.
New destination namespaces or monitoring scrapes require explicit rules.
The policies do not sandbox handlers' privileged host access, reduce Kubernetes
RBAC, or change VM guest network rules. The subsequent worker validation below exercises the authenticated export path.

Validated 2026-10-21: all seven component roles passed allowed/denied network
checks; all four aggregated APIs stayed Available. A Windows guest-agent API
request succeeded through virt-api and its handler. A disposable CirrOS VMI
with masquerade networking migrated from zachd-ubuntu-6 to zachd-ubuntu-5 with
its UID preserved. Its guest network was denied throughout. The first test's
default bridge interface was correctly rejected by migration admission and
replaced with the migratable test configuration. Windows retained its original
UID/node, Ready state and guest-agent connection; all 72 Alloy targets stayed
healthy. No production VM was migrated or restarted.


## VM import, upload, clone and export workers

In the current VM destination namespace `dev`, CDI-labeled workers get only
cluster DNS by default. Importers additionally get public IPv4 HTTP/HTTPS,
excluding private, LAN, tailnet and metadata ranges. Upload workers accept TCP
8443 only from the CDI upload proxy and same-namespace clone-source workers;
clone sources can reach only those upload workers on 8443. Export workers have
no egress and accept TCP 8443 only from KubeVirt's export proxy. NAS mounts are
performed by nodes, so workers need no direct NAS network access. These rules
leave Windows launcher and Guacamole policies unchanged.

This covers the currently used HTTP/registry imports, uploads and same-namespace
host-assisted clones. Private image sources, cross-namespace copies, new VM
namespaces and specialized VDDK/imageio imports need explicit dependency review
before use. Public egress is IP/port restricted, not hostname restricted.
Kubernetes API access is not granted to these data workers.


Validated 2026-10-21: all four worker-role egress matrices passed. A disposable
archive upload succeeded and its payload matched on a read-only mount. Its
VirtualMachineExport returned the exact same file through virt-exportproxy
with the export token and rejected an unauthenticated request. A public CirrOS
HTTP import completed, followed by a host-assisted `copy` clone from NFS to
iSCSI; the source and clone disk files compared byte-for-byte. All test
DataVolumes, exports, token Secret, pods and claims were cleaned up. Windows
retained its UID and Ready state; all 72 Alloy scrape targets stayed healthy.
