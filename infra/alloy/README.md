# alloy — ingress logs and metrics to Grafana Cloud

Grafana Alloy as a DaemonSet, shipping the cluster's **external ingress** logs
and Traefik's metrics to Grafana Cloud's free tier.

Values-only against the upstream `grafana/alloy` chart, the same arrangement as
[`infra/headlamp`](../headlamp) and [`minecraft/`](../../minecraft).

```
Traefik access log ─┐
cloudflared        ─┼─▶ Alloy (DaemonSet) ─▶ Grafana Cloud  (Loki + Prometheus)
Authelia           ─┤
ingress backends   ─┘
Traefik :9100 metrics ─┘
```

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

## Cardinality discipline

Every distinct label combination is a Loki stream, and the free tier caps active
series at 10k. Promoted to labels: `namespace`, `pod`, `container`, `app`, plus
`RequestHost` (~20 values) and `DownstreamStatus` (~10).

**Deliberately not promoted:** `ClientHost`, `X-Forwarded-For`, `RequestPath`.
Those are unbounded — one stream per client IP or per URL would exhaust the
budget immediately. They stay as fields in the log line and are still fully
queryable with `| json`, which costs no series at all.

On the metrics side, `*_bucket` series are dropped: Traefik's histograms are the
bulk of its series count, and duration percentiles aren't worth the whole
budget. Drop that rule if the count turns out to have room.

## Deploy

```bash
cp values.local.yaml.example values.local.yaml    # then add the Grafana Cloud token
./upgrade.sh
```

`upgrade.sh` adds the upstream repo, builds the credentials Secret from
`values.local.yaml` (so the token never lands in the rendered ConfigMap, which
is readable by anyone with namespace access), then installs.

It also greps the log for delivery failures afterwards, because **a bad token
does not stop the pod**: Alloy starts cleanly and simply fails every push, so
`Running` proves nothing.

## Verify

```promql
# in Grafana Cloud -> Explore
{cluster="home-k3s", app="traefik"} | json | DownstreamStatus >= 400

# a pod with NO external ingress must be absent
{cluster="home-k3s", namespace="default"}      # talaria workers: expect nothing

# metrics arriving
traefik_service_requests_total{cluster="home-k3s"}

# egress: who is talking to what, and how much
{cluster="home-k3s", app="egress-proxy"} | logfmt
sum by (svc) (count_over_time({cluster="home-k3s", app="egress-proxy"}[1h]))

# egress: the two ways an allowlist breaks a service
{cluster="home-k3s", app="egress-proxy", status=~"403|407"}
```

Note that the access-log queries can only be run from Grafana — the token in
`values.local.yaml` is deliberately write-only (`logs:write`), so the push side
can be verified from a shell but the read side cannot.

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

Requests that never reach Traefik are invisible here, because there is nothing
to log them:

- `mc-minecraft` — LoadBalancer TCP 25565
- `mc-minecraft-bluemap` — LoadBalancer `:8100` on **every** node IP, so BlueMap
  is reachable on the LAN without passing through Traefik at all
- `headlamp` — NodePort 30100, cluster-admin token

The BlueMap one is fixable (`service.type: ClusterIP` in the minecraft values)
but restarts the server, so it wants an offline window. See
[`minecraft/bluemap-ingress`](../../minecraft/bluemap-ingress).
