# grafana-dashboards — dashboards and alerts as code

Four dashboards and twenty-six alert rules, pushed to Grafana Cloud over its HTTP
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

## Alert delivery

Twenty-six rules evaluate in Grafana. Public notification delivery is active through
Cloudflare; critical infrastructure rules also send independent cloud email.
Then infrastructure rules use `ntfy-primary`; the dedicated SMS gateway rule
remains non-notifying because its local monitor already delivers those events.

The `unwired` receiver is retained for staged and locally owned rules so they
do not fall through to the default notification policy. See
[the Cloudflare handoff](../../docs/notification-routing.md) for activation.

### The rules

| Rule | Fires when |
|---|---|
| Schedulable node not Ready | any registered, schedulable node has been NotReady for 10m |
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
| Active series approaching the cap | above 8k of the 10k free allowance; separate from stored-series enforcement |
| Grafana Cloud rejecting metric samples | any discarded samples for 5m, grouped by reason; independent usage datasource, ntfy/SMS |
| Elevated 5xx | >5% of a hostname's requests failing |

**"CronJob overdue" is schedule-aware on purpose.** These CronJobs run anywhere
from every 5 minutes (duckdns) to quarterly (money's anchor refresh), so no
single "hours since last success" threshold is correct for all of them — a 48h
rule would fire permanently on the weekly Renovate job. The rule watches
`kube_cronjob_next_schedule_time` instead, which advances when the controller
schedules a run. A value stuck an hour in the past indicates a missed schedule;
subsequent successful catch-up runs and suspended CronJobs are excluded.

Clock-based schedules must set `spec.timeZone` explicitly. The controller
uses the host timezone when it is absent, while kube-state-metrics can calculate
the next run in UTC. Money, Renovate, certificate renewal, and CrowdSec cleanup
use `America/Los_Angeles` to preserve their existing local run times and prevent
false overdue alerts.

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
**Alerting → Alert rules** that the twenty-six rules show health `ok`. Under
**Contact points**, verify `ntfy-primary` and the webhook/email integrations in
`critical-outages`. The locally monitored SMS rule retains `unwired`.

For an end-to-end check of the security dashboard, from outside the tailnet:

```bash
curl -s -o /dev/null https://status.diemer.codes/.env
curl -s -o /dev/null https://status.diemer.codes/wp-login.php
```

Both should appear on **Security & Intrusion** → *Known-bad path probes* within
a minute, and the source IP in *Top source IPs by 4xx*.

### Job failure recovery

`sh-job-failed` uses the terminal `kube_job_failed` condition, not failed pod
attempts. A successful retry in the same Job no longer fires. A retained failed
CronJob run clears after its owner's last successful run is later than its start.
K8up Backup Jobs clear after a later successful full backup in the same namespace;
prebackup hooks cannot clear them. This assumes the current single full-namespace
backup policy; split the join by backup policy/repository before adding multiple
independent policies per namespace. Standalone and unresolved failures remain
visible, even if a CronJob is suspended. History is retained rather than deleted.

Each input uses its latest sample within ten minutes. This bridges short metric
gaps without falsely resolving and re-firing the same failed Job. Recovery
timestamps and owner mappings use the same window so a gap cannot resurrect an
already-recovered failure. A fresh successful run clears immediately; a deleted
Job ages out after ten minutes. The fifteen-minute pending period still applies.
Longer telemetry outages need the separate telemetry alerts; missing data is
not evidence of a successful Job.

The dashboard still shows retained terminal failures as historical inventory.
Warning notifications use `ntfy-primary`. Regression checks use the actual rule
expression and Prometheus' engine:

```sh
PROMTOOL=/path/to/promtool python3 infra/grafana-dashboards/tests/test_job_alert.py
```

### Arr health persistence

`sh-arr-health` requires warnings to persist for one hour, allowing ordinary
indexer timeout/backoff cycles to recover silently. `sh-arr-health-error` retains
fifteen-minute notification for health errors. Each selects its source severity
and removes that label with `sum without (severity)` before assigning routing
severity. Querying
both source severities directly collapses them into one alert identity when the
rule overrides `severity`, causing contradictory transitions and duplicate
`GRAFANA_ALERTS` timestamps. Inspect `grafanacloud-alert-state-history` for
opposing transitions at the same timestamp and fingerprint. Run regression
checks with:

```sh
PROMTOOL=/path/to/promtool python3 infra/grafana-dashboards/tests/test_arr_health_alert.py
```

### SMS gateway monitoring

`sh-sms-gateway-down` evaluates the authenticated phone connectivity probe in
Grafana and keeps the existing non-notifying `unwired` receiver. ntfy resolves
to Tailscale in public DNS, so Grafana Cloud cannot deliver a webhook directly.
The independent `cluster-status-sms-monitor` CronJob sends outage and recovery
notifications from inside the cluster to ntfy's `alerts` topic. See
[SMS recovery](../../docs/sms-gateway-recovery.md) for probe semantics and timing.

### ntfy routing activation

The public receiver is provisioned as `ntfy-primary`, with credentials from the
SMS relay vault item. It remains staged while `notifications.json` has
`enabled: false`. Follow [the Cloudflare handoff](../../docs/notification-routing.md),
then run `configure-notifications.sh --activate` and commit the enabled setting.
This routes infrastructure alerts through ntfy with SMS fallback on publish
failure. The local SMS gateway monitor remains the sole notification owner for
that dependency. Dashboard upgrades honor this setting.

### Conditional email fallback (2026-10-20)

All application and infrastructure alerts use `ntfy-primary`, including critical,
telemetry and ingestion-rejection alerts. Only `sh-ntfy-unreachable` routes to
`notification-path-email`, after a direct unhealthy ntfy check persists for five
minutes. Missing telemetry and query errors do not trigger email. Recovery
notifications remain enabled. The former parallel critical email integration is
removed. See [notification-routing.md](../../docs/notification-routing.md).

The CronJob overdue rule accounts for subsequent successful catch-up runs.
Recurring OOM detection uses observed counter differences, avoiding fractional
extrapolation that incorrectly classified one restart as more than one.
