# DigitalOcean backup recovery rehearsal

Status 2026-10-22: **prepared, not executed against a provider backup**. No
DigitalOcean API credential is present in the workspace environment or the
accessible vault items inspected. The mounted 1Password credentials provide
vault access, not provider access. Obtain a vault reference for the provider
credential, or perform the provider steps in the DigitalOcean console. Do not
paste API tokens into chat or commit them.

This rehearsal tests an actual provider backup, not a fresh copy from a running
host. It is distinct from rebuilding a clean host from Ansible and the egress
vault archive. That clean-host path still needs separate validation if only the
provider-backup path is exercised.

## Known production hosts

| Host | Public address | Provider ID | Relevant state |
|---|---|---|---|
| Whatnow | 64.227.0.147 | 587384488 (read from live metadata) | `/opt/whatnowgg/data/users.sqlite`, `enrichment.sqlite`, both WAL/SHM companions, Caddy volumes, runtime secrets |
| Egress | 147.182.206.23 | resolve from provider inventory | Squid/TURN/media configs, CrowdSec state, Caddy state, Tailscale identity |

The owner reports provider backups enabled. Actual backup IDs, ages, availability,
minimum disk size and supported region still require provider inventory; this
runbook does not assert that a particular snapshot exists. Whatnow currently
uses a 25GB-class root disk, with about 17GB used. Its live SQLite files had WALs
present during inspection; never discard WAL files from a disk-backup clone.

Egress SSH additionally requested a Tailscale step-up check during preparation.
The inspection was stopped without changing the host. A provider clone should
use an independent operator SSH key, not the copied Tailscale identity.

## Isolate before first boot

1. Read the source droplet and its available backups. Record the source ID,
   selected full backup image ID/time, image minimum disk size, region, and
   matching temporary size/cost. Start with Whatnow, then egress, one clone at a
   time. Provider clones incur temporary compute charges until deleted.
2. Create a unique rehearsal tag and **a new cloud firewall attached to that tag
   before creating the clone**. Inbound: SSH only from the operator's current
   public `/32` or `/128`. Outbound: no rules. Do not reuse production tags,
   firewalls, DNS, reserved IPs, load balancers, monitoring agents or backup plans.
   Check the firewall is fully applied, with no pending changes, and that no
   other firewall grants broader traffic to that tag/clone.
3. Create a **new** droplet from the chosen backup image, named
   `dr-rehearsal-whatnow-YYYYMMDD` (or `dr-rehearsal-egress-YYYYMMDD`), carrying
   only the quarantine tag and an operator SSH key. Do not use the in-place
   Restore action on production. Keep the firewall attached throughout.
4. Confirm provider identity, firewall association and host-key fingerprint via
   the provider console. Through console/SSH, stop application containers first
   (before stopping Docker, which might use live-restore), then stop and mask
   Docker/socket/containerd, Tailscale, CrowdSec/bouncer, Caddy, HAProxy, Squid,
   coturn and cron/timers on **the clone only**. Inspect other enabled units,
   user services and cron entries. These copied processes must not contact
   production services, renew certificates, send email or join the tailnet.
5. Confirm there are no remaining container processes or active cloned service
   units. Do not relax egress to make diagnostics pass. A disk clone is not a new
   identity: never start its copied tailscaled state while production is alive.

[DigitalOcean documents creating a new droplet using a backup image ID](https://docs.digitalocean.com/products/backups/how-to/create-and-restore/).
[Tag-based cloud firewall assignments include newly created tagged droplets](https://docs.digitalocean.com/products/droplets/getting-started/recommended-droplet-setup/).
[Cloud firewalls are stateful and rule-based](https://docs.digitalocean.com/products/networking/firewalls/how-to/create/);
check their effective rules rather than relying only on a firewall name. The
provider firewall is the first-boot boundary; stopping services after SSH becomes
available is not a substitute for it. Do not proceed if that boundary cannot be
established before creation.

## Validate the restored copy

`verify-clone.py` runs on the isolated clone. It refuses the known production
addresses/Whatnow ID, requires explicit matching provider metadata and a
`dr-rehearsal-` hostname, and refuses active service/container processes. It
opens SQLite read-only **with WAL support**, checks integrity and foreign keys,
and emits only aggregate counts. It does not start services, change firewall
rules, modify the database, or read runtime secrets.

```sh
# CLONE must already be quarantined; use a separately verified SSH host key.
ssh root@CLONE python3 - whatnow --droplet-id CLONE_ID --public-ip CLONE_IP \
  < infra/droplet-recovery/verify-clone.py
# Use `egress` for the restored CrowdSec database.
python3 -m unittest discover -s infra/droplet-recovery/tests -v
```

Database verification alone does **not** complete the rehearsal. Next:

- Whatnow: verify restored release/image availability, data/assets and required
  runtime configuration without logging secrets. Start a separate local-only
  app test with networking disabled, no production email/provider credentials,
  and a scratch copy of restored data. Exercise migrations and `/healthz` through
  container-local access. Validate Caddy/firewall configuration without public
  DNS or ACME calls. Do not use the production `apply.sh` unchanged: it starts
  services under their production identities and expects production addresses.
- Egress: validate restored CrowdSec DB/config and bouncer registration locally,
  plus Caddy, HAProxy, Squid and coturn syntax. Use synthetic/local backends and
  isolated test credentials for listener/authentication checks. Do not enable
  console reporting, copied Tailscale state or production TURN/media traffic.
  Do not probe Minecraft port 25565; it wakes the production JVM.
- Reboot the quarantined clone and repeat checks, including the egress fence,
  service startup policy, data integrity and intended local-only runtime tests.
  Record the selected backup's recovery point, elapsed recovery time and any
  missing prerequisites. A full production cutover is a separate operation.

## Cleanup and evidence

Save only aggregate results, image/release IDs, backup timestamps and test
outcomes. Verify the production public health paths still work. Delete only the
recorded clone ID, and confirm its deletion before removing its unique firewall
and tag. Do not delete backup images, production droplets, reserved IPs or VPCs.
Check that no rehearsal resources continue accruing charges. Leave the tracked
rehearsal workstream open until these provider restore/runtime/reboot/cleanup
steps have actually completed.
