# Grafana Cloud duplicate-sample investigation — 2026-10-20

Status: **resolved**. Fix deployed at **23:43:29 UTC**; the ingestion alert
returned to Normal at **23:48:40 UTC**. The Arr health
alert overwrote the source metric's `severity` label, collapsing warning and error
series into the same alert instance. Post-deployment verification is recorded below.
The earlier investigation notes are retained as historical evidence.

## Root cause and repair

`home_arr_health_issues` emits one series per application and source severity.
Prowlarr and Radarr each had `severity="warning"` with value 1 and
`severity="error"` with value 0. The `sh-arr-health` rule queried both directly
and then assigned its routing label `severity="warning"` to both results.

The alert-state history datasource revealed 120 Normal-to-Pending and 120
Pending-to-Normal transitions over an hour: two applications, every minute. Each
opposing pair had the **same timestamp and fingerprint**, not merely similar
labels. Grafana's history writer emits a pending `GRAFANA_ALERTS` sample and a
stale marker for the same series at that timestamp. This explains why the
ingestion counter increased while Alloy was held and why adding a healthy
diagnostic alert did not reproduce it. It also prevented the Arr rule from
reliably completing its 15-minute pending period.

The fixed expression combines the source severities before Grafana adds its
routing severity:

```promql
sum without (severity) (home_arr_health_issues{cluster="home-k3s"})
```

The threshold, pending period, routing, and enabled state are unchanged. Four
Prometheus-engine regression cases cover healthy, warning-only, error-only, and
combined-severity inputs, including a second application to check separation:

```sh
PROMTOOL=/path/to/promtool python3 infra/grafana-dashboards/tests/test_arr_health_alert.py
```

All 27 Prometheus-backed alert queries were checked against live data after
applying their configured label overrides: none produced colliding identities.
The remaining Loki-backed rule returned no current instances.

### Post-deployment verification

The discard counter stopped at **2,792** (first flat reading at 23:44:48 UTC).
At **23:48:05 UTC**, the reported duplicate-discard rate was **zero**. Prowlarr
and Radarr retained the same pending start time, **23:43:40**, across successive
evaluations instead of resetting every minute. The original ingestion alert was
left enabled throughout and returned to **Normal at 23:48:40 UTC** through normal
evaluation. Six consecutive counter readings from 23:43:48 through 23:48:48
remained at 2,792.

The regression cases pass with the repaired expression and fail with the
original expression. No collector relabeling or alert suppression is part of
the repair.

### Additional checks that led to the finding

- Adaptive Metrics rules and segments are empty. The working read endpoints are
  `/api/plugin-proxy/grafana-adaptive-metrics-app/rules`, `/segments`, and
  `/v2/segments/default/rules` (the last two use the same plugin prefix).
  The existing Admin service-account credential can read them; no new scopes
  were granted. The previously attempted `/resources/aggregations/rules` route
  was not the route used by the installed frontend.
- A single calibration request at **23:38:42 UTC** sent 32 identical samples in
  one explicitly diagnostic series, expecting 31 discards. It returned HTTP 200;
  the counter rose from 2,751 to 2,784 at **23:39:48**, accounting for the 31
  synthetic discards plus the normal two. Exclude this intentional pulse when
  interpreting incident volume. The retained sample belongs to
  `selfhosted_diagnostic_duplicate_calibration{diagnostic="20261020-accounting-delay"}`;
  no continuing writer or job was created.
- A planned four-minute hold was cancelled **before its gate activated** when
  the conflicting state-history transitions were found. Both ConfigMaps were
  restored, all eleven runtime endpoints verified direct and healthy, and the
  relay and temporary TLS files removed.
