# Notification routing

ntfy is primary; SMS is selected only when ntfy publication fails. Grafana Cloud
email is reserved for an explicit unhealthy ntfy check lasting five minutes.
Critical severity and missing telemetry alone do not trigger email.

## Topics and owners

Subscribe on `https://ntfy.zachd.duckdns.org`.

| Source | Topic | Delivery owner |
|---|---|---|
| Systemd checks, backups, sync, watchdog, version report | `alerts` | Local sender and relay |
| CrowdSec non-routine remediation | `security` | Aggregate for five minutes or 20 alerts; routine scanners stay silent |
| Owner's Talaria new/ending listings | `talaria` | Relay; configured recipient/templates only |
| SMS handset outage/recovery | `alerts` | `cluster-status-sms-monitor` CronJob |
| Grafana application and infrastructure rules | `alerts` | `ntfy-primary` webhook to relay |
| ntfy explicitly unavailable for five minutes | Email | Grafana Cloud `notification-path-email` |

Verification codes, admin messages, and other users' Talaria messages retain
SMS behavior. Routine downloads do not notify. See
[health alert coverage](../infra/health-exporter/README.md) for workload, backup,
connection, and delivery checks.

The relay stores one message and its selected transport. Acknowledged ntfy
publishes become `published`; SMS is selected after publication fails. Once
selected, SMS retries do not retry ntfy too. HTTP 202 means queued, and
`published` means ntfy accepted the message, not that a phone received it.
A lost response or crash between acceptance and commit can still duplicate a
notification. SMS fallback requires a reachable handset; see
[SMS recovery](sms-gateway-recovery.md).

## Public webhook

The existing home Cloudflare Tunnel exposes this route:

| Setting | Value |
|---|---|
| Hostname / HTTP Host Header | `notify.diemer.codes` |
| Origin | `https://traefik.kube-system.svc.cluster.local:443` |
| No TLS Verify | On, matching the tunnel-origin TLS setup |
| Interactive Access login | None; the webhook requires a dedicated bearer credential |

Route through Traefik. It exposes only `/api/v1/grafana`, with a 64 KiB body
limit and rate limit of 30 requests/minute, burst 10. The app accepts POST only
and checks the dedicated credential. This hostname does not expose the SMS API
or ntfy subscriptions. Keep ntfy's private hostname separate.

The tunnel hostname normally creates its DNS record; if managing DNS separately,
point the proxied record to the existing tunnel UUID target. No webhook credential
belongs in Cloudflare configuration.

## Provisioning

The Grafana contact point uses `https://notify.diemer.codes/api/v1/grafana`.
Its credential comes from `op://homelab/infra-sms-relay/values.local.yaml`.

```sh
infra/grafana-dashboards/configure-notifications.sh --activate
```

Activation checks the public route with an authenticated invalid payload and
requires the application's 400 response without queuing a message. It uses
Grafana's User-Agent because Cloudflare can reject Python's default User-Agent.
It then enables the owned rules and records notification activation in
`infra/grafana-dashboards/notifications.json`; commit that configuration change.
Dashboard upgrades preserve the routing.

The SMS handset rule remains non-notifying because its local monitor owns
delivery. The generic probe rule excludes `sms-gateway` to avoid duplicate alerts.

## Grouping and email fallback

Grafana waits 30 seconds for grouping, groups changes for five minutes, repeats
ongoing incidents every four hours, and sends recovery notifications. It sees
the relay's queue acknowledgement, not eventual ntfy delivery.

Only `sh-ntfy-unreachable` uses `notification-path-email`. Missing metrics or a
query error are not evidence of ntfy failure. It sends one notification-path
outage email directing the owner to the firing alerts. The destination is the
sole Grafana administrator; provisioning rejects zero or multiple candidates.
Grafana Cloud handles SMTP; no public Grafana route or home SMTP service is needed.

A total home cluster outage can also remove ntfy and SMS delivery. Missing
telemetry alone deliberately does not activate email. Extending this coverage
requires an independent ntfy reachability probe that respects the same policy.

## Validation

Run the [relay tests](../infra/sms-relay/README.md) and notification policy tests
in [Grafana configuration](../infra/grafana-dashboards/README.md). Verify the public
route rejects unauthenticated requests and an authenticated malformed payload
returns 400 without a queued notification. Use explicitly authorized test
notifications to check delivery and fallback; distinguish provider acceptance
from actual receipt.
