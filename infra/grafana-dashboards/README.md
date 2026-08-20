# grafana-dashboards — dashboards and alerts as code

Four dashboards and thirteen alert rules, pushed to Grafana Cloud over its HTTP
API by `upgrade.sh`.

**This project deploys nothing to the cluster.** There is no chart and no helm
release — the deploy target is a SaaS API. It keeps the repo's shape anyway (an
`upgrade.sh` per project, a gitignored `values.local.yaml` materialized from a
tracked `op://` template) so it behaves like every other directory here.

```
infra/alloy ──▶ Grafana Cloud (Loki + Prometheus) ◀── these dashboards read
```

## Why this exists

`infra/alloy` had been shipping telemetry to Grafana Cloud for days and
**nothing consumed it**. There were no dashboards anywhere in the repo, no
alerting on metrics or logs at all, and the only documented queries were five
lines of LogQL in `infra/alloy/README.md`. The data was there; the view was not.

## The four dashboards

| Dashboard | uid | Answers |
|---|---|---|
| **Edge Traffic — In & Out** | `home-edge-traffic` | What is crossing the boundary, in bytes and requests: HTTP through Traefik, proxied egress through Squid, raw NIC throughput for the paths that bypass both, and the Cloudflare Tunnel's own view |
| **Security & Intrusion** | `home-security` | CrowdSec detections and source IPs, scanner probes, 4xx/429, auth failures — plus an explicit statement of what it cannot see |
| **Cluster Health** | `home-cluster-health` | Nodes, workloads, volume fill, CronJob and backup freshness, DNS, and the telemetry pipeline's own health |
| **Service Availability** | `home-slo` | Per-hostname uptime from an actual prober, TLS expiry, and per-host success rate |

Three things worth knowing about how they are built:

**Latency percentiles come from logs, not histograms.** `infra/alloy` drops
Traefik's `*_bucket` series to protect the series budget. The access log already
carries a per-request `Duration`, so
`quantile_over_time(0.95, {app="traefik"} | json | unwrap Duration [5m])` gives
p50/p95/p99 at full precision and **zero series cost**. That is strictly better
than re-enabling buckets, not a workaround.

**High-cardinality fields are aggregated at query time.** Client IPs, request
paths, user agents and egress destinations are deliberately *not* Loki labels —
one stream per client IP would exhaust the 10k series budget immediately. They
stay in the log line and are pulled out with `| json` or `| regexp` in the
panels that need them, which costs nothing.

**Both security-relevant dashboards state their own blind spots.** Minecraft
`:25565`, Jellyfin via the VPS Caddy relay, and headlamp's NodePort never
traverse Traefik, so no access-log panel can see them. A security dashboard that
doesn't say what it misses invites being trusted past its edge.

## Setup — the one thing you have to provision

`infra/alloy`'s token is an **Access Policy** token scoped `logs:write` +
`metrics:write`. It can push data and nothing else — it cannot create a
dashboard and cannot run a query. This needs a token issued by the Grafana
instance itself:

1. `https://<stack>.grafana.net` → **Administration → Users and access →
   Service accounts → Add service account**
2. Name `selfhosted-dashboards`, role **Admin**
3. **Add service account token**, copy the `glsa_…` string
4. Put it and the Grafana URL in `values.local.yaml` (see the `.example`)

> **Admin, not Editor or Viewer.** A service account created through the UI
> defaults to **Viewer**, and a Viewer token authenticates perfectly happily —
> `/api/search` returns `[]` rather than `401` — while every write and every
> datasource lookup 403s. `upgrade.sh` checks this up front and tells you how to
> fix it, because the failure is otherwise an opaque "Access denied" partway
> through the push. Changing the role on the existing service account is enough;
> the same token then works.

## Datasource UIDs are resolved, never hardcoded

Dashboards carry the sentinels `__PROM_UID__`, `__LOKI_UID__` and
`__USAGE_UID__`. `upgrade.sh` resolves the real UIDs from `GET /api/datasources`
at push time and substitutes them.