- The deployed Grafana commit is `ba35f5126fe58b2ab365a9e69bfe3cff27b7e9e3`
  (version `13.3.0-37680305507`). Its
  [history writer](https://github.com/grafana/grafana/blob/ba35f5126fe58b2ab365a9e69bfe3cff27b7e9e3/pkg/services/ngalert/state/historian/prometheus.go)
  emits active and stale samples for state transitions. The Mimir build reported
  revision `e58bddf0`; its distributor counts repeated timestamps inside a series
  within a request under `sample_duplicate_timestamp`.

The decisive query was against **grafanacloud-alert-state-history**, not the
metric ingestion logs:

```logql
{from="state-history"} | json | ruleUID="sh-arr-health"
```

Group the returned entries by timestamp and fingerprint to expose conflicting
transitions hidden by an ordinary instant metric query.

## Earlier observations (before the root cause was found)

- Metrics tenant `3438530`, stack `1757153`, region `prod-us-west-0`;
  endpoint `prometheus-prod-67-prod-us-west-0.grafana.net`.
- `grafanacloud_instance_samples_discarded_per_second` reported approximately
  `0.0333333333` for `reason="sample_duplicate_timestamp"` (two samples/minute).
  Capacity rejection rates were zero. Active usage was approximately 6,500 series
  against a 15,000-series limit.
- Eleven Alloy collectors run version `v1.18.1`. Their ordinary Cloud write-error
  counters and logs did not report corresponding failures.
- A 150-second live-debug capture observed 17,139 samples with no duplicate
  timestamp/label pairs. That capture precedes WAL serialization and alone cannot
  rule out a write-path issue.
- A temporary second endpoint decoded Snappy/protobuf remote-write batches from
  the same WAL. It received no Cloud credentials and did not forward anything.
  No duplicate float timestamp/label pairs were found across the eleven senders.
- A temporary TLS relay then decoded the actual Cloud-bound payloads and forwarded
  the original bytes, unchanged, to the existing HTTPS Cloud endpoint. Alloy
  trusted a temporary certificate explicitly; upstream TLS validation remained on.
  By the end of the capture it had inspected 22,950 float samples in 162 requests
  from all eleven collectors, with no duplicate timestamp/label pairs. Every
  response was HTTP 200.
  The Cloud counter remained at two duplicates/minute during the capture.
- No current native-histogram series were returned by
  `count(histogram_count({__name__=~".+"}))`.
- The known SMS-phone-down rule was briefly paused, then restored. The ingestion
  rejection rule was separately paused for about five minutes while its underlying
  usage metric was queried directly, then restored. Neither test produced an
  observed drop in the discard rate. This did not establish alert-history writes
  as the cause; the precise Cloud accounting window is provider-owned.
- The datasource-managed rules API returned no groups in the earlier investigation.
  Usage Insights logs did not name an offending series. A direct read of Adaptive
  Metrics aggregation rules was denied because the available ingestion credential
  lacks the required scope. No additional permissions were granted.

## Earlier unknowns (subsequently resolved above)

At this stage, the affected metric, the originating write client, and whether the
duplicate was introduced inside the hosted write pipeline were unknown. A mirror uses its own
queue; the TLS relay addresses that limitation for the observed capture window,
not for every historical request or other writer into this tenant.

The relay and mirror were diagnostic only. Both Alloy ConfigMaps were restored to
their pre-test contents. All eleven collectors were verified healthy with only
the original direct Cloud endpoint loaded. Both receivers were stopped and their
temporary TLS key/certificate removed. Both alert rules are enabled. Temporary mirror
failures during decoder development were confined to the mirror; do not interpret
those diagnostic-endpoint counters as original Cloud failures.

## Archived Grafana support draft (not submitted; source now identified)

Please identify the metric and originating client behind
`sample_duplicate_timestamp` discards for tenant `3438530` / stack `1757153`.
A useful UTC window is **2026-10-20 19:25:30–19:27:00**: all eleven collectors were
sending through a TLS relay that decoded the actual remote-write payloads,
forwarded the bytes unchanged, found no duplicate float timestamp/label pairs,
and received HTTP 200 responses. The independent usage datasource continued to
report about two duplicate samples per minute.

Please inspect distributor traces for the metric associated with deduplicated
samples/histograms, and identify whether the originating request is Alloy,
Grafana alert-state history, another client, or a hosted aggregation path. Please
also confirm the accounting window behind the usage gauge and whether configured
Adaptive Metrics rules can affect it. We cannot inspect those rules with the
available access-policy scope.

Mimir's distributor code includes a trace event named
`Distributor.prePushValidationMiddleware[deduplicated samples/histograms with conflicting timestamps from write request]`
with metric/count attributes. This may help locate the source if supported by the
hosted version:
https://github.com/grafana/mimir/blob/main/pkg/distributor/distributor.go

## Useful queries

```promql
sum by (reason) (grafanacloud_instance_samples_discarded_per_second)
```

Run that against `grafanacloud-usage`, independently of the workload datasource.
Against `grafanacloud-prom`, verify freshness and write failures separately:

```promql
max(time() - timestamp(node_memory_MemAvailable_bytes))
max(time() - timestamp(kubelet_volume_stats_available_bytes))
sum(rate(prometheus_remote_storage_samples_failed_total[5m]))
```

Do not drop distinguishing metric labels or raise the rejection threshold merely
to clear this alert. A clean scrape or HTTP 200 does not by itself prove that Mimir
retained every sample.

## Follow-up: isolate traffic, not just inspect it

Further investigation on the same day used the cumulative
`grafanacloud_instance_samples_discarded` metric. It continued to increase by
approximately two each minute; this was not merely an unchanged rate gauge.

A TLS-verified relay was configured to return retryable HTTP 503 responses for
exactly 120 seconds, automatically reopening afterward. All eleven collectors
were verified on that endpoint before each hold. Their WAL/queues retained the
samples. No alert rules were paused for these tests.

- First hold: approximately **19:38:52–19:40:52 UTC**. The relay's forwarded-request
  count stayed at 39 throughout the hold. The Cloud counter advanced from 2,273
  at the 19:39:48 usage sample to 2,275 at 19:40:48, an interval fully inside the
  hold. The queues subsequently drained and writes returned HTTP 200.
- Second hold: approximately **19:44:57–19:46:57 UTC**. The forwarded-request count
  stayed at 243 during the hold. The discard count continued to advance.
- At **19:46:51–19:46:53**, before that second hold ended, 128 metric families were
  checked individually for samples newer than **19:45:12**. Only `GRAFANA_ALERTS`
  remained fresh: the ingestion-rejection and SMS-phone-down rules. No observed
  Alloy metric family remained fresh during the hold.

For the freshness check, query `timestamp(metric_name) > cutoff` separately for
each metric family. Do not put `label_replace` before `timestamp`: that produces
an expression-evaluation timestamp and falsely makes old samples look fresh.
A broad `timestamp({__name__=~".+"})` also fails when dropping metric names creates
identical label sets.

These tests strongly narrow the investigation away from the eleven observed
Alloy streams. They do not establish a specific hosted writer as the cause:
provider-side accounting latency, other writers, and hosted processing still
need to be accounted for. `GRAFANA_ALERTS` is a candidate, not a proven culprit.

A temporary pending diagnostic rule was also created with a 24-hour pending
period and automatic five-minute deletion. Adding its history series did not
produce a sustained increase above the original discard rate. One interval rose
by four and a later interval by zero, so the isolated increase is not proof of
causation. A separate, short-lived firing diagnostic used the existing
non-notifying `unwired` receiver and automatic cleanup to test firing-state
history independently. Neither test established a sustained increase; both
diagnostic rules were deleted. Do not infer that all alert-history samples are
duplicated merely because the two real alert series were the only fresh series
during the Alloy hold. Both original alert rules remain enabled. All eleven
collectors were verified healthy on their original direct endpoint afterward,
and the relay, gate file, and temporary TLS key/certificate were removed.

### Broken managed ingestion-log panel

The built-in dashboard `cdn9jukg55bswb` references the deleted datasource
`grafanacloud-maroonmulberry1632-usage-insights` in its error-details panel. The
working datasource is `grafanacloud-usage-insights`. The attempted narrow repair
was rejected with `Cannot save provisioned dashboard`; the managed dashboard
was not modified. Its query can be run in Explore against the working datasource:

```logql
{instance_type="metrics", instance_id="3438530"}
|~ "error while ingesting|failed to process sample due to error|failed to process exemplar due to error|failed to push aggregation result|the sample has been rejected"
```

The query returned older series-limit failures, but no details identifying the
current duplicate-timestamp source. Include this broken datasource reference in
the prepared support request as a separate, concrete dashboard defect.

### Repeat notification explained

The ingestion alert returned to `Alerting` at **19:32:40 UTC**, five minutes after
its earlier restoration at **19:27:40**. The subsequent notification was triggered
by the diagnostic pause/resume resetting the pending period; the rejection rate
had not spiked. Avoid further pause/resume tests of that production rule.

Final cleanup verification at approximately **19:59 UTC** confirmed both original
rules enabled and neither diagnostic rule present. Cloud still reported
`sample_duplicate_timestamp` rejections (counter 2,313; rate 0.04167/s, or about
2.5/minute). This remains unresolved; the diagnostic cleanup is not a fix for
rejected samples.
