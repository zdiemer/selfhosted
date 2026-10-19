# Notification routing and Cloudflare handoff

Updated 2026-10-19. Local senders are live; Grafana Cloud delivery is staged
until its public hostname is connected and activation passes.

## Routes

| Source | Primary | Fallback / scope |
|---|---|---|
| systemd secret checks, backups, sync, watchdog, version report | ntfy `alerts` | Existing SMS recipient; same hourly idempotency key |
| CrowdSec non-routine remediation | ntfy `security` | SMS on publish failure; aggregate for 5 minutes or 20 alerts |
| Owner's Talaria new/ending listings | ntfy `talaria` | SMS on publish failure; only configured recipient and listing templates |
| SMS gateway monitoring | ntfy `alerts` | Durable relay queue if direct ntfy publish fails |
| Grafana infrastructure rules | **Staged:** ntfy `alerts` | Relay selects SMS if ntfy publishing fails |

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

## Cloudflare setup (owner action)

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

## Activate after the hostname works

The Grafana `ntfy-primary` contact point is already provisioned with its bearer
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
Subsequent dashboard upgrades retain the notification routing. Test the contact
point from Grafana and confirm a real message reaches ntfy after activation.

The dedicated SMS gateway Grafana rule remains non-notifying: its existing
local monitor owns notifications. The generic probe rule excludes `sms-gateway`,
preventing a second alert for the same phone outage. Existing failed-Job rules
already exclude failures superseded by successful CronJob runs or backups.

## Validation performed

- Relay regression tests cover owner-only routing, preservation of verification
  SMS, successful ntfy without SMS, failed ntfy selecting SMS, webhook auth,
  size limits, deduplication/recovery, and migration of existing SQLite rows.
- Monitor tests cover ntfy failure queuing fallback with a stable key.
- Live duplicated requests for all three topics produced one published message
  each with `transport=ntfy`; no SMS fallback was selected.
- Through Traefik, `/` returns 404, GET webhook 405, unauthenticated POST 401,
  another caller's key 403, malformed authenticated payload 400, oversized body
  413. Public DNS/Cloudflare delivery remains pending the owner step above.
- Database snapshot before migration is on the relay PVC at
  `/data/sms-relay-before-ntfy-20261019.db` (private message data; do not commit).
