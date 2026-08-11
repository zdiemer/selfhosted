# alloy — logs and metrics to Grafana Cloud

Grafana Alloy shipping the cluster's logs and metrics to Grafana Cloud's free
tier. Values-only against the upstream `grafana/alloy` chart, the same
arrangement as [`infra/headlamp`](../headlamp) and [`minecraft/`](../../minecraft).

**Two releases from this one directory**, the way
[`infra/democratic-csi`](../democratic-csi) does:

| Release | Values | Shape | Job |
|---|---|---|---|
| `alloy` | `values.yaml` | DaemonSet, every node | logs + every pod-local scrape |
| `alloy-probe` | `values-probe.yaml` | Deployment, 1 replica | blackbox probes of the public hostnames |

```
Traefik access log ─┐
cloudflared        ─┤
Authelia           ─┼─▶ Alloy (DaemonSet) ─▶ Grafana Cloud  (Loki + Prometheus)
ingress backends   ─┤                              ▲
egress proxy       ─┤                              │
CrowdSec agents    ─┘                              │
                                                   │
Traefik · node-exporter · kube-state-metrics ──────┤
CrowdSec · cloudflared · CoreDNS · k8up ───────────┤
kubelet volume stats · Alloy itself ───────────────┤
                                                   │
16 public hostnames ─▶ Alloy-probe (Deployment) ───┘
```

Consumed by [`infra/grafana-dashboards`](../grafana-dashboards).

## Why this exists

Before this, the cluster had **zero request-level visibility**. Traefik ran with
no `--accesslog` flag at all — about two log lines a day — and its Prometheus
metrics on `:9100` were exported with nothing scraping them. There was no way to
answer "is anything scanning me", "who hit that endpoint", or "why did that
502". See [`infra/traefik`](../traefik), which now produces the log this ships.

## Why Grafana Cloud rather than self-hosted Loki

The free tier covers this many times over: **50 GB/month, 14-day retention, 10k
active series, 3 users**, free indefinitely.

Self-hosting Loki here would mean a ReadWriteOnce `local-path` PVC, and that
combination is genuinely bad in this cluster. Pods must not be pinned to a node,
so a reschedule would strand the log history on whichever node still holds the
volume — precisely when you most want to read it. Shipping off-node removes the
problem rather than managing it. No PVC appears anywhere in this chart.

**The trade, stated plainly:** access logs contain client IPs, hostnames,
request paths, and every internal service name. They leave the house. Retention
is 14 days, so this is a rolling two-week window, not history.

## What gets shipped, and what doesn't

Only pods that genuinely have external ingress. This is the setting that keeps
us inside the free tier — the cluster runs ~150 pods, and blanket shipping would
plausibly exceed 50 GB/month on talaria's Elasticsearch and Logstash alone. The
allowlist should land near **0.3–1 GB/month**.

**Tier 1 — the ingress plane.** `kube-system/traefik`, `infra/cloudflared`,
`auth/authelia`. This is *complete* coverage of external HTTP by construction:
every external request traverses Traefik, so its access log is the whole record
regardless of which namespace or repo owns the workload behind it.

**Tier 2 — the workloads behind an Ingress**, so a 502 in Traefik's log can be
read next to the application's own stack trace. These carry a pod label:

```yaml
logging.zachd/external-ingress: "true"
```

which each chart stamps **from its own `ingress.enabled`**:

```yaml
{{- if .Values.ingress.enabled }}
logging.zachd/external-ingress: "true"
{{- end }}
```

That is the point of the design: a service that turns its ingress off stops
shipping, and a new service starts shipping, with **no change to this chart**.
The allowlist cannot drift out of sync with reality because it is derived from
it. The two tiers are explicitly disjoint — a pod matching both would have every
line shipped and billed twice, silently.

Not labelled yet: the three submodule charts (`games/gamedex`,
`finance/money`, `infra/sms-relay`) live in their own repos. Tier 1 already
covers every request to them; only the app-side correlation is missing.

**Tier 3 — the egress plane.** `infra/egress-proxy`. The mirror image of tier 1:
every opted-in outbound request traverses the proxy, so its access log is the
whole record of external egress the way Traefik's is for ingress. The proxy has
no Ingress and never will, so it cannot be double-shipped by tier 2.

Squid emits its access log as logfmt precisely so this needs no regex:

```
ts=1786121898.340 svc=apartment-watch lane=direct src=10.42.9.249 method=CONNECT \
  url=sapi.craigslist.org:443 status=200 bytes_in=71889 bytes_out=2440 \
  peer=TCP_TUNNEL:HIER_DIRECT dst=208.82.238.1 duration=6465
```

`svc`, `lane` and `status` are promoted to labels — all three bounded by
construction, and `status` earns it because a spike in 403 is an `allowedHosts`
list that is too tight while 407 is a credential that has not landed. Those are
the two ways this breaks a service and both are invisible without it.

`url` and `dst` stay in the line. One stream per destination host is exactly the
cardinality bomb the `RequestPath` rule below is about — a single browser-driven
apartment-watch run has already been measured at **70 distinct hosts**.

