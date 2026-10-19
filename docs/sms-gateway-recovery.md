# SMS gateway outage and recovery

Status: unresolved; handset checks deferred until the owner is home.
Evidence collected 2026-10-19 (UTC). No test SMS was sent or failed messages
requeued during diagnosis.

## Findings

- Last confirmed delivery: **2026-10-07 12:56:02 UTC** (message creation time).
- All **48 outbound messages** created after that last successful message have
  failed: 32 from CrowdSec, 10 from Talaria, and 6 from selfhosted-timers.
- Failures report `gateway unreachable: All connection attempts failed`.
  Recent messages exhausted all five attempts; the latest was created on
  October 19 at 08:31 UTC.
- The `infra/sms-relay` deployment is Ready, and `/api/health` returns HTTP 200.
  This endpoint checks the database and queue, **not handset connectivity**.
- The configured Android gateway is `http://192.168.4.24:8080`. TCP connections
  are refused from the relay pod. A separate check from node5's host network
  also failed, while its neighbor table showed that LAN address reachable.
  This points to the gateway listener or the configured address, rather than
  a pod-only networking problem. The device currently at that IP has not been
  confirmed to be the intended phone.

## Recovery when home

1. Check that the Android gateway phone is powered, charged, and on home Wi-Fi.
   Compare its current IP with `192.168.4.24`.
2. Open **SMS Gateway for Android** and start its local HTTP server. Confirm
   the configured port is `8080`. Check SMS permissions and any app error shown.
3. If the phone's address changed, restore its DHCP reservation or update the
   relay's `provider.gatewayUrl` through the normal secrets/deployment workflow.
   Do not put gateway credentials in this document or Git.
4. Recheck the authenticated gateway API from the relay pod. Then send one
   explicitly authorized test SMS and verify both actual receipt and the
   relay's delivery status. HTTP 202 from the relay means queued, not delivered.
5. Review Android battery/background restrictions and enable the app's
   supported startup behavior so the local server remains available.
6. Review failed messages before deciding whether to retry any. They have
   exhausted retries and will **not automatically resend**. Avoid bulk replay:
   old alerts and Talaria notifications may no longer be relevant.

## Monitoring follow-up

- Add handset connectivity monitoring and alerts for outbound delivery failures
  or exhausted retries; queue depth alone misses messages already marked failed.
- Keep handset availability separate from Kubernetes liveness/readiness: a
  phone outage should not restart the relay that owns the durable queue.
- Verify the alert destination works independently of this SMS gateway.

Implementation details: [SMS relay README](../infra/sms-relay/README.md).