Hardcoding `grafanacloud-<slug>-prom` is the classic way these files rot: the
UID is per-stack, so a committed one is wrong for anybody else and silently
wrong after a stack migration.

Every dashboard also pins a stable `uid`. Without one, each push **creates a new
dashboard** instead of updating the existing one, and you end up with six copies
of "Edge Traffic" and no idea which is live. CI enforces both the JSON parsing
and the presence of a uid.

## Alerts are defined but deliberately not delivered

Thirteen rules in `alerts/rules.json`, evaluating live — you can watch them go
Pending and Firing in the Grafana UI — but every one routes to a contact point
named `unwired`, a webhook pointed at `http://127.0.0.1:9/` (the standard
discard port: refused instantly, on loopback, no DNS, no packet leaves the
machine).

That contact point has to exist. Without it, rules fall through to Grafana's
**default** notification policy, which on a Cloud stack means email — so
"I only defined the rules" would quietly start mailing on the first firing rule.

**To make them live**, change one block in `upgrade.sh` to point at sms-relay
and re-run:

```json
{ "url": "https://sms-relay.zachd.duckdns.org/api/v1/messages", "httpMethod": "POST" }
```

plus an `X-API-Key` header — matching `scripts/notify-failure.sh`, which is how
the systemd timers already text on failure.

### The rules

| Rule | Fires when |
|---|---|
| Node not Ready | fewer than 9 nodes Ready for 10m |
| Node root filesystem above 85% | the most common way this cluster falls over |
| Volume above 85% full | `local-path` volumes cannot be expanded — that one needs a migration, not a resize |
| CronJob overdue | a schedule stopped advancing (see below) |
| Job failed | a run happened and failed |
| CrowdSec overflow spike | >20 threshold crossings in 15m |
| CrowdSec parser failing | detection has stopped and the dashboard looks reassuringly empty |
| TLS certificate expiring | under 14 days |
| Public hostname unreachable | a probe has failed for 5m |
| Cloudflare Tunnel has no connections | every `diemer.codes` host is down while the cluster looks fine |
| Log delivery stalled | no logs reached Grafana Cloud in an hour |
| Active series approaching the cap | above 8k of the 10k free tier |
| Elevated 5xx | >5% of a hostname's requests failing |

**"CronJob overdue" is schedule-aware on purpose.** These CronJobs run anywhere
from every 5 minutes (duckdns) to quarterly (money's anchor refresh), so no
single "hours since last success" threshold is correct for all of them — a 48h
rule would fire permanently on the weekly Renovate job. The rule watches
`kube_cronjob_next_schedule_time` instead, which moves forward on every
successful run: a value stuck an hour in the past means the schedule stopped
advancing, whatever its period. Suspended CronJobs are excluded.

## Deploy

```bash
./upgrade.sh              # push dashboards + alert rules
./upgrade.sh --verify     # push, then run every panel query against live data
./upgrade.sh --dry-run    # resolve UIDs and validate JSON, push nothing
```

`--verify` is the one worth running after any edit. A dashboard that pushes
cleanly and shows twelve empty panels is **worse than no dashboard**, because it
reads as "nothing is happening" rather than "this query is wrong". It runs each
panel's query through the datasource the panel uses and reports what came back
empty.

Emptiness is not automatically a bug — no CrashLooping pods is good news — so
panels whose description contains `[may-be-empty]` are reported separately
rather than as failures. If you add a panel that is expected to be empty on a
healthy cluster, mark it.

## Verify

```bash
./upgrade.sh --verify
```

then open all four at 24h and 7d. In the Grafana UI, confirm under
**Alerting → Alert rules** that the thirteen rules show health `ok`, and under
**Contact points** that `unwired` is the webhook to `127.0.0.1:9`.

For an end-to-end check of the security dashboard, from outside the tailnet:

```bash
curl -s -o /dev/null https://status.diemer.codes/.env
curl -s -o /dev/null https://status.diemer.codes/wp-login.php
```

Both should appear on **Security & Intrusion** → *Known-bad path probes* within
a minute, and the source IP in *Top source IPs by 4xx*.
