# coredns-config

Cluster DNS configuration overlay. Today it owns exactly one thing: a **gated DNS query log** that produces the cluster's egress inventory.

k3s installs and owns CoreDNS (a manifest Addon, not a HelmChart — `/var/lib/rancher/k3s/server/manifests/coredns.yaml`). This chart does not install anything and does not patch anything k3s owns. It writes a single ConfigMap into an extension point CoreDNS already reads:

```
.:53 {
    ...
    import /etc/coredns/custom/*.override    <- our file lands here
    forward . /etc/resolv.conf
}
```

The CoreDNS Deployment already mounts `coredns-custom` at `/etc/coredns/custom` with `optional: true`, so the ConfigMap is picked up simply by existing.

## Why

Every outbound connection starts with a DNS lookup, which makes CoreDNS the one place that sees **all** cluster egress — including from workloads deployed out of repos this one cannot read (whatnowgg, talaria). Traefik's access log is the equivalent complete record for ingress; there was no counterpart for egress.

The inventory this produces is what [`infra/egress-proxy`](../egress-proxy/)'s per-service allowlists get built from. Collection lives in [`scripts/egress-audit.sh`](../../scripts/egress-audit.sh).

## This is an audit instrument, not a pipeline

**`dnsLogging.enabled` defaults to `false` and is meant to spend most of its life there.** The `log` plugin writes a line per query; across ~150 pods that is plausibly several GB/month in Loki, against the 50 GB/month free tier that `infra/alloy`'s allowlist is carefully built to stay inside. Switch it on, collect for a day or two, switch it off.

The permanent, cheap counterpart is the egress-proxy access log — bounded by the number of opted-in services, and attributed by authenticated client name rather than by an ephemeral pod IP.

### `class success` is the volume knob

Kubernetes clients walk a search path, so `api.ipify.org` is tried as `api.ipify.org.<ns>.svc.cluster.local`, then `.svc.cluster.local`, then `.cluster.local`, and only then as written. The first three are NXDOMAIN. Four or five lines in five are a failed prefix of a lookup that succeeded.

Filtering those at the plugin costs nothing; filtering them in Alloy means paying to read and discard them. `success` keeps NOERROR only — exactly the set that means "this name resolved and something is about to connect to it".

## Applying this restarts cluster DNS

Read this before running `upgrade.sh`.

- The `reload` plugin watches the **Corefile**, not the files it imports. A change here is inert until CoreDNS restarts.
- CoreDNS runs **one replica with `maxUnavailable: 1`**, so the old pod is terminated before the replacement is ready. Every restart is a short, cluster-wide DNS gap.
- A Corefile that fails to parse means the replacement crashloops with the original already gone — cluster DNS stays down until the ConfigMap is removed.

`upgrade.sh` is mostly guard rails against that last point:

1. Verifies the import line and the volume mount still exist. Both are k3s's; a k3s upgrade that changed either would make this chart a silent no-op.
2. **Boots a throwaway pod on the real CoreDNS image with the rendered stanza and proves it parses**, before touching anything live. Deliberately a minimal server block rather than a copy of the whole Corefile — k3s's config is already known good, and the only new thing is our stanza. Verified to work in both directions: a bad `class` fails the pod with `plugin/log: invalid Class` and the script aborts having changed nothing.
3. Applies, then restarts CoreDNS **only if the content actually moved**.
4. Resolves an internal and an external name afterwards, and **rolls the ConfigMap back automatically** if either the rollout or the resolution check fails.

Do not apply this chart with a plain `helm upgrade`.

## The collector

Turning the log on only produces lines in a pod that rotates them away, so the chart also ships the thing that reads them. It runs **in the cluster, not in a terminal** — a measurement window is measured in days and a laptop is not.

It streams the CoreDNS log through the Kubernetes API, attributes each lookup, and writes an aggregate to a PVC. Three properties are worth knowing:

- **It persists counts, not lines.** The raw log is gigabytes over a few days and uninteresting once counted; what lands on disk is a few KB of `{workload → host → count}`. Nothing to rotate, and the PVC never fills.
- **It attributes at query time, against a watched pod cache.** This is the part that matters. It polled every 20s at first, and the result was exactly what you'd predict: six long-lived workloads attributed, while `www.duckdns.org` and `api.smitegame.com` landed in the unattributed bucket — those are CronJobs, and a duckdns pod resolves its name and exits in about a second. A watch gets the `ADDED` event with the pod IP already set, before the pod has finished starting.
- **It resumes on restart.** The counters live in memory, so without reloading the aggregate at startup an eviction or a chart upgrade would silently reset a multi-day window to zero while leaving a full-looking file on disk.

RBAC is read-only and narrow, but note it includes `get` on `pods/log` cluster-wide. It only ever reads CoreDNS's, but RBAC cannot express "one pod's log" without naming the pod, and CoreDNS pod names change on every restart — which this audit causes twice.

## Deploy

```sh
./upgrade.sh                       # default: logging off, no collector

# open a measurement window:
cp values.local.yaml.example values.local.yaml
./upgrade.sh                       # restarts CoreDNS — brief DNS gap

../../scripts/egress-audit.sh status
../../scripts/egress-audit.sh report

# close it again:
rm values.local.yaml && ./upgrade.sh
```

The window lives in `values.local.yaml` (gitignored) rather than `values.yaml` on purpose: the tracked default must stay `false`, so anyone running `upgrade.sh` without that file closes the window rather than silently re-opening it.

Manual rollback, if it is ever needed:

```sh
kubectl -n kube-system delete configmap coredns-custom
kubectl -n kube-system rollout restart deployment coredns
```

## Verify

```sh
kubectl -n kube-system get cm coredns-custom -o jsonpath='{.data.egress-audit\.override}'
kubectl -n kube-system logs deployment/coredns --tail=20
```

A logging line looks like:

```
10.42.3.15:41234 - 12345 "A IN api.ipify.org. udp 41 false 512" NOERROR qr,rd,ra 123 0.023s
```

Client IP, query name, response code. That is the whole payload the audit needs.

## Known gaps

- **Attribution is by pod IP, which is ephemeral.** The watch closes most of that gap, but a pod that is created, resolves, and is deleted faster than the watch event is delivered still lands in the unattributed bucket. Precise, durable attribution comes from the proxy, where the client authenticates by name.
- **A cached lookup is an invisible connection.** The Corefile sets `cache 30`, and a pod that holds a connection open resolves once. The log proves a name *was* resolved; it does not measure how much traffic followed — so this ranks reach, not volume. Bytes come from the proxy.
- **A resolved name is not a connection.** The reverse also holds: a pre-flight lookup that never gets dialled still appears here.
- **No NetworkPolicy on the collector.** Deliberate. RBAC is the control that matters for a pod holding cluster-wide `pods/log`, a netpol wouldn't reduce that, and a wrong egress rule would break the audit silently rather than loudly.
- **No negative zone filter.** `log` takes zones to include, and "everything except cluster.local" is not expressible. Internal service lookups are dropped in `infra/alloy` instead, where a regex can say "not internal".
- **One ConfigMap, one owner.** Anything else that ever needs to extend the Corefile adds a key to this chart rather than creating its own object — there is only one `coredns-custom`, and the last writer would win.
