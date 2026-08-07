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
```

Then check the monthly projection at grafana.com → your stack → Billing/Usage
after 24h. Budget is 50 GB/month.

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
