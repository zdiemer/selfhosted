# Host infrastructure containment

This is the reviewed host-access baseline, not a claim that privileged storage
or virtualization agents are sandboxed against the host.

## Apply and verify

```bash
python3 infra/host-containment/apply.py --node zachd-ubuntu-6 # render only
python3 infra/host-containment/apply.py --apply --node zachd-ubuntu-6
python3 infra/host-containment/apply.py --apply
python3 infra/host-containment/verify-network.py
python3 infra/host-containment/verify-network.py --source-node zachd-ubuntu-6
python3 infra/host-containment/verify-ipv6.py
python3 infra/host-containment/verify-handler.py
python3 infra/host-containment/verify-workloads.py
```

The installer reads node addresses and per-node pod CIDRs from Kubernetes,
validates addresses, and installs a dedicated `inet selfhosted_host_guard`
nftables table atomically. It never flushes the ruleset or another table.
IPv4 and IPv6 drops run before the ordinary input filter, so later Tailscale or
Kubernetes ACCEPT rules cannot bypass them. Application/NodePort forwarding,
Storage data ports are unchanged; SSH and API access retain the existing LAN
IPv4/tailnet administrative boundary. This is a targeted
infrastructure input guard, not a default-deny host firewall or egress filter.

- SSH 22 and API 6443: LAN IPv4/tailnet and node IPv6 only; cluster pod
  interfaces also reach the API. Existing SSH/API authentication remains required.
- Etcd 2379/2380: only control-plane nodes and loopback.
- Old development listeners 3777, 8899, 8900: loopback only. These were
  user-session Next/Python processes on the primary node, outside Kubernetes.
  They were not killed; use an SSH tunnel if local development access is needed.
- Node-exporter 9100: loopback and this node's pod CIDR on `cni0` only. Existing
  pod egress policies further limit clients; traffic is not cryptographically
  authenticated and an unrestricted local pod could still scrape it.
- SMB health/metrics 29642–29644: loopback only. The controller also binds its
  metrics listener to loopback.
- Flannel VXLAN UDP 8472: cluster node IPv4/IPv6 addresses only. IP allowlists
  are not cryptographic identity and do not defend against trusted-node or
  same-L2 source spoofing.
- Kubelet 10250: nodes and cluster pod interfaces/CIDR only; kubelet's existing
  TLS/authentication and pod egress policies remain required.
- RPC port 111 TCP/UDP: nodes and NAS only.

The service is enabled at boot and k3s units require it before starting.
Existing services are not restarted by the final installer: it applies the
rules, uses `systemctl start`, and checks that k3s process IDs are unchanged.
**Do not restart/stop the guard unit:** systemd propagates a stop/restart to
requiring k3s units. Use `systemctl reload selfhosted-host-guard` for an existing
rules file. The first rollout accidentally used restart, briefly restarted k3s
services and caused scrape failures; the installer was corrected and monitoring
recovered. Running workload containers and the Windows VMI remained in place.

Reapply on all nodes whenever node IPs, IPv6 addresses or membership change.
This installation supports the current IPv4 pod network `10.42.0.0/16`; review
rules before enabling dual-stack pods or changing CNI interfaces/CIDRs. Install
on newly joined nodes before scheduling production workloads. No reboot was
performed to test boot ordering; service dependencies, activation and reload
are checked online.

For the laptop with an expired tailnet key, an explicit admin bootstrap is
available: `--apply --node zachd-ubuntu-laptop-6 --via-kubernetes`. It runs a
short-lived privileged Job in the node's namespaces, with no service-account
token, and deletes it in `finally`. It is not a permanent agent. Tailnet key
renewal is separate from the firewall installation.

Rollback only this guard with `nft delete table inet selfhosted_host_guard` on
the affected node. For persistent removal, remove the two `40-host-guard.conf`
k3s drop-ins first, run `systemctl daemon-reload`, then disable the guard unit
and remove its files. Do not stop the guard while the Requires dependencies
remain installed. Never flush nftables globally.

## Required host-access exceptions

| Component | Retained host access | Containment and rationale |
|---|---|---|
| democratic-csi node helpers | Privileged, host network, explicit kubelet/device/iSCSI paths | Kernel mounts and bidirectional propagation require privilege. No host IPC/PID. NFS drops the whole-host-root mount; iSCSI retains `/host` because its image wrapper chroots into the host storage client. Sidecars hardened. API controllers remain on pod networking with existing network policy. |
| SMB CSI helpers | Privileged, host network; nodes mount kubelet paths | Kernel SMB mounts and propagation. Sidecars hardened, metrics bound to loopback, host port guard. |
| virt-handler and node-labeller init | Privileged and host PID, explicit KubeVirt/kubelet paths | Host process/namespace and device management is integral to KubeVirt. Pod networking stays isolated by the KubeVirt policies. Bound-token node restriction and restricted RBAC were verified. |
| node-exporter | Host network, read-only `/proc`, `/sys`, `/` | Needed for host metrics. Nonroot, no capabilities, no privilege escalation, RuntimeDefault seccomp; no host PID; host port guard. |
| GPU/Steam device and input-runtime plugins | Explicit device-plugin, device and NRI paths | Existing nonprivileged containers have no service-account token, dropped capabilities, no privilege escalation and read-only root filesystems. NRI/device-plugin sockets still confer host runtime trust. |

A compromised mount helper or virt-handler can still compromise its node.
Removing that trust requires changing the storage/VM architecture or separating
these workloads onto dedicated nodes. Do not add ineffective seccomp/capability
claims to privileged containers: privilege overrides those restrictions.
Likewise, this baseline does not constrain a cluster administrator who can
create privileged pods. The workload verifier detects drift; it is not an
admission policy or an authorization boundary for service-account names.

KubeVirt's [security architecture](https://kubevirt.io/2020/KubeVirt-Security-Fundamentals.html)
and [installation requirements](https://kubevirt.io/user-guide/cluster_admin/installation/)
explain its trusted host agent. Its existing Fail/Deny node-restriction policy
was exercised with an actual bound pod token and server-side dry runs: own
KubeVirt label allowed; another node, cordon, unrelated label denied. Secret
listing, pod creation and ClusterRoleBinding creation were also denied by RBAC.
No live node metadata was changed by those tests.

## Validation record (2026-10-22)

All ten nodes received the guard. Final installer runs preserved k3s process
IDs. Peer tests from a control-plane node and a worker covered every node:
exporter blocked, kubelet/API reachable, etcd reachable only from a control-plane
source, and primary-node development ports blocked. A temporary IPv6 network
namespace provided a positive route control and confirmed drop-counter hits
for every protected TCP port group and UDP VXLAN. Its interfaces/namespace were
removed. The node's exporter was independently confirmed to listen on IPv6.

Both CSI classes passed provision/write/fsync/snapshot/restore/byte-compare and
1Gi-to-2Gi expansion after the iSCSI chroot correction. All 22 democratic-csi
pods verified NAS TLS. SMB fresh read-only mounts, real-token handler admission
checks and the live host-workload inventory passed. Both ten-node CSI
DaemonSets, ten SMB node plugins, nine virt-handlers and all 72 Alloy scrape
targets were healthy. Windows retained its original VMI UID, Ready state and
guest-agent connection. Test namespaces, PVs, reader Jobs and admin Jobs were
removed; no host reboot was performed.
