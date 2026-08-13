# kube-state-metrics — Kubernetes object state as metrics

Values-only against the upstream `prometheus-community/kube-state-metrics`
chart, the same arrangement as [`infra/node-exporter`](../node-exporter) and
[`infra/crowdsec`](../crowdsec).

```
Kubernetes API ──▶ kube-state-metrics :8080 ──▶ Alloy scrape ──▶ Grafana Cloud
```

## Why this exists

It answers the questions nothing else here can, because everything else is a
*live snapshot*. [`infra/cluster-status`](../cluster-status) polls the API every
15s and keeps no history, so a CronJob that failed at 02:00 and was retried by
02:05 left no trace anywhere at all.

The CronJobs are most of the value. Twelve-plus schedules run in this cluster —
renovate, duckdns, money's SimpleFIN sync, seven smitele-bot jobs, talaria's
reindex — and **none of them were watched by anything**. One
metric fixes that:

```promql
(time() - kube_cronjob_status_last_successful_time) / 3600   # hours since last success
```

Then [`infra/k8up`](../k8up)'s per-namespace backup Schedules, PVC bound/pending
state and requested size (the denominator for the fill-percentage panels, whose
numerator is `kubelet_volume_stats_used_bytes`), pod restarts, CrashLoop
reasons, and DaemonSet coverage — which is how you notice Alloy or
node-exporter silently missing a node.

## The series budget is the design

Unfiltered KSM against this cluster is roughly **15–20k series — more than
double the entire 10k Grafana Cloud free tier, from one exporter.** Two filters
bring it to a measured **2,481**, of which Alloy ships about **2,000** (it drops
the `Running`/`Succeeded` pod phases on the way out). Which filter does the work
matters:

| Filter | Effect |
|---|---|
| `collectors` | KSM never **watches** the resource — no informer, no cache, no API load |
| `metricAllowlist` | KSM watches but never **exports** the series |

Both are set here rather than in Alloy's relabel rules. Dropping a series after
it has been generated, serialized and scraped costs CPU on both ends and memory
in KSM's cache, and it means a scrape-config change could silently reopen the
firehose. Alloy's keep-list is a second gate, not the first.

`pods` is the dangerous collector: 247 pods × KSM's ~25 pod metrics is ~6k
series by itself. Three pod families survive the allowlist, all of them about
something being **wrong**. `kube_pod_status_phase` is still the largest family
here (247 pods × 5 phases ≈ 1,235) and is trimmed further on the Alloy side to
the problem phases; it is kept at all because nothing else can answer "how many
pods are stuck Pending".

**Job metrics are the churniest thing KSM produces** — a new Job object per
CronJob run, each with its own series. `complete`, `succeeded`, `start_time` and
`owner` were ~550 series saying what `kube_cronjob_status_last_successful_time`
says in one, so only `kube_job_status_failed` survives: a suspended CronJob and
a crashing one look identical through last-success alone.

Deliberately not watched: `secrets`, `configmaps`, `endpointslices`, `leases`,
`replicasets`. Leases alone are hundreds of series that change every few seconds
and answer nothing; replicasets multiply every Deployment by its rollout history.

**`metricLabelsAllowlist` is deliberately unset.** It promotes Kubernetes labels
to Prometheus labels, which is the usual way a KSM install blows its budget by
accident.

### Adding a metric

One direction only: a panel that needs a new metric adds it to
`metricAllowlist` first. Adding a metric costs series forever; a panel with no
data is a five-minute fix.

## Deploy

```bash
./upgrade.sh
```

No secrets, no `values.local.yaml`. `upgrade.sh` **fails the deploy** above 3000
series — a chart bump that re-enables default collectors, or an allowlist typo,
both show up there and nowhere else until the stack starts refusing writes.

It also lists allowlist entries that produced no series. Some are legitimately
empty (no failed jobs is good news), but **KSM silently ignores metric names it
doesn't recognise**, so a misspelling looks exactly like a metric with nothing
to report — and would sit there forever otherwise.

## Verify

```bash
kubectl -n infra port-forward deploy/kube-state-metrics 8080:8080
curl -s localhost:8080/metrics | grep -cve '^#'      # expect ~2500
```

```promql
# in Grafana Cloud, once Alloy is scraping it
count(kube_cronjob_info{cluster="home-k3s"})                    # expect ~12+
(time() - kube_cronjob_status_last_successful_time) / 3600      # hours since success
kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"} > 0
```
