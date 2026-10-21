# SMS gateway recovery

Use this when the relay is healthy but the Android handset is unreachable or
messages are not delivered. Handset access may require being home.

## Diagnose and restore

1. Check relay `/api/health` and authenticated `/api/health/gateway` separately.
   The first checks the database/queue; the second checks the phone. A Ready pod
   alone does not establish SMS availability.
2. Confirm the phone is powered, charged, and connected to home Wi-Fi. Compare
   its IP and local server port with the relay's configured `provider.gatewayUrl`.
3. Open **SMS Gateway for Android**, start its local HTTP server, and check SMS
   permissions and app errors. Compare reachability from the relay pod and a
   host on the LAN to distinguish handset trouble from cluster networking.
4. If the address changed, restore its DHCP reservation or update the URL through
   the normal secrets/deployment workflow. Keep credentials out of Git.
5. Recheck the authenticated gateway API. With explicit authorization, send one
   test SMS and verify both receipt and the relay's delivery status. HTTP 202
   means queued, not delivered.
6. Review Android battery/background restrictions and supported startup behavior.
7. Review exhausted messages individually before retrying. They do not
   automatically resend, and old notifications may no longer be relevant.

## Monitoring and fallback

`/api/health/gateway` performs an authenticated request to the phone with a
three-second deadline; refusal, timeout, redirects, and non-200 responses fail
with 503. It sends no SMS and does not establish carrier delivery.

Alloy probes it every four minutes. The dedicated Grafana rule detects sustained
failure, but its non-notifying receiver avoids duplicating the local monitor.
The `cluster-status-sms-monitor` CronJob checks every minute, publishes outage
and recovery notifications to ntfy `alerts`, and keeps durable state to deduplicate
repeats. The status page uses the handset check too. Kubernetes probes stay on
the relay's own health endpoint because restarting it cannot repair the phone.

SMS fallback is unavailable while the handset is down. See
[notification routing](notification-routing.md) for ntfy-first delivery and the
conditional Grafana email fallback, and the [SMS relay README](../infra/sms-relay/README.md)
for API and queue operations.
