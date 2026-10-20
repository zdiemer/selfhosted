# Notification routing and Cloudflare handoff

Updated 2026-10-20. Local senders and Grafana Cloud delivery are live.
Grafana email is now conditional on ntfy explicitly failing its health check
for five minutes. Critical severity alone never causes email.

## Routes

| Source | Primary | Fallback / scope |
|---|---|---|
| systemd secret checks, backups, sync, watchdog, version report | ntfy `alerts` | Existing SMS recipient; same hourly idempotency key |
| CrowdSec non-routine remediation | ntfy `security` | SMS on publish failure; aggregate for 5 minutes or 20 alerts |
| Owner's Talaria new/ending listings | ntfy `talaria` | SMS on publish failure; only configured recipient and listing templates |
| SMS gateway monitoring | ntfy `alerts` | Durable relay queue if direct ntfy publish fails |
| Grafana warning rules | ntfy `alerts` | Relay selects SMS if ntfy publishing fails |
| Grafana critical infrastructure rules | ntfy `alerts` | SMS fallback on publish failure |
| ntfy unavailable for 5m | Grafana Cloud email | Only explicit unhealthy ntfy checks; not missing telemetry |

Subscribe to `alerts`, `security`, and optionally `talaria` on
`https://ntfy.zachd.duckdns.org`. Routine CrowdSec scanners remain silent.
Talaria verification codes, admin messages, and other users' messages keep their
SMS behavior. No old failed messages were replayed.

The relay stores one message and its selected transport. Acknowledged ntfy
publishes become `published`; SMS fallback is selected only after ntfy fails.
Once selected, SMS retries never also retry ntfy. The API's 202 means queued;
`published` means accepted by ntfy, not confirmed delivery to a phone.
A lost publish response or crash between acceptance and commit can still cause
an at-least-once duplicate. Neither transport bypasses a full cluster outage.
The Android gateway still needs its at-home repair before SMS fallback can work.

## Cloudflare setup (configured and verified)

Add a **Published application route / Public hostname** to the existing home
Cloudflare Tunnel:

| Setting | Value |
|---|---|
| Hostname | `notify.diemer.codes` |
| Service type | `HTTPS` |
| Origin service | `traefik.kube-system.svc.cluster.local:443` |
| HTTP Host Header | `notify.diemer.codes` |
| No TLS Verify | **On**, matching other tunnel origins whose TLS terminates at Cloudflare |
| Access policy / interactive login | None on this webhook; the app requires a dedicated bearer credential |

Route through Traefik, **not directly to sms-relay**: Traefik exposes only the
exact `/api/v1/grafana` path and applies a 64 KiB request limit and rate limit
(30 requests/minute, burst 10). The application accepts POST only and checks the
dedicated Grafana credential. `/`, SMS APIs, and ntfy subscription endpoints
are not exposed by this hostname. Leave ntfy's existing private hostname alone.

The tunnel UI normally creates the DNS record with the hostname. If configuring
DNS separately, point the proxied record at this tunnel's existing UUID target.
No credential needs to be entered in Cloudflare.

## Provisioning and reactivation

The Grafana `ntfy-primary` contact point is active with its bearer
credential from `op://homelab/infra-sms-relay/values.local.yaml`. Its URL is:

`https://notify.diemer.codes/api/v1/grafana`

Run from the selfhosted checkout:

```bash
infra/grafana-dashboards/configure-notifications.sh --activate
```

Activation first sends an authenticated invalid payload to the public URL and
requires the application's specific 400 response, without queuing a message.
It then enables the owned Grafana rules and persists `enabled: true` in
`infra/grafana-dashboards/notifications.json`; commit and push that change.
Subsequent dashboard upgrades retain the notification routing. The authenticated
readiness request uses Grafana's User-Agent: Cloudflare rejected Python's default
User-Agent with error 1010. A real Grafana Cloud webhook test also passed.

The dedicated SMS gateway Grafana rule remains non-notifying: its existing
local monitor owns notifications. The generic probe rule excludes `sms-gateway`,
preventing a second alert for the same phone outage. Existing failed-Job rules
already exclude failures superseded by successful CronJob runs or backups.

## Grafana delivery and grouping

Owned infrastructure rules use `ntfy-primary`. The compatibility receiver
`critical-outages` retains only its webhook integration; the unconditional email
integration has been removed. Grafana sees a queue acknowledgement, not eventual
ntfy delivery. Grouping waits 30 seconds, groups changes for 5 minutes, and
repeats ongoing incidents every 4 hours. Recovery notifications are enabled.

The fallback email destination is the existing sole Grafana administrator.
Provisioning refuses zero or multiple admin email candidates. Grafana Cloud
handles SMTP; no home SMTP service or public Grafana ingress is needed.

`Home cluster telemetry missing` still detects loss of cluster scrape heartbeats,
but it now uses ntfy/SMS. It cannot deliver through a fully unavailable home
notification path. Only the explicit ntfy-unreachable rule can trigger email;
see the conditional policy below.

## Validation performed

- Relay regression tests cover owner-only routing, preservation of verification
  SMS, successful ntfy without SMS, failed ntfy selecting SMS, webhook auth,
  size limits, deduplication/recovery, and migration of existing SQLite rows.
- Monitor tests cover ntfy failure queuing fallback with a stable key.
- Live duplicated requests for all three topics produced one published message
  each with `transport=ntfy`; no SMS fallback was selected.
- Through Traefik, `/` returns 404, GET webhook 405, unauthenticated POST 401,
  another caller's key 403, malformed authenticated payload 400, oversized body
  413. Public authenticated readiness passed; Grafana Cloud reported successful
  webhook and email test deliveries. Email success is provider acceptance, not
  confirmation that the owner read or received it in their inbox.
- Database snapshot before migration is on the relay PVC at
  `/data/sms-relay-before-ntfy-20261019.db` (private message data; do not commit).

## Additional coverage (2026-10-19)

All five follow-up areas are configured: notification delivery health, weekly
backup freshness, workload/OOM failures, messaging connections, and Arr health.
See [health alert coverage](../infra/health-exporter/README.md) for thresholds,
limitations, expected backup namespaces, and validation. Notification-path
failures use ntfy/SMS; explicit ntfy unavailability alone uses email.
Routine download events are not published.


## Conditional email policy (2026-10-20)

This replaces the parallel critical-email configuration introduced on October 19.
All owned application, infrastructure, delivery-degradation and telemetry alerts
use `ntfy-primary` (with the existing relay SMS fallback). The local SMS monitor
continues to own its non-notifying Grafana rule.

Only `sh-ntfy-unreachable` uses `notification-path-email`. It requires the direct
ntfy health metric to report unhealthy for five minutes; a missing metric or
Grafana query error does not count as evidence that ntfy is unreachable. This
sends one notification-path outage email, not a copy of every application alert.
The email directs the owner to the current firing alerts; recovery messages stay
enabled. The legacy unconditional `critical-outages-email` integration is removed.

Consequently, a total cluster/telemetry outage alone will no longer email the
owner. This is intentional under the requested strict ntfy-unavailable policy;
an independently measured ntfy reachability probe would be needed to extend
coverage without treating missing data as failure.

The CronJob overdue rule clears a missed scheduled run after a subsequent
successful completion (including a recorded catch-up run). It is a missed-run
check, not a general data-freshness guarantee: a stuck scheduling timestamp
requires separate scheduling/freshness investigation. The OOM rule uses observed
restart-counter differences over thirty minutes, deduplicated across exporters,
rather than extrapolated `increase() > 1`. One OOM must not trigger a recurring
OOM notification. The last exit reason still cannot prove that every restart
in the window was an OOM.
