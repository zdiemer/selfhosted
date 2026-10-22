# Tailscale node-key expiry notifications

A hardened systemd timer on `zachd-ubuntu` reads `tailscale status --json` every
five minutes and publishes to the existing private ntfy **alerts** topic. It
warns at **7d, 3d, 2d, 1d, 12hr**, plus once when expired. Delivery can be up to
five minutes and 15 seconds after a threshold, plus network/downtime delays.
The primary host's own key currently has expiry disabled; this monitor does
not change anyone's expiry policy or reauthenticate any device.

This monitors **node keys**, not enrollment auth keys or API tokens. See
[Tailscale key expiry](https://tailscale.com/docs/features/access-control/key-expiry).
No Tailscale admin credential is needed. The unprivileged DynamicUser can read
public peer metadata through the existing local socket, without mounting
Tailscale's private state or granting the service an operator identity.

## Coverage and delivery

Coverage is the primary host's own device and peers visible to it under tailnet
policy, including offline peers and `egress-sfo3`. The first deployment saw
26 devices. This is not an authoritative admin-API inventory: devices hidden
by ACLs, removed from the tailnet, or not enrolled are not visible. A future
tailnet grant change needs a coverage review. Missing/zero expiry means no
scheduled expiry and produces no warning.

The device's stable ID, expiry timestamp, and last delivered window are saved
under `/var/lib/selfhosted-tailscale-expiry`. Renewal resets the window state;
reboots, reinstalls, and timer restarts preserve it. If first installed late or
resumed after downtime, only the most urgent applicable reminder is sent, not
a burst of obsolete reminders. Simultaneous due devices share one notification.
Already-expired devices get one alert each per expiry timestamp, not daily spam.

State advances only after ntfy acknowledges the message. Failed publication
retries at the next timer run. A process crash after publication but before the
atomic state write may duplicate a notification (at-least-once delivery).
An unhealthy/non-Running local Tailscale backend or malformed expiry fails the
check instead of treating devices as safe. Monitor errors also attempt a
rate-limited ntfy notification, at most daily until a successful check.
If ntfy or this host is down, alerts cannot be delivered; check systemd failures
and existing host outage alerts. Receipt confirms server acceptance, not that
the phone displayed the push.

## Install or rotate the publisher credential

From a workspace with cluster credentials and root Tailscale SSH to the primary:

```sh
python3 infra/tailscale-expiry/install.py          # describe only
python3 infra/tailscale-expiry/install.py --apply  # install, enable, run
python3 -m unittest discover -s infra/tailscale-expiry/tests -v
```

The installer reads the existing `infra/ntfy-seed` `pw-alerts` publisher
credential in memory, streams it through SSH stdin, and stores it root-only at
`/etc/selfhosted/tailscale-expiry/ntfy-password`. Systemd supplies it through
`LoadCredential`; it is absent from argv, environment, Git, and logs. Rerun the
installer after rotating the ntfy credential. The service has no Linux
capabilities, cannot write system files, and uses only its private state directory.
No Kubernetes workloads, host firewall rules, or Tailscale settings are changed.

On `zachd-ubuntu`:

```sh
systemctl list-timers selfhosted-tailscale-expiry.timer
journalctl -u selfhosted-tailscale-expiry.service --since today
systemctl start selfhosted-tailscale-expiry.service
# Preview independently, without reading credentials or changing delivery state:
STATE_DIRECTORY=$(mktemp -d) python3 /usr/local/lib/selfhosted-tailscale-expiry/monitor.py --dry-run
```

Disable with `systemctl disable --now selfhosted-tailscale-expiry.timer`.
Preserve the state directory to avoid replaying reminders on re-enable.

## Validation (2026-10-22)

Eight unit tests cover exact boundaries, deduplication, renewal, missed windows,
offline/disabled expiry, unhealthy inventories, atomic persistence, and retry
following failed publication. The live sandboxed service saw 26 devices,
published one combined alert for six already-expired devices, then an immediate
second run reported `due=0`. The timer is enabled; the credential and state
files are mode 0600. Laptop `zachd-ubuntu-laptop-6` renewal remains pending the
interactive Tailscale sign-in; this monitor does not complete that login.
