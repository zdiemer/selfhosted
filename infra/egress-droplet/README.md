# Egress SFO3 managed configuration — adoption pending

**Not deployed.** Tailscale SSH currently requires an authentication check, so the
live host's configuration, credentials and service identities have not been
reviewed. `live_configuration_reviewed: false` deliberately blocks apply. These
files describe the repository baseline and proposed hardening, not verified live
state. Do not flip that flag to skip review.

This follows the Whatnow model: Ansible desired state in Git, credentials in
1Password, tmpfs staging, hidden secret diffs, validated configurations and
runtime probes. It adopts an existing egress host with its package repositories,
CrowdSec setup/collections, ACME state and Tailscale enrollment intact. It is not
yet a bare-droplet provisioning recipe or a Tailscale account policy manager.

## Complete adoption

1. Authenticate the Tailscale SSH check, then inspect running services, package
   versions, addresses, listeners, Squid configuration, resolver configuration,
   HAProxy/Caddy backends, TURN settings and nftables rules. Confirm the service
   users (`proxy`, `caddy`, `haproxy`, `turnserver`) match the proposed rules.
2. Authenticate `op` with the existing `scripts/op-session.sh` helper and run
   `python3 infra/egress-droplet/import-existing.py`. It imports the current
   Squid credentials, TURN password, existing Squid TLS material and CrowdSec
   credentials into `homelab/egress-sfo3-production`, field `secrets.json`. It
   preserves content, ownership and permissions, verifies the vault round trip,
   prints no secrets and refuses to overwrite an existing item. Tailscale node
   identity and Caddy certificate storage are not copied to the vault.
3. Reconcile `config.yml` against the live host, including `public_ipv4`, backend
   nodes and port/user settings. Review differences before setting
   `live_configuration_reviewed: true`. Check that all four relays/services are
   actually enabled; the baseline assumes Squid, media, Minecraft and TURN.
4. Run `infra/egress-droplet/apply.sh --check --diff`, then `apply.sh`. The current
   service package repositories must already exist, as established by the old
   bootstrap. No credentials are generated or rotated during apply.
5. Verify the runtime probes, media service, proxy access from the cluster, TURN
   and CrowdSec enforcement. Verify HAProxy workers run as `haproxy`. Avoid
   periodic Minecraft TCP probes: they wake the auto-paused server.
6. Repeat apply and verify no configuration changes. Commit the reviewed
   inventory and record deployment verification before calling adoption complete.

## Proposed policy

- Squid denies private, loopback, link-local, tailnet, multicast and the droplet's
  own public address before the authenticated allow rule. Public HTTP/CONNECT
  and existing Basic authentication remain available over Tailscale.
- Kernel rules independently deny private/local destinations for the service
  users, including metadata, so a Squid policy bypass does not suffice. Incoming
  connection replies are allowed first, preserving authenticated proxy replies
  and SSH. Local DNS and Caddy's localhost reload API are explicit exceptions.
- Only Caddy may open tailnet connections to configured media nodes on 30096;
  only HAProxy workers may reach configured Minecraft nodes on 25565. Arbitrary
  peers on those ports are no longer allowed. ICMP diagnostics remain available.
- Public listener ports and existing connection meters are preserved. The proxy
  has no public fallback. Unused DDNS proxy-allowlist refresh is retired.
- Firewall updates replace only `inet egress` atomically and preserve CrowdSec and
  Tailscale tables. The packaged nftables unit's potentially global-flush stop
  action is overridden. Public service units require the firewall before start.
- Runtime tests check proxy authentication, successful public CONNECT, private
  destination rejection, direct kernel blocking as the Squid user, and media
  reachability as Caddy. No response bodies or credentials are printed.

A root compromise can still change this host firewall. Independently enforced
Tailscale grants/ACLs must also restrict the droplet to the exact backend nodes
and ports. Tailnet policy access has not been established, so that protection is
not claimed here. DNS/DO resources, backup policy and provider DDoS protection
remain separate, as with the Whatnow host configuration.

Existing bootstrap remains available for an unmanaged host, but refuses to run
once `/etc/egress-iac-managed` is present. Do not use it to change an adopted host.
Secrets are delivered as protected runtime files; host root and whole-disk backups
can still read them. For replacement-host recovery, preserve the vault item and
restore/re-enroll the relevant service state; do not clone a Tailscale identity
onto two running machines.
