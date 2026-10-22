# node-exporter — host metrics for every node

Values-only against the upstream
`prometheus-community/prometheus-node-exporter` chart, the same arrangement as
[`infra/crowdsec`](../crowdsec) and [`infra/alloy`](../alloy).

```
/proc, /sys, host NIC ──▶ node-exporter :9100 (DaemonSet, hostNetwork)
                                │
                                └──▶ Alloy scrape ──▶ Grafana Cloud
```

## Why this exists

The cluster had **no host-level metrics at all**. CPU, memory, disk fill and
temperature were visible only through [`infra/cluster-status`](../cluster-status),
which polls the kubelet for a live snapshot and keeps no history — so "was the
node swapping at 3am" had no answer.

More specifically, it is the only source for **traffic that never touches
Traefik**. [`infra/alloy`](../alloy)'s access-log pipeline is complete coverage
of external *HTTP* by construction, but three things bypass it entirely:

| Path | Why Traefik never sees it |
|---|---|
| Minecraft `:25565` | LoadBalancer TCP, via the VPS haproxy relay |
| Jellyfin `:30096` | NodePort, via the VPS Caddy relay |

Those are plausibly the *largest* byte flows in and out of this cluster, and
until now none of them were counted anywhere. `node_network_receive_bytes_total`
is where they show up.

## Why its own chart rather than Alloy's built-in exporter

Alloy ships `prometheus.exporter.unix`, which does the same job in-process and
would save a release. It would also mean giving Alloy `hostNetwork`
and hostPath mounts of `/proc`, `/sys` and `/` — and Alloy is deliberately
built the other way round, reading container logs through the Kubernetes API
*specifically* so it has no host bindings at all. Host access lives here, in
the chart whose whole job is host access.

## hostNetwork, and what it costs

`hostNetwork: true` is not optional. `/proc/net` resolves inside the reading
process's network namespace, so a pod on the cluster network reports **its own
veth** no matter how `/proc` is mounted — the netdev numbers would be real, and
about the wrong interface.

Port `:9100` is restricted by [the host guard](../host-containment/README.md)
to loopback and the local node's pod network. External LAN/tailnet clients
cannot reach it. Alloy scrapes from the local pod network; its own egress
NetworkPolicy remains in force.

If a node ever already has something on `:9100`, that pod CrashLoops with
`address already in use` — `upgrade.sh` compares desired-vs-ready and says so,
because one unready pod out of ten is easy to miss.

## The series budget is the design

Grafana Cloud's free tier caps **active series at 10k** for the whole stack.
Default node-exporter is ~600–800 series per node; ten nodes would be 6–8k,
most of the budget, on one exporter.

`--collector.disable-defaults` flips the model to opt-in, which is the version
that stays correct as upstream adds collectors. Ten collectors are enabled;
each has a one-line justification in `values.yaml`. Measured result: **~570
series exported per node, of which Alloy keeps ~180 — about 1.8k across ten
nodes.** `node_cpu_seconds_total` is over half of the kept set on its own
(cores x 8 modes), and stays, because iowait and steal explain a slow node when
the CPU average looks fine.

Two exclusions do most of the work, and both guard against *churn* rather than
just volume — series that are replaced by new ones on every pod restart burn
the budget with data nobody will ever query:

- **netdev** excludes `veth*`/`cali*`/`cni*`/`flannel*` — one interface per pod,
  renamed on every restart.
- **filesystem** excludes `/var/lib/kubelet/**` — one mount point per volume
  per pod, likewise.

`upgrade.sh` prints the live per-node series count after every deploy and warns
above 800 (against a ~570 baseline), so a chart bump that re-enables defaults
is caught here rather than on the Grafana Cloud usage page a week later.

## Deploy

```bash
./upgrade.sh
```

No secrets, no `values.local.yaml`. Nothing here is load-bearing for ingress —
a bad deploy loses node metrics, not traffic.

## Verify

```bash
# every node covered
kubectl -n infra get ds node-exporter-prometheus-node-exporter

# what a node is actually exporting
kubectl -n infra port-forward ds/node-exporter-prometheus-node-exporter 9100:9100
curl -s localhost:9100/metrics | grep -c '^node_'
```

```promql
# in Grafana Cloud, once Alloy is scraping it
count by (node) (node_cpu_seconds_total{cluster="home-k3s"})   # expect one row per node
rate(node_network_receive_bytes_total{cluster="home-k3s"}[5m]) # the Minecraft/Jellyfin blind spot
```

If `node_network_*` shows interfaces named `veth…`, `hostNetwork` is off and
the numbers are the pod's, not the node's.

## Container containment

The exporter runs as UID/GID 65534 with a read-only container filesystem,
all Linux capabilities dropped, privilege escalation disabled, and
`RuntimeDefault` seccomp. It does not join the host PID namespace. The ten
explicitly enabled collectors read the mounted host procfs without needing
host PID membership.

Host networking remains necessary for node NIC counters. Read-only host
`/proc`, `/sys` and `/` mounts also remain: this is still a trusted host reader,
not an application-grade sandbox. Removing host PID membership does not hide
host process information exposed by the procfs mount. Port 9100 uses no application authentication, but the
[host firewall](../host-containment/README.md) now restricts it to loopback and
the local pod network. Ordinary pod NetworkPolicies do not contain host-network
traffic; Alloy egress restrictions remain an additional layer.

Validated 2026-10-22 using a separate Job-owned canary on port 19101, followed
by the DaemonSet rollout: all ten collectors succeeded and CPU, physical NIC,
filesystem, disk and temperature metric identities matched the pre-change
baseline on every node. Recheck these when changing collectors or confinement.
