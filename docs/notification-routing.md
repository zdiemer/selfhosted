# Notification routing and Cloudflare handoff

Updated 2026-10-19. Local senders and Grafana Cloud delivery are live.
The public Cloudflare route is verified; critical alerts also use independent
Grafana Cloud email to the existing sole Grafana administrator.

## Routes

| Source | Primary | Fallback / scope |
|---|---|---|
| systemd secret checks, backups, sync, watchdog, version report | ntfy `alerts` | Existing SMS recipient; same hourly idempotency key |
| CrowdSec non-routine remediation | ntfy `security` | SMS on publish failure; aggregate for 5 minutes or 20 alerts |
| Owner's Talaria new/ending listings | ntfy `talaria` | SMS on publish failure; only configured recipient and listing templates |
| SMS gateway monitoring | ntfy `alerts` | Durable relay queue if direct ntfy publish fails |
| Grafana warning rules | ntfy `alerts` | Relay selects SMS if ntfy publishing fails |
| Grafana critical infrastructure rules | ntfy `alerts` **and Grafana Cloud email** | Email does not depend on the home cluster, relay, phone, or tunnel |

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

## Independent critical email

`critical-outages` has two integrations: the authenticated relay webhook and
Grafana Cloud email. Critical node availability, disk, public probe, tunnel,
log-delivery and missing-telemetry rules use this receiver. Warning rules use
`ntfy-primary`. Both integrations send independently, including resolved events;
email is a parallel critical safety copy, **not conditional on delivery failure**.
Grafana only sees the relay's queue acknowledgement, not eventual ntfy/SMS
success. This avoids depending on an unreachable relay to request escalation.
Notification grouping waits 30 seconds, groups changes for 5 minutes, and repeats
ongoing incidents every 4 hours. An alert transition/resolution may notify sooner.

The email destination is resolved from the existing sole Grafana organization
administrator. Provisioning refuses zero or multiple admin email candidates;
review recipient selection if the organization's membership changes. Grafana
Cloud handles SMTP; no home SMTP service or public Grafana ingress is needed.

`Home cluster telemetry missing` evaluates `absent_over_time(up{cluster="home-k3s"}[10m])`
in Grafana Cloud (with a zero-valued healthy result), then waits 2 minutes before
firing. It detects loss of all cluster scrape heartbeats even when every local
notification component is offline. Query errors/no data also alert. It requires
Grafana Cloud and the recipient's email service to remain operational. Loss of
only part of the cluster is handled by the existing infrastructure rules.

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