The stream selector also carries a line filter (`|= "svc="`), because the same
container writes squid's `cache_log` to stderr. Those lines still ship, just
unparsed — which is what you want when something goes wrong at startup.

**Tier 4 — the security plane.** `crowdsec`, **agents only**. CrowdSec is the
cluster's entire intrusion-detection story and none of it used to leave the pod:
the only way to read a detection was `cscli` in a shell. The line that matters
looks like

```
msg="Ip 203.0.113.77 performed 'crowdsecurity/http-crawl-non_statics' (44 events over 1.5s) at ..."
```

and carries the **source IP** — something the Prometheus metrics deliberately
cannot, since an IP is unbounded cardinality. `scenario` is promoted to a label
(bounded by the installed collections); the IP stays in the line.

The LAPI is excluded on purpose: it logs every agent heartbeat and every kubelet
probe, measured at 22 lines/minute — ~32k lines/day of `GET /v1/heartbeat 200`.
Its *metrics* are still scraped, which is where `cs_alerts` comes from. The
agents are silent unless something actually happens, which is exactly the
property you want from a security log.

## Locality — the rule that makes this a DaemonSet rather than nine copies

`discovery.kubernetes` returns every pod in the **cluster**, to **every** Alloy.
Nothing about being a DaemonSet changes that, and before the locality filter all
nine pods tailed the same logs and shipped them all: measured at ~100 MB sent
per pod over three days, nine times over, for ~100 MB of actual data. Loki
deduplicates identical `(stream, timestamp, line)` entries so the stored volume
was right, but egress, API load and ingest metering were all 9×. The README's
"0.3–1 GB/month" estimate was quietly running at roughly 8 GB.

`discovery.relabel "local_pods"` keeps only targets whose
`__meta_kubernetes_pod_node_name` matches this pod's own `K8S_NODE_NAME` (an env
var the upstream chart already injects). Every tier and every scrape job derives
from it, so each target is handled by exactly one Alloy — including
cluster-singletons like kube-state-metrics, which need no leader election
because whichever Alloy shares their node picks them up.

> ⚠️ **This is only correct because `controller.tolerations` puts a pod on every
> node.** Before that toleration the DaemonSet ran on 9 of 10 — `zachd-ubuntu`
> carries the control-plane `NoSchedule` taint and is *not* empty (a CoreDNS
> replica, both CSI node plugins). With locality filtering, a node without an
> Alloy is a silent blind spot rather than a gap someone else covers. `upgrade.sh`
> asserts scheduled-vs-node-count and fails the deploy if they diverge.

## Cardinality discipline

Every distinct label combination is a Loki stream, and the free tier caps active
series at 10k for the whole stack. Promoted to labels: `namespace`, `pod`,
`container`, `app`, plus `RequestHost` (~20 values) and `DownstreamStatus` (~10)
from Traefik, `svc`/`lane`/`status` from the egress proxy, and `scenario` from
CrowdSec.

**Deliberately not promoted:** `ClientHost`, `X-Forwarded-For`, `RequestPath`,
`url`, `dst`, and CrowdSec's `source_ip`. Those are unbounded — one stream per
client IP or per URL would exhaust the budget immediately. They stay as fields
in the log line and are still fully queryable with `| json` / `| logfmt` /
`| regexp`, which costs no series at all. The dashboards' "top attacking IP"
and "top destination" panels are built exactly that way.

`app` deserves a note: it is set from `k8s-app` **first** and then from
`app.kubernetes.io/name`, because the CrowdSec chart sets only the former. Both
rules use `regex = "(.+)"` rather than the default `(.*)`, since a rule matching
an empty value sets the target to the empty string — which would *delete* the
fallback the previous rule just set.

### The metrics side

Each of the nine scrape jobs carries its own keep-list. Measured contributions:

| Job | Series | Note |
|---|---|---|
| node-exporter | ~1,800 | ~180 of ~570 exported, ×10 nodes |
| kube-state-metrics | ~2,000 | of 2,481 exported; `Running`/`Succeeded` phases dropped here |
| Traefik | ~800 | `*_bucket` dropped |
| CoreDNS, kubelet, cloudflared, k8up, Alloy, CrowdSec | ~700 | |
| blackbox probes | ~100 | 6 metrics × 16 targets |

`*_bucket` stays dropped, and there is now a strictly better source for
percentiles than re-enabling it: the access log carries a per-request `Duration`,
so `quantile_over_time(0.95, {app="traefik"} | json | unwrap Duration [5m])`
gives p50/p95/p99 at full precision and **zero series cost**.

> ⚠️ **Do not "simplify" the CrowdSec keep-list into a `labeldrop` of `source`.**
> Several CrowdSec metrics carry a `source` label holding the full container-log
> path including the container ID, which churns on every Traefik restart —
> dropping the label is the obvious fix and it is wrong. The agent tails **two**
> such files at once (the current Traefik pod and the one it replaced), so
> dropping `source` collapses two distinct series into one identity with two
> values at the same timestamp; remote_write then discards one and the counter
> reads low forever. The keep-list excludes the metrics where `source` appears
> *and* the cardinality is real (`cs_node_hits_*` above all) instead.

