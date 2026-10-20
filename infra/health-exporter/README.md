# Health alert coverage

`helm upgrade --install health-exporter infra/health-exporter -n infra --wait`
deploys a read-only exporter with permission to list K8up Schedules and Backups.
It checks ntfy availability and exports each expected namespace's age since its
last successful full backup. Failed runs do not reset that age. Expected
namespaces are explicit so deletion of a schedule cannot silently remove its
alert; update `backupNamespaces` alongside `infra/k8up/values.yaml` schedules.
One full repository policy per namespace is assumed, matching current K8up setup.
This checks successful backup completion, not restore integrity or NAS replication.

Alloy scrapes annotated dedicated port 9105 endpoints once per minute:

- This exporter: ntfy health, backup age and collector success.
- SMS relay: oldest queued/sending message, failed messages updated in the last
  hour, and messages switched to SMS fallback created in the last hour. No phone
  numbers, keys, message bodies, or service labels are exposed.
- Arr sidecars: local authenticated `/api/v1/health` (Prowlarr) or
  `/api/v3/health` (Sonarr/Radarr). Keys are read from the existing read-only
  config volume, never copied into metrics or logs. Exported warning/error
  counts omit raw health messages.
- Messaging gateway: enabled transport connection states. WhatsApp uses live
  open/close events; Signal uses its local signal-cli socket connection. This
  does not prove upstream Signal connectivity, authorization policy correctness,
  or end-to-end message delivery. A missing exporter also alerts.

No metrics Service or public ingress is created for these listeners. The
workspace and relay allow Alloy ingress only on the dedicated monitoring port;
the workspace terminal remains inaccessible from Alloy.

## Routing and thresholds

Rules live in `infra/grafana-dashboards/alerts/rules.json`:

| Condition | Threshold/pending | Destination |
|---|---|---|
| Notification degradation | ntfy down, queue older than 10m, recent failure/fallback; 5m pending | ntfy/SMS |
| ntfy explicitly unhealthy | 5m | Grafana Cloud email only |
| Missing health exporter | 10m | ntfy/SMS |
| Weekly backup stale | 9 days; 15m pending | ntfy/SMS |
| Backup status collection unavailable | 10m | ntfy/SMS |
| Deployment/StatefulSet replicas missing | 10m | ntfy/SMS |
| DaemonSet coverage missing | 15m | ntfy/SMS |
| Startup failure / CrashLoop | 10m | ntfy/SMS |
| Recurring OOM | observed restart count delta >1 in 30m, last exit OOMKilled; 2m pending | ntfy/SMS |
| Messaging transport disconnected | 5m | ntfy/SMS |
| Arr health warning/error | 15m | ntfy/SMS |

Existing four-hour repeat and recovery notifications apply. Only the dedicated
ntfy-unreachable rule uses email, after five minutes of explicit unhealthy checks.
Missing telemetry alone never triggers email. Other delivery failures use ntfy/SMS. Historical
failed messages outside the one-hour window do not page indefinitely. OOM is a
correlation of restart count with last exit reason, not an exact OOM event count.

## Validation

Run `python3 -m unittest discover -s infra/health-exporter/tests -v`,
`python3 infra/grafana-dashboards/tests/test_notification_policy.py`, and
`scripts/ci-lint-dashboards.sh`. Live metrics were verified in Grafana Cloud.

### Activation record, 2026-10-19

All 26 Grafana rules evaluated with health `ok`; all new metric families arrived
in Grafana Cloud. Relay tests passed (51), gateway tests passed (172), and both
repository CI runs passed. The email-only contact point accepted its test.
Sonarr reported Internet Archive and Uindex indexer warnings during validation;
the alert reports these existing conditions rather than suppressing them.

The gateway was updated in place because the operational session lives in the
same workspace pod. Helm was upgraded while the Deployment was paused, and its
owning ReplicaSet template was reconciled to the verified image/metrics changes
before resuming. The live messaging Secret matched the running environment; only
its historical checksum differed. Deployment rollout status passed, both
transports reported connected, and the Deployment was left unpaused. Current
pod port metadata still predates the metrics port; Alloy selects the gateway
container explicitly, so scraping works now and after normal pod recreation.
Future Helm changes retain their normal Recreate behavior.
