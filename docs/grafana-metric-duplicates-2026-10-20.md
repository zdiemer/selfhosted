# Grafana Cloud duplicate-sample investigation — 2026-10-20

Status: unresolved. The observed Alloy requests do not explain the Cloud discard
counter. Do not claim this is a confirmed Cloud bug or suppress the discard alert.

## Observations

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

## What remains unknown

The affected metric, the originating write client, and whether the duplicate is
introduced inside the hosted write pipeline remain unknown. A mirror uses its own
queue; the TLS relay addresses that limitation for the observed capture window,
not for every historical request or other writer into this tenant.

The relay and mirror were diagnostic only. Both Alloy ConfigMaps were restored to
their pre-test contents. All eleven collectors were verified healthy with only
the original direct Cloud endpoint loaded. Both receivers were stopped and their
temporary TLS key/certificate removed. Both alert rules are enabled. Temporary mirror
failures during decoder development were confined to the mirror; do not interpret
those diagnostic-endpoint counters as original Cloud failures.

## Prepared Grafana support request (not submitted)

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