## Deploy

```bash
cp values.local.yaml.example values.local.yaml    # then add the Grafana Cloud token
./upgrade.sh
```

`upgrade.sh` adds the upstream repo, builds the credentials Secret from
`values.local.yaml` (so the token never lands in the rendered ConfigMap, which
is readable by anyone with namespace access), then installs.

It validates **both** rendered configs with `alloy validate` in a throwaway pod
before applying anything (a dangling component reference is valid YAML and fails
only at startup), installs both releases, then checks delivery afterwards —
because **a bad token does not stop the pod**: Alloy starts cleanly and simply
fails every push, so `Running` proves nothing.

The delivery check **sums across every pod** rather than sampling one. With the
locality filter, an Alloy on a node running no Traefik, no Authelia, no
cloudflared, no egress-proxy and a quiet CrowdSec agent legitimately reports
`sent_bytes=0` forever; sampling `.items[0]` would fail a healthy deploy about
one time in ten, at random.

## Verify

```promql
# in Grafana Cloud -> Explore
{cluster="home-k3s", app="traefik"} | json | DownstreamStatus >= 400

# a pod with NO external ingress must be absent
{cluster="home-k3s", namespace="default"}      # talaria workers: expect nothing

# the security plane (tier 4)
{cluster="home-k3s", app="crowdsec"} |= "performed"

# egress: who is talking to what, and how much
{cluster="home-k3s", app="egress-proxy"} | logfmt
sum by (svc) (count_over_time({cluster="home-k3s", app="egress-proxy"}[1h]))

# egress: the two ways an allowlist breaks a service
{cluster="home-k3s", app="egress-proxy", status=~"403|407"}
```

Every scrape job should be producing data:

```promql
traefik_service_requests_total{cluster="home-k3s"}
count by (node) (node_cpu_seconds_total{cluster="home-k3s"})   # expect 10 rows
kube_cronjob_status_last_successful_time{cluster="home-k3s"}
cs_bucket_overflowed_total{cluster="home-k3s"}
cloudflared_tunnel_ha_connections{cluster="home-k3s"}
coredns_dns_responses_total{cluster="home-k3s"}
kubelet_volume_stats_used_bytes{cluster="home-k3s"}
probe_success{cluster="home-k3s"}                              # from alloy-probe
```

**Cardinality regression check**, 30 minutes and again 24 hours after any change
to a keep-list. If anything CrowdSec-shaped or `veth`-shaped is near the top, a
label escaped:

```promql
topk(15, count by (__name__) ({__name__=~".+", cluster="home-k3s"}))
count({__name__=~".+", cluster="home-k3s"})                    # against the 10k cap
```

Note that these can only be run from Grafana — the token in `values.local.yaml`
is deliberately write-only. Reading needs the separate service-account token in
[`infra/grafana-dashboards`](../grafana-dashboards).

Then check the monthly projection at grafana.com → your stack → Billing/Usage
after 24h. Budget is 50 GB/month.

### `dropped_entries` after a restart is expected

`upgrade.sh` restarts the DaemonSet every run (env vars are read once at start),
and `loki.source.kubernetes` then re-reads each pod's log from the beginning.
Long-lived pods like `cloudflared` and `authelia` have weeks of history, and
Grafana Cloud rejects anything older than about 7 days:

```
status=400 ... has timestamp too old: 2026-07-25T11:38:01Z,
             oldest acceptable timestamp is: 2026-07-31T17:26:36Z
```

So a five-figure `dropped_entries` immediately after an upgrade is the replay
being trimmed, not a broken pipeline — the number to read is `sent_bytes`, and
`status_code="204"` appearing alongside the 400s. Lines that *are* inside the
window get re-sent, but Loki deduplicates identical (stream, timestamp, line)
entries, so they are not billed twice.

## Known gaps

Requests that never reach Traefik have **no access log anywhere**:

| Path | Exposure | Now partly covered by |
|---|---|---|
| `mc-minecraft` — LoadBalancer TCP `:25565` | public, via the VPS haproxy relay | a `tcp_connect` probe (up/down only) and node NIC counters (bytes only) |
| Jellyfin — NodePort `:30096` | public, via the VPS Caddy relay | an HTTP probe and node NIC counters |
| `headlamp` — NodePort `:30100`, **cluster-admin** ServiceAccount | LAN/tailnet | nothing |

Neither relay ships its own log: haproxy writes to VPS syslog and Caddy keeps
its access log on the VPS. The haproxy relay also does not preserve source IP,
so even that log could not attribute a connection.

What the additions in this chart *do* buy is that these paths are no longer
completely invisible — `node_network_*` counts their bytes and the probes in
`alloy-probe` notice when they stop answering. Neither is a request log.

**Fixed since this was first written:** BlueMap's `:8100` LoadBalancer on every
node IP is now `ClusterIP`, so `map.*` through Traefik is the only door. See
[`minecraft/bluemap-ingress`](../../minecraft/bluemap-ingress).
