# cluster-status — public cluster dashboard

Live at **<https://status.diemer.codes>** (and `status.zachd.duckdns.org`). Node
CPU/RAM/disk with a pods-vs-k3s-vs-system breakdown, per-node pod tables,
deployment health, top consumers, and recent warnings.

Ported from talaria's authed `/admin/cluster` page. That page's collector was
already fully generic — no talaria names, no filters — and its only coupling was
plumbing: talaria's `Handler` framework, its k8s client, protobuf, and structlog.
This drops all four and emits plain JSON. **The measurement logic is otherwise
deliberately unchanged**: it was the valuable part and it was already right.

talaria keeps its own page. This one is a separate, public, read-only view.

```
  collector sidecar ──reads k8s API──▶ /data/status.json  (emptyDir)
   (the only thing                            │
    talking to k8s)          nginx ───serves──┤  index.html + status.json
                                              │
                                    Ingress ──┴──▶ status.diemer.codes  (tunnel)
                                                   status.zachd.duckdns.org
```

## Why a sidecar, not a CronJob

Network rates are computed by diffing kubelet's *cumulative* rx/tx counters
against the previous sample, held in memory. A CronJob starts fresh every run, so
it would never have a previous sample and every rate would be `null` forever. The
collector has to be a long-lived process.

## Why nothing serves from an API

**This page is public.** nginx only ever serves two files off local disk, so
public traffic never reaches the Kubernetes API. An always-on API querying k8s per
request would hand anyone on the internet a way to hammer the API server and every
kubelet's `/stats/summary`, unauthenticated. The collector is the only thing that
talks to k8s, on its own fixed schedule, no matter how much traffic the page gets.

The cost is that data is up to `collector.intervalSeconds` (15s) stale — so the
page says how old it is rather than pretending to be live.

## What's public, and where to change it

Everything the collector writes is public the moment it's written. `values.yaml`
has the switches, both **on** by default:

| Flag | Covers | Why you might turn it off |
|---|---|---|
| `publish.events` | Kubernetes Warning event messages | Benign on a healthy cluster, but the text is unbounded and on a bad day routinely names Secrets, images and paths (`secret "x" not found`, `Failed to pull image ghcr.io/… unauthorized`). Least safe exactly when the page is most useful. |
| `publish.nodeVersions` | `kernelVersion`, `osImage` | Publishing exact kernel versions tells anyone which CVEs apply to these nodes. |

Redaction happens **at collection**, not in the page or the ingress — a field
that's off is never written to `status.json` at all, so it can't leak through a
later misconfiguration.

Also public by nature: node names, and every pod/namespace/deployment name in the
cluster — effectively an inventory of what runs here. That's largely the point of
a status page, but it's worth knowing.

No IPs are published: the payload carries names only.

`upgrade.sh` prints what's actually being published on every deploy, so a flag
that was meant to be off is visible at deploy time.

## RBAC

Read-only, and narrower than talaria's backend role this came from (that one also
carries `pods/log`, `jobs` and `cronjobs` for other handlers): `nodes` list,
`nodes/proxy` + `nodes/stats` get, `pods` list, `events` list, `deployments` list.

`nodes/proxy` is the powerful one — it's what reaches kubelet's `/stats/summary`
for the CPU/RAM/disk/network numbers, and it's why no metrics-server is needed.
It also means this ServiceAccount can proxy to kubelets, so nothing else should
borrow it.

## A bug fixed on the way over

talaria's collector read network counters from kubelet's **top-level**
`node.network.rxBytes`/`txBytes`. On this cluster kubelet reports `name: ""` with
those fields absent and only a per-interface list populated — so the read returns
`None`, and **talaria's network column has always been blank**. Here the collector
falls back to the busiest physical interface, skipping the overlay and tunnel
devices (`flannel*`, `cni*`, `veth*`, `tailscale*`, …) whose traffic is already
counted on the uplink. Rates now populate on all 10 nodes.

If talaria's page ever needs it, that fix is `iface_counters()` in the collector.

## Deploy

```bash
./upgrade.sh    # installs into `infra`
```

There's no `values.local.yaml` — no secrets. Both the collector script and the
page ship as ConfigMaps, so there's no image to build and no GHCR package; a
`checksum/` annotation rolls the pods when either changes.

Verify:

```bash
curl -s https://status.diemer.codes/status.json | python3 -m json.tool | head -30
kubectl -n infra logs -l app.kubernetes.io/name=cluster-status -c collector --tail=20
```

Network rates need **two** scrapes before they appear — expect `null` for the
first ~15s after a restart. That's the diff working as designed, not a fault.

## Editing the page

`templates/web-configmap.yaml` holds `index.html` (inline CSS + vanilla JS, no
build step, no framework) and the nginx config. It's a port of a 988-line React
page; if it grows much past this, it wants a real repo and a build, per the
convention in the root README.

Everything from `status.json` is HTML-escaped before it reaches `innerHTML` —
event messages are arbitrary Kubernetes text on a public page, so `esc()` is not
optional. Keep it that way.
